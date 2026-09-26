#!/usr/bin/env python3
"""
Conversation extraction utility for obsidian-ingest / obsidian-claude-export-ingest skills.
Handles all mechanical JSON parsing so the LLM only does routing decisions and paraphrasing.

Usage:
  extract_conversations.py check-cli
      Exit 0 if the Obsidian CLI is responsive, exit 1 with a message if not.
      Run this at the start of any skill that uses Obsidian CLI commands.

  extract_conversations.py inventory [--scope <project-path>]
      Emit the whole ingest inventory as one JSON blob — obsidian-ingest Steps 2-4.
      Call it with NO arguments: the vault-root _sources/ is the only source
      directory. --scope narrows the scan to one project folder and is retained
      as a diagnostic (used by retire_sources.py and for checking a stray nested
      _sources/); obsidian-ingest never passes it. Output keys:
        mode, scope, vault_root, source_dir, source_dir_exists, notes_scanned,
        uuid_inventory {uuid: {note, message_count, last_ingested}},
        ignored_uuids [uuid],
        sources [{filename, path, type, sha256, verdict, notes, recorded_sha256}],
        owners {note_path: human|agent|shared}
      type is 'claude-export', 'claude-projects', 'chatgpt-export', or 'document'; verdict is new | changed | unchanged
      | untracked (recorded as a flat sources: string with no sha256).
      Run this INSTEAD of hand-writing a scan script — see SKILL.md Steps 2-4.

  extract_conversations.py <json_file> sha256
      Print SHA-256 hex digest of file content.

  extract_conversations.py <json_file> detect
      Exit 0 if file is a valid Claude export JSON array, exit 1 otherwise.

  extract_conversations.py <json_file> list
      Print tab-separated rows for each non-empty conversation:
        uuid | name | message_count | created_at | updated_at

  extract_conversations.py <json_file> classify <inventory_json>
      Classify each conversation against a stored inventory.
      inventory_json: '{"<uuid>": <message_count>, ...}'
      Output rows: uuid | action | name | stored_count | current_count
      Actions: unchanged, decreased, update, new

  extract_conversations.py <json_file> extract <uuid> [--from N]
      Print all messages in a conversation formatted for LLM paraphrasing.
      --from N: skip the first N messages (for update sections only).
      Attached files print inline under their message. Files Claude created print
      once at the end, rebuilt through their edits. See conversation_files().

  extract_conversations.py <json_file> files <uuid> [--full]
      Print only the files in one conversation. Used to backfill conversations
      ingested before files were extracted. --full lifts MAX_FILE_CHARS.

  extract_conversations.py <projects_zip> project-doc <doc_uuid> [--full]
      Print one doc from a projects export, with its project name, instructions,
      and content sha256. Inventory lists the pending docs of a projects zip in
      `pending_docs` (type `claude-projects`).

  extract_conversations.py <chatgpt_zip> chatgpt-list
      JSON list of every conversation in a ChatGPT export: id, title, message
      count, timestamps, size, and attachment names.

  extract_conversations.py <chatgpt_zip> chatgpt-extract <conversation_id> [--from N]
      Print one ChatGPT conversation for paraphrasing — the branch ending at
      `current_node`, without reasoning content. Attachments print as names only.

  extract_conversations.py files-backlog
      JSON list of ingested conversations with file content whose ingested_uuids
      entry lacks `files_ingested: true`. Takes no json_file.

Notes:
  - message_count is len(chat_messages) — the raw array length, matching the stored field.
  - Conversations with empty name or no messages with text are skipped in list/classify/extract.
  - All tab characters and newlines in name are collapsed to spaces for TSV output.
  - <json_file> may also be a .zip (a raw Claude Desktop export archive). detect/list/
    classify/extract transparently read the export JSON from inside it (preferring a
    conversations.json member), so a dropped zip behaves as if it were extracted. sha256
    hashes the *inner* export content — the conversation JSON is the change-tracking
    authority, not the zip packaging — so an unchanged re-download hashes the same even
    though the zip's outer bytes vary, and a zip and a hand-extracted conversations.json
    with identical content share one hash.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeIs

# PyYAML is imported lazily by `inventory` only (see _require_yaml). Every other
# subcommand is pure stdlib and must keep working without it — `check-cli` in
# particular runs before anything else in the skill, so a hard top-level import
# would turn a missing optional dependency into a total ingest failure.
# Typed Any rather than left as None so the module-level rebind type-checks.
yaml: Any = None


def cmd_check_cli() -> None:
    """Verify the Obsidian CLI is installed and responsive. Exit 1 with a message if not."""
    try:
        result = subprocess.run(
            ["obsidian", "vault"],
            capture_output=True, text=True, timeout=25
        )
        if result.returncode == 0:
            print("Obsidian CLI: OK")
            return
        # Non-zero exit may still mean CLI is present but no vault open
        print("Obsidian CLI: OK (vault may not be open yet)")
    except FileNotFoundError:
        print(
            "ERROR: 'obsidian' command not found.\n"
            "Install the Obsidian CLI and ensure it is in your PATH.\n"
            "See: https://obsidian.md/help/cli",
            file=sys.stderr,
        )
        sys.exit(1)
    except subprocess.TimeoutExpired:
        print(
            "WARNING: Obsidian CLI timed out — Obsidian may not be running.\n"
            "Please open Obsidian, wait for your vault to load, then try again.",
            file=sys.stderr,
        )
        sys.exit(1)


def load_json(path: Path):
    try:
        return json.loads(path.read_bytes())
    except (json.JSONDecodeError, OSError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


def _export_member_from_zip(path: Path):
    """Return (raw_bytes, parsed_data) for the export member inside a .zip.

    Scans .json members, preferring one basenamed `conversations.json`, and
    returns the first that validates as a Claude export array. `projects.json`
    and `users.json` in a Claude data export won't validate (no chat_messages),
    so the fallback scan is safe. Returns (None, None) if no member validates.
    """
    try:
        with zipfile.ZipFile(path) as zf:
            members = [n for n in zf.namelist()
                       if n.lower().endswith(".json") and not n.endswith("/")]
            members.sort(key=lambda n: (0 if Path(n).name.lower() == "conversations.json" else 1, n))
            for name in members:
                try:
                    raw = zf.read(name)
                    data = json.loads(raw)
                except (json.JSONDecodeError, KeyError, OSError):
                    continue
                if is_claude_export(data):
                    return raw, data
    except (zipfile.BadZipFile, OSError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
    return None, None


def load_export_data(path: Path):
    """Load Claude export JSON, transparently reading from inside a .zip archive.

    A dropped Claude Desktop export .zip behaves exactly as if the user had
    extracted conversations.json themselves. Returns parsed export data, or None
    if the file is a zip with no valid export member (callers then fail the
    is_claude_export check as usual). Non-zip files go through load_json.
    """
    if zipfile.is_zipfile(path):
        return _export_member_from_zip(path)[1]
    return load_json(path)


def export_content_bytes(path: Path) -> bytes:
    """Return the authoritative content bytes to hash for change-tracking.

    The conversation content — not the packaging — is the authority. For a .zip,
    that is the raw bytes of the inner export member (the same bytes a
    hand-extracted conversations.json would contain), so re-downloading an
    unchanged export produces a stable hash even though the zip's outer bytes
    (member mod-times, compression, ordering) vary. For a bare .json it is the
    file itself. A zip with no valid export member falls back to the outer bytes.
    """
    if zipfile.is_zipfile(path):
        raw, _ = _export_member_from_zip(path)
        if raw is not None:
            return raw
        if load_chatgpt(path):
            with zipfile.ZipFile(path) as zf:
                return zf.read("conversations.json")
        members = _project_members(path)
        if members:
            # A projects export has one JSON member per project. Hash names and bytes
            # in sorted order, so a re-download with the same content matches.
            with zipfile.ZipFile(path) as zf:
                return b"".join(name.encode() + b"\0" + zf.read(name) for name in members)
    return path.read_bytes()


# ----- ChatGPT export ---------------------------------------------------------
#
# A ChatGPT data export is a zip holding conversations.json plus chat.html,
# attachments as file_*.dat, and a few small metadata files. Measured 2026-09-16:
# 73 conversations, 8 MB of JSON, 9 attachments.
#
# Differences from a Claude export that the ingest has to handle:
#   - Messages form a `mapping` TREE. Every regenerate or edit leaves a sibling
#     branch. Walking `current_node` back to the root gives the branch the human
#     actually saw. 66 abandoned nodes were dropped that way across 73 chats.
#   - The export is a FULL DUMP every time, not incremental. The file hash changes
#     on every export while most conversations do not, so change is judged per
#     conversation, like project docs.
#   - Reasoning content types (`thoughts`, `reasoning_recap`) are dropped, the
#     same as Claude's thinking blocks.
#   - Attachments carry no text. Images are never copied into the vault, and a
#     PDF is extracted only when the human says so, and never for a published work.

CHATGPT_SKIP_TYPES = ("thoughts", "reasoning_recap")


def load_chatgpt(path: Path) -> list[dict]:
    """Return the conversations in a ChatGPT export zip, or [] if it is not one."""
    if path.suffix.lower() != ".zip" or not zipfile.is_zipfile(path):
        return []
    try:
        with zipfile.ZipFile(path) as zf:
            if "conversations.json" not in zf.namelist():
                return []
            data = json.loads(zf.read("conversations.json"))
    except (zipfile.BadZipFile, KeyError, OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list) or not data:
        return []
    first = data[0]
    if not (isinstance(first, dict) and "mapping" in first and "conversation_id" in first):
        return []
    return data


def chatgpt_branch(conv: dict) -> list[dict]:
    """Messages from the root to `current_node`, in order — the branch as seen."""
    mapping = conv.get("mapping") or {}
    chain: list[dict] = []
    node, seen = conv.get("current_node"), set()
    while node and node in mapping and node not in seen:
        seen.add(node)                       # cycle guard; exports are dirty
        entry = mapping[node]
        if entry.get("message"):
            chain.append(entry["message"])
        node = entry.get("parent")
    chain.reverse()
    return chain


def chatgpt_text(message: dict) -> str:
    content = message.get("content") or {}
    if content.get("content_type") in CHATGPT_SKIP_TYPES:
        return ""
    parts = [p for p in content.get("parts") or [] if isinstance(p, str)]
    return "\n".join(parts).strip()


def chatgpt_attachments(message: dict) -> list[dict]:
    out = []
    for att in (message.get("metadata") or {}).get("attachments") or []:
        name = att.get("name") or "(unnamed)"
        kind = "pdf" if name.lower().endswith(".pdf") else (
            "image" if name.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")) else "file")
        out.append({"name": name, "kind": kind})
    return out


def chatgpt_summary(conv: dict) -> dict:
    """Title, timestamps, visible message count, and attachments for one chat."""
    messages = [m for m in chatgpt_branch(conv) if chatgpt_text(m)]
    attachments: list[dict] = []
    for msg in chatgpt_branch(conv):
        for att in chatgpt_attachments(msg):
            if att not in attachments:
                attachments.append(att)

    def stamp(value):
        if not value:
            return None
        return datetime.fromtimestamp(value, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    return {
        "uuid": str(conv.get("conversation_id")),
        "app": "chatgpt",
        "name": safe_name(conv.get("title") or ""),
        "message_count": len(messages),
        "created_at": stamp(conv.get("create_time")),
        "updated_at": stamp(conv.get("update_time")),
        "archived": bool(conv.get("is_archived")),
        "chars": sum(len(chatgpt_text(m)) for m in messages),
        "attachments": attachments,
    }


def pending_chats(path: Path, uuid_inventory: dict, ignored: set) -> list[dict]:
    """ChatGPT conversations that are new, or longer than the note records."""
    pending = []
    for conv in load_chatgpt(path):
        item = chatgpt_summary(conv)
        if not item["message_count"] or item["uuid"] in ignored:
            continue
        known = uuid_inventory.get(item["uuid"])
        if known is None:
            item["state"] = "new"
        else:
            stored = known.get("message_count")
            item["note"] = known.get("note")
            if stored is not None and item["message_count"] <= stored:
                continue
            item["state"] = "update"
            item["from_message"] = stored or 0
        pending.append(item)
    pending.sort(key=lambda i: i.get("updated_at") or "", reverse=True)
    return pending


# ----- Claude projects export --------------------------------------------------
#
# The `projects` category of a data export is a zip of `projects/<uuid>.json`, one
# member per project: name, description, prompt_template (instructions), and
# docs[] — each doc with its own uuid, filename, created_at, and full text in
# `content` (.docx and .odt arrive as extracted text). Measured 2026-09-14: 30
# projects, 16 docs with text, 263k chars.
#
# The unit of change is the doc, not the zip. Every export produces a new zip even
# when no doc changed, so a doc is tracked by its uuid plus a sha256 of its content,
# in the `ingested_project_docs:` frontmatter list of the note that holds it.


def _project_members(path: Path) -> list[str]:
    try:
        with zipfile.ZipFile(path) as zf:
            return sorted(n for n in zf.namelist()
                          if n.startswith("projects/") and n.endswith(".json"))
    except (zipfile.BadZipFile, OSError):
        return []


def load_projects(path: Path) -> list[dict]:
    """Return the project dicts inside a projects export zip, or [] if it is not one."""
    projects = []
    members = _project_members(path)
    if not members:
        return []
    with zipfile.ZipFile(path) as zf:
        for name in members:
            try:
                data = json.loads(zf.read(name))
            except (json.JSONDecodeError, KeyError, OSError):
                continue
            if isinstance(data, dict) and data.get("uuid") and "docs" in data:
                projects.append(data)
    return projects


def doc_sha256(doc: dict) -> str:
    return hashlib.sha256((doc.get("content") or "").encode("utf-8")).hexdigest()


def _normalize(text: str) -> str:
    return " ".join((text or "").split())


def is_claude_export(data) -> TypeIs[list]:
    return (
        isinstance(data, list)
        and len(data) > 0
        and isinstance(data[0], dict)
        and all(k in data[0] for k in ("uuid", "name", "chat_messages"))
    )


def safe_name(name: str) -> str:
    return name.replace("\t", " ").replace("\n", " ").replace("\r", " ")


def has_content(conv: dict) -> bool:
    name = conv.get("name", "").strip()
    if not name:
        return False
    messages = conv.get("chat_messages", [])
    return any(m.get("text", "").strip() for m in messages)


# ----- Files inside a conversation --------------------------------------------
#
# A message's `text` field holds only the prose. File content lives elsewhere in
# the same message, and extraction read none of it before 2026-09-14:
#
#   attachments[].extracted_content   full text of a file the human attached or
#                                     pasted (.md, .txt, extracted .docx/.odt)
#   content[] tool_use create_file    full text of a file Claude created
#   content[] tool_use str_replace    one edit to a file Claude created
#   files[]                           one entry per upload; a binary upload (PDF,
#                                     image) has only a uuid and maybe a name
#
# Project knowledge files are NOT here unless the file also passed through a chat.
# Measured 2026-09-14: 2 of 11 project docs matched chat content. They live in the
# separate `projects` export category.

# Per-file character cap for extract output. Measured 2026-09-14 across 57 files in
# 34 conversations: median 7,910 chars, 9 files over 20k, max 49,999, 650k total.
# A 30k cap keeps 87% of files whole (41 of 47 in the backlog) and cuts only the
# long tail to a head excerpt. The `files --full` subcommand lifts the cap.
MAX_FILE_CHARS = 30_000


def conversation_files(conv: dict) -> dict:
    """Collect every file in a conversation, with created files rebuilt to their final state.

    Edits replay in message order per path. Measured 2026-09-14: 108 of 130 edits
    applied cleanly. The rest failed because Claude also changed files through shell
    commands the export does not record as edits. A file with any failed edit is
    marked `complete: False`, so the reader never treats a partial rebuild as final.
    """
    attachments: list[dict] = []
    binaries: list[dict] = []
    created: dict[str, dict] = {}
    orphan_edits = 0

    for index, msg in enumerate(conv.get("chat_messages", [])):
        atts = msg.get("attachments") or []
        for att in atts:
            text = att.get("extracted_content") or ""
            if text.strip():
                attachments.append({
                    "message": index,
                    "name": att.get("file_name") or "(unnamed attachment)",
                    "text": text,
                })
        # A text attachment also appears in files[]. Only the surplus entries are
        # uploads with no retrievable content. Name matching alone misfires, because
        # attachments often carry an empty file_name.
        att_names = {a.get("file_name") for a in atts if a.get("file_name")}
        uploads = msg.get("files") or []
        surplus = max(0, len(uploads) - len(atts))
        named = [f.get("file_name") for f in uploads
                 if f.get("file_name") and f.get("file_name") not in att_names]
        for i in range(surplus):
            binaries.append({"message": index,
                             "name": named[i] if i < len(named) else "(name not in export)"})

        for block in msg.get("content") or []:
            if block.get("type") != "tool_use":
                continue
            inp = block.get("input") or {}
            path = inp.get("path") or ""
            if block.get("name") == "create_file" and path:
                created[path] = {"path": path, "text": inp.get("file_text") or "",
                                 "first_message": index, "last_message": index,
                                 "applied": 0, "failed": 0}
            elif block.get("name") == "str_replace" and path:
                entry = created.get(path)
                if entry is None:
                    orphan_edits += 1
                    continue
                entry["last_message"] = index
                old = inp.get("old_str") or ""
                # The real tool requires a unique match. Zero or several matches means
                # the rebuilt text has already diverged from the real file.
                if old and entry["text"].count(old) == 1:
                    entry["text"] = entry["text"].replace(old, inp.get("new_str") or "", 1)
                    entry["applied"] += 1
                else:
                    entry["failed"] += 1

    files = list(created.values())
    for entry in files:
        entry["complete"] = entry["failed"] == 0
    return {"attachments": attachments, "created": files,
            "binaries": binaries, "orphan_edits": orphan_edits}


def has_file_content(conv: dict) -> bool:
    found = conversation_files(conv)
    return bool(found["attachments"] or found["created"])


def _print_file_body(text: str, full: bool) -> None:
    if full or len(text) <= MAX_FILE_CHARS:
        body = text
    else:
        body = (text[:MAX_FILE_CHARS]
                + f"\n[... excerpt ends: {MAX_FILE_CHARS:,} of {len(text):,} chars shown."
                  " Run the `files` subcommand with --full for the rest ...]")
    print("<<<FILE")
    print(body.rstrip("\n"))
    print("FILE>>>")


def print_created_files(found: dict, from_offset: int, full: bool) -> None:
    """Print created files touched at or after from_offset, in their final state."""
    touched = [f for f in found["created"] if f["last_message"] >= from_offset]
    if not touched:
        return
    print("=" * 60)
    print("Files Claude created in this conversation (final state after edits):")
    for entry in touched:
        edits = entry["applied"] + entry["failed"]
        state = (f"{edits} edits applied" if entry["complete"] else
                 f"INCOMPLETE REBUILD — {entry['failed']} of {edits} edits could not be"
                 " applied; this text may differ from the real final file")
        print(f"[created file: {entry['path']} | {len(entry['text']):,} chars | {state}]")
        _print_file_body(entry["text"], full)
        print("---")


def cmd_sha256(path: Path) -> None:
    print(hashlib.sha256(export_content_bytes(path)).hexdigest())


def cmd_detect(path: Path) -> None:
    data = load_export_data(path)
    if not is_claude_export(data):
        sys.exit(1)


def cmd_list(path: Path) -> None:
    data = load_export_data(path)
    if not is_claude_export(data):
        print("ERROR: not a Claude export JSON", file=sys.stderr)
        sys.exit(1)
    for conv in data:
        if not has_content(conv):
            continue
        uuid = conv.get("uuid", "")
        name = safe_name(conv.get("name", ""))
        msg_count = len(conv.get("chat_messages", []))
        created = conv.get("created_at", "")
        updated = conv.get("updated_at", "")
        print(f"{uuid}\t{name}\t{msg_count}\t{created}\t{updated}")


def cmd_classify(path: Path, inventory_json: str) -> None:
    try:
        inventory = json.loads(inventory_json)
    except json.JSONDecodeError as e:
        print(f"ERROR: invalid inventory JSON: {e}", file=sys.stderr)
        sys.exit(1)

    data = load_export_data(path)
    if not is_claude_export(data):
        print("ERROR: not a Claude export JSON", file=sys.stderr)
        sys.exit(1)

    for conv in data:
        if not has_content(conv):
            continue
        uuid = conv.get("uuid", "")
        name = safe_name(conv.get("name", ""))
        current_count = len(conv.get("chat_messages", []))

        if uuid in inventory:
            stored_count = inventory[uuid]
            if current_count == stored_count:
                action = "unchanged"
            elif current_count < stored_count:
                action = "decreased"
            else:
                action = "update"
        else:
            action = "new"
            stored_count = 0

        print(f"{uuid}\t{action}\t{name}\t{stored_count}\t{current_count}")


def _load_conversation(path: Path, uuid: str) -> dict:
    data = load_export_data(path)
    if not is_claude_export(data):
        print("ERROR: not a Claude export JSON", file=sys.stderr)
        sys.exit(1)
    conv = next((c for c in data if c.get("uuid") == uuid), None)
    if conv is None:
        print(f"ERROR: UUID {uuid} not found", file=sys.stderr)
        sys.exit(1)
    return conv


def cmd_extract(path: Path, uuid: str, from_offset: int) -> None:
    conv = _load_conversation(path, uuid)
    all_messages = conv.get("chat_messages", [])
    messages = all_messages[from_offset:]
    # Replay edits over the whole conversation, even for an update section. A file
    # created before the offset can still be edited after it.
    found = conversation_files(conv)
    attachments_at: dict[int, list[dict]] = {}
    for att in found["attachments"]:
        attachments_at.setdefault(att["message"], []).append(att)
    binaries_at: dict[int, list[dict]] = {}
    for item in found["binaries"]:
        binaries_at.setdefault(item["message"], []).append(item)

    print(f"Conversation: {conv.get('name', '')}")
    print(f"UUID: {uuid}")
    print(f"Total messages: {len(all_messages)}  |  Extracting from offset {from_offset}: {len(messages)} messages")
    print("=" * 60)

    for index, msg in enumerate(messages, start=from_offset):
        text = msg.get("text", "").strip()
        atts = attachments_at.get(index, [])
        bins = binaries_at.get(index, [])
        if not text and not atts and not bins:
            continue
        sender = msg.get("sender", "unknown")
        print(f"[{sender}]: {text}")
        for att in atts:
            print(f"[attached file: {att['name']} | {len(att['text']):,} chars]")
            _print_file_body(att["text"], full=False)
        for item in bins:
            print(f"[uploaded file with no content in the export: {item['name']}]")
        print("---")

    print_created_files(found, from_offset, full=False)


def cmd_files(path: Path, uuid: str, full: bool) -> None:
    """Print only the files of one conversation — the backfill read."""
    conv = _load_conversation(path, uuid)
    found = conversation_files(conv)
    print(f"Conversation: {conv.get('name', '')}")
    print(f"UUID: {uuid}")
    print(f"Total messages: {len(conv.get('chat_messages', []))}")
    print("=" * 60)
    for att in found["attachments"]:
        print(f"[attached file: {att['name']} | message {att['message']} | {len(att['text']):,} chars]")
        _print_file_body(att["text"], full)
        print("---")
    print_created_files(found, 0, full)
    for item in found["binaries"]:
        print(f"[uploaded file with no content in the export: {item['name']} | message {item['message']}]")
    if found["orphan_edits"]:
        print(f"[{found['orphan_edits']} edits target files this conversation did not create — not shown]")


# ----- Inventory (obsidian-ingest Steps 2-4 mechanics) ------------------------
#
# NOTE: vault-root discovery, frontmatter parsing, and note walking are
# derived from obsidian-lint-light/lint.py so each skill stays
# self-contained and independently runnable. If a third consumer appears,
# factor them into a shared module rather than copying a third time.
#
# `writable_roots` is DELIBERATELY SHORTER than the lint's `audited_roots`. The
# lint audits a folder; this decides where ingest may WRITE. A folder can be
# audited but never written to — for example a folder of `owner: human` notes.
# Do not "sync" the two lists.


def _require_yaml() -> None:
    """Bind the module-level `yaml` name, or exit with an install hint."""
    global yaml
    if yaml is not None:
        return
    try:
        import yaml as _yaml
    except ImportError:
        print(
            "ERROR: the 'inventory' subcommand requires PyYAML. "
            "Install with: pip install pyyaml",
            file=sys.stderr,
        )
        sys.exit(2)
    yaml = _yaml


def load_writable_roots(vault: Path) -> tuple[str, ...]:
    """Read `writable_roots` from `_meta/vault-config.yml`: the top-level folders ingest may write to."""
    _require_yaml()
    path = vault / "_meta" / "vault-config.yml"
    if not path.is_file():
        print(f"ERROR: missing {path}. It lists the folders ingest may write to "
              "(writable_roots).", file=sys.stderr)
        sys.exit(1)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        print(f"ERROR: {path} is not valid YAML: {e}", file=sys.stderr)
        sys.exit(1)
    roots = data.get("writable_roots") if isinstance(data, dict) else None
    if not isinstance(roots, list) or not all(isinstance(r, str) and r for r in roots):
        print(f"ERROR: {path}: `writable_roots` must be a list of folder names", file=sys.stderr)
        sys.exit(1)
    return tuple(roots)


def find_vault_root() -> Path:
    """Walk up from this script to the vault root (AGENTS.md or CLAUDE.md + _meta/ marker)."""
    for parent in Path(__file__).resolve().parents:
        marker = (parent / "AGENTS.md").exists() or (parent / "CLAUDE.md").exists()
        if marker and (parent / "_meta").is_dir():
            return parent
    print(
        "ERROR: could not locate vault root (no AGENTS.md or CLAUDE.md + _meta above this script)",
        file=sys.stderr,
    )
    sys.exit(1)


def parse_frontmatter(path: Path) -> dict:
    """Return the note's frontmatter mapping, or {} if absent/unparseable."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}
    try:
        data = yaml.safe_load(text[4:end])
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def iter_notes(vault: Path, scope_rel: str | None = None):
    """Yield note paths under scope (or the writable roots), skipping _-dirs and index.md."""
    roots = [vault / scope_rel] if scope_rel else [vault / top for top in load_writable_roots(vault)]
    for root in roots:
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("_"))
            for fn in sorted(filenames):
                if fn.endswith(".md") and fn != "index.md":
                    yield Path(dirpath) / fn


