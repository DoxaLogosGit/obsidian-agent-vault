#!/usr/bin/env python3
"""Retire ingested source files so a project's _sources/ directory can be deleted.

The vault treats `sources:` as append-only history, so simply deleting a source
file turns every citing entry into a lint "stale source" forever. Retiring marks
each entry `retired: true` and — critically — records the file's sha256 *before*
it is deleted, so the lineage record stays complete. `obsidian-lint-light` skips
the stale and drift checks for retired entries and reports them under their own
count, so the deletion stays visible rather than silently disappearing.

Usage:
  retire_sources.py --scope "<project-path>"           # dry run (default)
  retire_sources.py --scope "<project-path>" --apply   # write the changes

Dry run prints the exact per-note frontmatter change. Nothing is written without
--apply, and this script never deletes anything: after a successful --apply run,
removing the `_sources/` directory is a separate manual step.

Idempotent: entries already carrying `retired:` are left untouched, so a partial
or repeated run is safe.

Notes:
  - Only the `sources:` block is rewritten, line by line. The rest of the
    frontmatter is never reparsed or reserialized, so key order, quoting, and
    formatting of hand-authored notes survive intact.
  - An entry is only retired when it actually resolves to the target directory,
    so a vault-root source that happens to share a basename is never touched.
  - Flat `sources:` strings are upgraded to path/sha256/retired here. That is
    deliberate and is the one exception to "upgrade only on ingest touch":
    retirement reads the file, and it is the last chance to record its hash.
"""

import argparse
import hashlib
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from extract_conversations import find_vault_root, iter_notes, source_key  # noqa: E402

ENTRY_RE = re.compile(r"^(\s*)-\s+(.*?)\s*$")


def frontmatter_lines(text: str):
    """Return (pre, fm_lines, post) splitting a note around its frontmatter."""
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4)
    if end < 0:
        return None
    return text[:4], text[4:end + 1].splitlines(keepends=True), text[end + 1:]


def find_sources_block(fm_lines):
    """Return (start, end) line indices of the `sources:` block, or None."""
    for i, line in enumerate(fm_lines):
        if re.match(r"^sources:\s*$", line):
            j = i + 1
            while j < len(fm_lines):
                stripped = fm_lines[j].strip()
                if not stripped:
                    break
                if not fm_lines[j][:1].isspace():
                    break
                j += 1
            return i, j
    return None


def split_entries(block):
    """Group block lines into list entries (each starting with `- `)."""
    entries, current = [], None
    for line in block:
        if ENTRY_RE.match(line) and line.lstrip().startswith("- "):
            if current is not None:
                entries.append(current)
            current = [line]
        elif current is not None:
            current.append(line)
    if current is not None:
        entries.append(current)
    return entries


def unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def entry_recorded_path(entry):
    """Return (indent, recorded_path) for an entry, or (None, None)."""
    m = ENTRY_RE.match(entry[0])
    if not m:
        return None, None
    indent, rest = m.group(1), m.group(2)
    m_path = re.match(r"^path:\s*(.+)$", rest)
    if m_path:
        return indent, unquote(m_path.group(1))
    if ":" not in rest:  # flat string form
        return indent, unquote(rest)
    # `- path:` with the value on a following line, or some other mapping key
    for line in entry[1:]:
        m_child = re.match(r"^\s*path:\s*(.+)$", line)
        if m_child:
            return indent, unquote(m_child.group(1))
    return indent, None


def entry_is_retired(entry) -> bool:
    return any(re.match(r"^\s*retired:\s*\S", line) for line in entry)


def rewrite_entry(entry, indent: str, recorded_path: str, digest: str):
    """Rebuild an entry as path/sha256/retired, preserving any other child keys."""
    child = indent + "  "
    preserved = [
        line for line in entry[1:]
        if not re.match(r"^\s*(path|sha256|retired):", line) and line.strip()
    ]
    quoted = recorded_path
    if re.search(r"[:#]", quoted) and not quoted.startswith(('"', "'")):
        quoted = f'"{quoted}"'
    return [
        f"{indent}- path: {quoted}\n",
        f"{child}sha256: {digest}\n",
        f"{child}retired: true\n",
        *preserved,
    ]


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("--scope", required=True, metavar="PROJECT_PATH",
                    help="vault-relative project folder whose _sources/ to retire")
    ap.add_argument("--apply", action="store_true",
                    help="write the changes (default is a dry run)")
    args = ap.parse_args()

    vault = find_vault_root()
    scope = args.scope.strip("/")
    src_dir = vault / scope / "_sources"
    if not src_dir.is_dir():
        print(f"No _sources/ directory at {scope}/_sources", file=sys.stderr)
        return 1

    targets = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(src_dir.glob("*"))
        if p.is_file() and not p.name.startswith(".")
    }
    if not targets:
        print(f"{scope}/_sources is empty — nothing to retire; safe to rmdir.")
        return 0

    print(f"Target: {scope}/_sources  ({len(targets)} file(s))\n")

    edited_notes = 0
    edited_entries = 0
    already = 0
    seen_files = set()

    for note in iter_notes(vault, None):
        text = note.read_text(encoding="utf-8", errors="replace")
        split = frontmatter_lines(text)
        if not split:
            continue
        pre, fm_lines, post = split
        span = find_sources_block(fm_lines)
        if not span:
            continue
        start, end = span
        entries = split_entries(fm_lines[start + 1:end])
        if not entries:
            continue

        rel = os.path.relpath(note, vault)
        changes, new_entries = [], []
        for entry in entries:
            indent, recorded = entry_recorded_path(entry)
            if indent is None or recorded is None:
                new_entries.append(entry)
                continue
            project, filename = source_key(vault, note, recorded)
            if project != scope or filename not in targets:
                new_entries.append(entry)
                continue
            seen_files.add(filename)
            if entry_is_retired(entry):
                already += 1
                new_entries.append(entry)
                continue
            replacement = rewrite_entry(entry, indent, recorded, targets[filename])
            changes.append((entry, replacement))
            new_entries.append(replacement)

        if not changes:
            continue

        edited_notes += 1
        edited_entries += len(changes)
        print(f"{rel}")
        for old, new in changes:
            for line in old:
                print(f"  - {line.rstrip()}")
            for line in new:
                print(f"  + {line.rstrip()}")
        print()

        if args.apply:
            rebuilt = fm_lines[:start + 1] + [l for e in new_entries for l in e] + fm_lines[end:]
            note.write_text(pre + "".join(rebuilt) + post, encoding="utf-8")

    missing = sorted(set(targets) - seen_files)
    print("—" * 60)
    print(f"Notes to edit:     {edited_notes}")
    print(f"Entries to retire: {edited_entries}")
    if already:
        print(f"Already retired:   {already} (left untouched)")
    if missing:
        print(f"\nWARNING: {len(missing)} file(s) in {scope}/_sources are cited by NO note:")
        for name in missing:
            print(f"  - {name}")
        print("  Deleting these loses their content with no lineage recorded anywhere.")
        print("  Ingest them first, or move them out of the vault deliberately.")

    if args.apply:
        print("\nApplied. `_sources/` was NOT deleted — remove it manually when ready.")
    else:
        print("\nDry run — nothing written. Re-run with --apply to make these changes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