def source_key(vault: Path, note_path: Path, recorded: str) -> tuple[str, str]:
    """Resolve a recorded `sources:` path to a (project_folder, filename) key.

    Recorded paths are project-relative by convention (`_sources/foo.md` inside a
    project note) but vault-root ingests record the same shape against the vault
    root. Resolution therefore tries project-relative first and falls back to the
    vault root, matching lint.py. The tuple key is what makes the inventory safe:
    a bare `_sources/<basename>` key would collide across projects, and a
    collision does not merely skip a file — it can append an `## Update` section
    to a note belonging to an entirely different project.
    """
    rec = recorded.replace("\\", "/").lstrip("/")
    filename = os.path.basename(rec)
    project = os.path.relpath(note_path.parent, vault)
    if (note_path.parent / rec).exists():
        return project, filename
    if (vault / rec).exists():
        return "", filename
    return project, filename  # unresolved — assume project-relative intent


def load_ignored_uuids(vault: Path) -> list[str]:
    """Read `_meta/ignored-uuids.md`; its YAML list lives in the body, not frontmatter."""
    path = vault / "_meta" / "ignored-uuids.md"
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    marker = text.find("ignored_uuids:")
    if marker < 0:
        return []
    try:
        block = yaml.safe_load(text[marker:])
    except yaml.YAMLError:
        return []
    entries = (block or {}).get("ignored_uuids") or []
    return [
        str(e["uuid"])
        for e in entries
        if isinstance(e, dict) and e.get("uuid")
    ]


def load_project_routes(vault: Path) -> dict[str, dict]:
    """Read `_meta/project-routes.md`: Claude project uuid -> {name, target}.

    `target` is a vault-relative folder or note path, or the word `ignore`. The
    human sets a route once per project. New docs in that project then route
    without a prompt. Like ignored-uuids.md, the YAML list lives in the body.
    """
    path = vault / "_meta" / "project-routes.md"
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8", errors="replace")
    marker = text.find("project_routes:")
    if marker < 0:
        return {}
    try:
        block = yaml.safe_load(text[marker:])
    except yaml.YAMLError:
        return {}
    routes = {}
    for entry in (block or {}).get("project_routes") or []:
        if isinstance(entry, dict) and entry.get("project_uuid") and entry.get("target"):
            routes[str(entry["project_uuid"])] = {"name": entry.get("name"),
                                                  "target": str(entry["target"])}
    return routes


def chat_file_index(vault: Path, uuid_inventory: dict) -> list[dict]:
    """Every attached or created file in every conversation export in _sources/.

    Used to spot a project doc that also passed through a chat. Measured
    2026-09-14: recent docs are often files Claude wrote in a chat that the human
    then added to the project, so the note may already hold that content.
    """
    index = []
    seen: set[tuple[str, str]] = set()
    for path in sorted((vault / "_sources").glob("*")):
        if not path.is_file() or classify_source_file(path) != "claude-export":
            continue
        for conv in load_export_data(path) or []:
            found = conversation_files(conv)
            texts = [(a["name"], a["text"], "attached") for a in found["attachments"]]
            texts += [(Path(c["path"]).name, c["text"],
                       "created" if c["complete"] else "created, incomplete rebuild")
                      for c in found["created"]]
            for name, text, kind in texts:
                key = (str(conv.get("uuid")), name)
                if key in seen:
                    continue
                seen.add(key)
                uuid = str(conv.get("uuid"))
                index.append({"uuid": uuid, "conversation": safe_name(conv.get("name", "")),
                              "name": name, "kind": kind, "norm": _normalize(text),
                              "note": (uuid_inventory.get(uuid) or {}).get("note")})
    return index


def match_chat_copy(doc: dict, index: list[dict]) -> list[dict]:
    """Chat files that hold this doc: same filename, or the same opening text."""
    norm = _normalize(doc.get("content") or "")
    head = norm[:400]
    matches = []
    for item in index:
        same_text = bool(head) and (item["norm"] == norm or head in item["norm"])
        if same_text or item["name"] == doc.get("filename"):
            matches.append({"conversation_uuid": item["uuid"],
                            "conversation": item["conversation"],
                            "file": item["name"], "kind": item["kind"],
                            "identical": item["norm"] == norm,
                            "note": item["note"]})
    return matches


def pending_project_docs(path: Path, doc_inventory: dict, ignored: set,
                         routes: dict, chat_index: list[dict] | None,
                         source_hashes: dict | None = None) -> list[dict]:
    """Docs in a projects export that no note holds at their current content.

    `recorded_as_source` lists notes whose `sources:` already carry this exact
    content hash — a doc the human once copied into _sources/ by hand. Those need
    only an `ingested_project_docs:` entry, not new body text.
    """
    pending = []
    for project in load_projects(path):
        route = routes.get(str(project["uuid"]))
        for doc in project.get("docs") or []:
            if not (doc.get("content") or "").strip():
                continue
            uuid = str(doc.get("uuid"))
            if uuid in ignored or (route and route["target"] == "ignore"):
                continue
            sha = doc_sha256(doc)
            known = doc_inventory.get(uuid)
            if known and sha in known["sha256s"]:
                continue
            pending.append({
                "doc_uuid": uuid,
                "filename": doc.get("filename"),
                "project": safe_name(project.get("name", "")),
                "project_uuid": str(project["uuid"]),
                "starter_project": bool(project.get("is_starter_project")),
                "created_at": doc.get("created_at"),
                "chars": len(doc.get("content") or ""),
                "sha256": sha,
                "state": "changed" if known else "new",
                "notes": known["notes"] if known else [],
                "route": route["target"] if route else None,
                "recorded_as_source": (source_hashes or {}).get(sha, []),
                "chat_copies": match_chat_copy(doc, chat_index) if chat_index is not None else [],
            })
    return pending


def classify_source_file(path: Path) -> str:
    """'claude-export' for a conversations export, 'claude-projects' for a projects
    export zip, else 'document'."""
    if path.suffix.lower() not in (".json", ".zip"):
        return "document"
    try:
        if is_claude_export(load_export_data(path)):
            return "claude-export"
        if path.suffix.lower() == ".zip" and load_projects(path):
            return "claude-projects"
        if load_chatgpt(path):
            return "chatgpt-export"
        return "document"
    except SystemExit:
        return "document"
    except Exception:
        return "document"


def missing_export_uuids(path: Path, stype: str, verdict: str,
                         uuid_inventory: dict, ignored: set) -> list[str]:
    """Return the uuids inside a Claude export that no note has ingested yet.

    A source file's verdict is otherwise decided by content hash alone. That is
    wrong for a multi-conversation export. The first note to record the hash
    marks the whole file `unchanged`, and the entry point then skips it without
    running anything against it. Every conversation that was never written is
    dropped, permanently and silently.

    This is what a partial run leaves behind, and a partial run is ordinary: a
    crash, a lost network, a context compaction, or the user stopping the run.
    Measured on 2026-08-09 — an export read `unchanged` with 5 of 16
    conversations ingested, which would have dropped the other 11.

    Only an `unchanged` export can hide this. `new`, `changed`, and `untracked`
    are processed regardless, and a document carries one unit of content.
    """
    if stype != "claude-export" or verdict != "unchanged":
        return []
    data = load_export_data(path)
    if not is_claude_export(data):
        return []
    missing = []
    for conv in data:
        uuid = conv.get("uuid")
        if not uuid or not has_content(conv):
            continue
        uuid = str(uuid)
        if uuid in ignored or uuid in uuid_inventory:
            continue
        missing.append(uuid)
    return missing


def cmd_inventory(scope_rel: str | None) -> None:
    """Emit the full ingest inventory as one JSON blob (Steps 2-4 mechanics).

    Replaces hand-written per-run scanning: builds the UUID and SHA256
    inventories, loads the ignored-UUID set, then discovers the active
    `_sources/` directory and classifies + hashes every file in it with a
    new/changed/unchanged/untracked verdict.
    """
    _require_yaml()
    vault = find_vault_root()
    uuid_inventory: dict[str, dict] = {}
    doc_inventory: dict[str, dict] = {}
    source_hashes: dict[str, list[dict]] = {}   # every sources: sha256, retired included
    sha_inventory: dict[str, dict] = {}
    owners: dict[str, str] = {}
    notes_scanned = 0

    for note in iter_notes(vault, scope_rel):
        fm = parse_frontmatter(note)
        rel = os.path.relpath(note, vault)
        notes_scanned += 1
        owners[rel] = str(fm.get("owner") or "shared")

        for entry in fm.get("ingested_project_docs") or []:
            if isinstance(entry, dict) and entry.get("uuid"):
                slot = doc_inventory.setdefault(str(entry["uuid"]),
                                                {"sha256s": set(), "notes": []})
                if entry.get("sha256"):
                    slot["sha256s"].add(str(entry["sha256"]))
                if rel not in slot["notes"]:
                    slot["notes"].append(rel)

        for entry in fm.get("ingested_uuids") or []:
            if isinstance(entry, dict) and entry.get("uuid"):
                stamp = entry.get("last_ingested")
                uuid_inventory[str(entry["uuid"])] = {
                    "note": rel,
                    "message_count": entry.get("message_count"),
                    # PyYAML coerces unquoted ISO timestamps to datetime objects,
                    # which json cannot serialize — keep them as text.
                    "last_ingested": None if stamp is None else str(stamp),
                    "files_ingested": bool(entry.get("files_ingested")),
                }
            elif isinstance(entry, str):
                uuid_inventory[entry] = {"note": rel, "message_count": None,
                                         "last_ingested": None, "files_ingested": False}

        for item in fm.get("sources") or []:
            if isinstance(item, str):
                recorded, sha = item, None
            elif isinstance(item, dict) and item.get("path"):
                recorded, sha = str(item["path"]), item.get("sha256")
                if sha:
                    source_hashes.setdefault(str(sha), []).append(
                        {"path": recorded, "note": rel, "retired": bool(item.get("retired"))})
            else:
                continue
            project, filename = source_key(vault, note, recorded)
            # One source can legitimately feed several notes (an export routed to
            # many, or a doc whose decision touches more than one note), so keep
            # every citing note. Reporting only the first would misdirect Step 5b,
            # which skips target discovery and writes to the stored note.
            slot = sha_inventory.setdefault(
                f"{project}|{filename}",
                {"notes": [], "recorded_sha256": None, "recorded_path": recorded},
            )
            slot["notes"].append(rel)
            if slot["recorded_sha256"] is None:
                slot["recorded_sha256"] = sha

    source_project = scope_rel or ""
    source_dir = (vault / scope_rel / "_sources") if scope_rel else (vault / "_sources")
    ignored = set(load_ignored_uuids(vault)) if not scope_rel else set()
    chat_index: list[dict] | None = None   # built only if a projects export is present
    discovered = []
    for path in sorted(source_dir.glob("*")) if source_dir.is_dir() else []:
        if not path.is_file() or path.name.startswith("."):
            continue
        digest = hashlib.sha256(export_content_bytes(path)).hexdigest()
        known = sha_inventory.get(f"{source_project}|{path.name}")
        if known is None:
            verdict = "new"
        elif known["recorded_sha256"] is None:
            verdict = "untracked"
        elif known["recorded_sha256"] == digest:
            verdict = "unchanged"
        else:
            verdict = "changed"
        stype = classify_source_file(path)
        docs: list[dict] = []
        if stype == "claude-projects":
            if chat_index is None:
                chat_index = chat_file_index(vault, uuid_inventory)
            docs = pending_project_docs(path, doc_inventory, ignored,
                                        load_project_routes(vault), chat_index,
                                        source_hashes)
            # The doc is the unit of change, so the doc list decides the verdict.
            # A new zip whose docs are all held is `unchanged`. A known zip with
            # pending docs is `incomplete`, like a partly ingested chat export.
            if not docs:
                verdict = "unchanged"
            elif verdict == "unchanged":
                verdict = "incomplete"
        chats: list[dict] = []
        if stype == "chatgpt-export":
            chats = pending_chats(path, uuid_inventory, ignored)
            # A ChatGPT export is a full dump, so the file hash moves on every
            # export while most conversations do not. The pending list decides.
            if not chats:
                verdict = "unchanged"
            elif verdict == "unchanged":
                verdict = "incomplete"
        missing = missing_export_uuids(path, stype, verdict, uuid_inventory, ignored)
        if missing:
            # The hash matches, but conversations inside it were never ingested.
            # Reporting `unchanged` here would make the entry point skip the file
            # and drop those conversations permanently. See missing_export_uuids.
            verdict = "incomplete"
        discovered.append({
            "filename": path.name,
            "path": os.path.relpath(path, vault),
            "type": stype,
            "sha256": digest,
            "verdict": verdict,
            "notes": known["notes"] if known else [],
            "recorded_sha256": known["recorded_sha256"] if known else None,
            "missing_uuids": missing,
            "pending_docs": docs,
            "pending_chats": chats,
        })

    json.dump({
        "mode": "project" if scope_rel else "vault",
        "scope": scope_rel,
        "vault_root": str(vault),
        "source_dir": os.path.relpath(source_dir, vault),
        "source_dir_exists": source_dir.is_dir(),
        "notes_scanned": notes_scanned,
        "uuid_inventory": uuid_inventory,
        "ignored_uuids": load_ignored_uuids(vault) if not scope_rel else [],
        "sources": discovered,
        "owners": owners,
    }, sys.stdout, indent=1, sort_keys=False)
    print()


def cmd_chatgpt_list(path: Path) -> None:
    """JSON list of every conversation in a ChatGPT export, newest update first."""
    data = load_chatgpt(path)
    if not data:
        print(f"ERROR: {path} is not a ChatGPT export", file=sys.stderr)
        sys.exit(1)
    rows = [chatgpt_summary(c) for c in data]
    rows.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
    json.dump(rows, sys.stdout, indent=1)
    print()


def cmd_chatgpt_extract(path: Path, conv_id: str, from_offset: int) -> None:
    """Print one ChatGPT conversation for paraphrasing.

    Only the branch ending at `current_node` prints. Reasoning content is
    dropped. An attachment prints as a name, never as content: images never enter
    the vault, and a PDF is extracted only when the human approves it.
    """
    conv = next((c for c in load_chatgpt(path) if str(c.get("conversation_id")) == conv_id), None)
    if conv is None:
        print(f"ERROR: conversation {conv_id} not found in {path}", file=sys.stderr)
        sys.exit(1)
    info = chatgpt_summary(conv)
    messages = [m for m in chatgpt_branch(conv) if chatgpt_text(m) or chatgpt_attachments(m)]
    print(f"Conversation: {info['name']}")
    print(f"ChatGPT id: {conv_id}  |  app: chatgpt")
    print(f"Created {info['created_at']}  |  updated {info['updated_at']}")
    print(f"Visible messages: {info['message_count']}  |  Extracting from offset {from_offset}")
    if info["attachments"]:
        print("Attachments (content not in the export): "
              + ", ".join(f"{a['name']} [{a['kind']}]" for a in info["attachments"]))
    print("=" * 60)
    for index, msg in enumerate(messages):
        if index < from_offset:
            continue
        role = (msg.get("author") or {}).get("role", "unknown")
        text = chatgpt_text(msg)
        print(f"[{role}]: {text}")
        for att in chatgpt_attachments(msg):
            print(f"[attached {att['kind']}: {att['name']} — content not in the export]")
        print("---")


def cmd_project_doc(path: Path, doc_uuid: str, full: bool) -> None:
    """Print one project doc — the read step of project ingest."""
    for project in load_projects(path):
        for doc in project.get("docs") or []:
            if str(doc.get("uuid")) == doc_uuid:
                text = doc.get("content") or ""
                print(f"Project: {project.get('name', '')}")
                print(f"Project UUID: {project.get('uuid')}")
                if (project.get("description") or "").strip():
                    print(f"Description: {project['description'].strip()}")
                if (project.get("prompt_template") or "").strip():
                    print(f"Instructions: {project['prompt_template'].strip()}")
                print(f"Doc: {doc.get('filename')} | uuid {doc_uuid} | created {doc.get('created_at')}"
                      f" | {len(text):,} chars | sha256 {doc_sha256(doc)}")
                print("=" * 60)
                _print_file_body(text, full)
                return
    print(f"ERROR: doc {doc_uuid} not found in {path}", file=sys.stderr)
    sys.exit(1)


def cmd_files_backlog() -> None:
    """List ingested conversations whose files no note holds yet.

    A conversation ingested before 2026-09-14 went through an extractor that read
    only message text. Its export hash and message count have not changed, so the
    normal verdicts call it `unchanged` forever. This list is the only way back to
    those files. An `ingested_uuids` entry with `files_ingested: true` is done.

    One conversation can appear in several exports. The copy with the most
    messages wins, because a later export only ever adds messages.
    """
    _require_yaml()
    vault = find_vault_root()
    wanted: dict[str, dict] = {}
    done: set[str] = set()
    for note in iter_notes(vault, None):
        for entry in parse_frontmatter(note).get("ingested_uuids") or []:
            if not isinstance(entry, dict) or not entry.get("uuid"):
                continue
            if entry.get("files_ingested"):
                done.add(str(entry["uuid"]))
            else:
                wanted[str(entry["uuid"])] = {"note": os.path.relpath(note, vault)}
    # One conversation can feed several notes. A flag on any of them settles it.
    for uuid in done:
        wanted.pop(uuid, None)

    best: dict[str, tuple[int, Path, dict]] = {}
    for path in sorted((vault / "_sources").glob("*")):
        if not path.is_file() or classify_source_file(path) != "claude-export":
            continue
        for conv in load_export_data(path) or []:
            uuid = str(conv.get("uuid"))
            if uuid not in wanted:
                continue
            count = len(conv.get("chat_messages", []))
            if uuid not in best or count >= best[uuid][0]:
                best[uuid] = (count, path, conv)

    rows = []
    for uuid, (_, path, conv) in best.items():
        found = conversation_files(conv)
        if not (found["attachments"] or found["created"] or found["binaries"]):
            continue
        files = ([{"kind": "attached", "name": a["name"], "chars": len(a["text"])}
                  for a in found["attachments"]]
                 + [{"kind": "created", "name": Path(c["path"]).name, "chars": len(c["text"]),
                     "complete": c["complete"]} for c in found["created"]])
        rows.append({
            "uuid": uuid,
            "name": safe_name(conv.get("name", "")),
            "note": wanted[uuid]["note"],
            "source": os.path.relpath(path, vault),
            "total_chars": sum(f["chars"] for f in files),
            "files": files,
            "no_content": [b["name"] for b in found["binaries"]],
        })
    rows.sort(key=lambda r: r["name"].lower())
    json.dump(rows, sys.stdout, indent=1)
    print()


def main():
    parser = argparse.ArgumentParser(
        description="Claude export JSON extraction utility for obsidian-ingest"
    )
    # check-cli and inventory take no json_file argument — handle them before
    # the normal subparser tree, which requires one.
    if len(sys.argv) == 2 and sys.argv[1] == "check-cli":
        cmd_check_cli()
        return
    if len(sys.argv) >= 2 and sys.argv[1] == "inventory":
        inv = argparse.ArgumentParser(prog="extract_conversations.py inventory")
        inv.add_argument(
            "--scope",
            default=None,
            metavar="PROJECT_PATH",
            help="DIAGNOSTIC: narrow the scan to one project folder. "
                 "obsidian-ingest never passes this — the vault-root _sources/ "
                 "is the only source directory. Used by retire_sources.py and "
                 "for inspecting a stray nested _sources/.",
        )
        cmd_inventory(inv.parse_args(sys.argv[2:]).scope)
        return
    if len(sys.argv) == 2 and sys.argv[1] == "files-backlog":
        cmd_files_backlog()
        return

    parser.add_argument("json_file", type=Path, help="Path to Claude export JSON file")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("sha256", help="Print SHA-256 of file")
    sub.add_parser("detect", help="Exit 0 if valid Claude export JSON, else exit 1")
    sub.add_parser("list", help="List conversations (TSV: uuid, name, msg_count, created, updated)")

    classify_p = sub.add_parser("classify", help="Classify conversations against stored inventory")
    classify_p.add_argument(
        "inventory_json",
        help='JSON string mapping uuid to stored message_count, e.g. \'{"uuid1": 8, "uuid2": 4}\'',
    )

    extract_p = sub.add_parser("extract", help="Print conversation messages for LLM paraphrasing")
    extract_p.add_argument("uuid", help="Conversation UUID")
    extract_p.add_argument(
        "--from",
        dest="from_offset",
        type=int,
        default=0,
        metavar="N",
        help="Skip first N messages (use stored message_count for update sections)",
    )

    sub.add_parser("chatgpt-list", help="JSON list of conversations in a ChatGPT export")

    cg_p = sub.add_parser("chatgpt-extract", help="Print one ChatGPT conversation")
    cg_p.add_argument("conversation_id", help="ChatGPT conversation_id (from inventory pending_chats)")
    cg_p.add_argument("--from", dest="from_offset", type=int, default=0, metavar="N",
                      help="Skip the first N visible messages (update sections only)")

    doc_p = sub.add_parser("project-doc", help="Print one doc from a projects export zip")
    doc_p.add_argument("doc_uuid", help="Project doc UUID (from inventory pending_docs)")
    doc_p.add_argument("--full", action="store_true",
                       help=f"Do not cut the doc at {MAX_FILE_CHARS:,} chars")

    files_p = sub.add_parser("files", help="Print only the files in one conversation (backfill)")
    files_p.add_argument("uuid", help="Conversation UUID")
    files_p.add_argument("--full", action="store_true",
                         help=f"Do not cut files at {MAX_FILE_CHARS:,} chars")

    args = parser.parse_args()

    if not args.json_file.exists():
        print(f"ERROR: file not found: {args.json_file}", file=sys.stderr)
        sys.exit(1)

    if args.command == "sha256":
        cmd_sha256(args.json_file)
    elif args.command == "detect":
        cmd_detect(args.json_file)
    elif args.command == "list":
        cmd_list(args.json_file)
    elif args.command == "classify":
        cmd_classify(args.json_file, args.inventory_json)
    elif args.command == "extract":
        cmd_extract(args.json_file, args.uuid, args.from_offset)
    elif args.command == "files":
        cmd_files(args.json_file, args.uuid, args.full)
    elif args.command == "chatgpt-list":
        cmd_chatgpt_list(args.json_file)
    elif args.command == "chatgpt-extract":
        cmd_chatgpt_extract(args.json_file, args.conversation_id, args.from_offset)
    elif args.command == "project-doc":
        cmd_project_doc(args.json_file, args.doc_uuid, args.full)


if __name__ == "__main__":
    main()
