#!/usr/bin/env python3
"""Retire ingested source files so they can be deleted without losing lineage.

The vault treats `sources:` as append-only history, so simply deleting a source
file turns every citing entry into a lint "stale source" forever. Retiring marks
each entry `retired: true` and — critically — records the file's sha256 *before*
it is deleted, so the lineage record stays complete. `obsidian-lint-light` skips
the stale and drift checks for retired entries and reports them under their own
count, so the deletion stays visible rather than silently disappearing.

Usage:
  retire_sources.py _sources/<file> [...]                    # dry run (default)
  retire_sources.py _sources/<file> [...] --apply            # write the changes
  retire_sources.py _sources/<file> [...] --apply --delete   # ...then delete the files
  retire_sources.py --scope "<project-path>" [--apply]       # a stray <project>/_sources/

File mode is the normal case: it retires named files in the vault-root
`_sources/`, for example a ChatGPT export after its conversations reached notes.
`--scope` retires every file in a stray per-project `_sources/` directory.

Dry run prints the exact per-note frontmatter change. Nothing is written without
--apply. `--delete` removes a file only when a note cites it, every citing entry
is now retired, and the file has not changed since ingest.

Idempotent: entries already carrying `retired:` are left untouched, so a partial
or repeated run is safe.

Notes:
  - The hash is `export_content_bytes`, the same one inventory and
    fetch_export.py use. For an export zip that is the inner content, so a
    re-download of a retired export is still recognized as already ingested.
  - An entry that already records a sha256 keeps it: it is the hash of what the
    note actually holds. A current file that differs from it is reported.
  - A note with `owner: human` is never written. Its change is printed, and its
    sources are not deleted.
  - Only the `sources:` block is rewritten, line by line. The rest of the
    frontmatter is never reparsed or reserialized, so key order, quoting, and
    formatting of hand-authored notes survive intact.
  - An entry is only retired when it resolves to a target file, so a source in
    another directory that happens to share a basename is never touched.
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
from extract_conversations import (  # noqa: E402
    export_content_bytes, find_vault_root, iter_notes, source_key)

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


def entry_recorded_sha(entry) -> str | None:
    for line in entry:
        m = re.match(r"^\s*sha256:\s*(\S+)", line)
        if m:
            return unquote(m.group(1))
    return None


def is_human_owned(fm_lines) -> bool:
    return any(re.match(r"^owner:\s*[\"']?human[\"']?\s*$", line) for line in fm_lines)


def content_sha(path: Path) -> str:
    return hashlib.sha256(export_content_bytes(path)).hexdigest()


def collect_targets(vault: Path, files, scope):
    """Return ({(project, filename): (path, sha)}, label), or (None, error message)."""
    if scope is not None:
        scope = scope.strip("/")
        src_dir = vault / scope / "_sources"
        if not src_dir.is_dir():
            return None, f"No _sources/ directory at {scope}/_sources"
        paths = [p for p in sorted(src_dir.glob("*")) if p.is_file() and not p.name.startswith(".")]
        return {(scope, p.name): (p, content_sha(p)) for p in paths}, f"{scope}/_sources"
    root_src = (vault / "_sources").resolve()
    targets = {}
    for f in files:
        p = Path(f)
        p = (p if p.is_absolute() else vault / p).resolve()
        if p.parent != root_src or not p.is_file():
            return None, f"Not a file in the vault-root _sources/: {f}"
        targets[("", p.name)] = (p, content_sha(p))
    return targets, f"{len(targets)} file(s) in _sources/"


def main(argv=None, vault: Path | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("files", nargs="*", metavar="FILE",
                    help="source files in the vault-root _sources/ to retire")
    ap.add_argument("--scope", metavar="PROJECT_PATH",
                    help="retire every file in a stray <project>/_sources/ instead")
    ap.add_argument("--apply", action="store_true",
                    help="write the changes (default is a dry run)")
    ap.add_argument("--delete", action="store_true",
                    help="with --apply, delete each file whose citing entries are all retired")
    args = ap.parse_args(argv)
    if bool(args.files) == bool(args.scope):
        ap.error("give either one or more FILEs or --scope, not both")
    if args.delete and not args.apply:
        ap.error("--delete needs --apply")

    vault = vault or find_vault_root()
    targets, label = collect_targets(vault, args.files, args.scope)
    if targets is None:
        print(label, file=sys.stderr)
        return 1
    if not targets:
        print(f"{label} is empty — nothing to retire; safe to rmdir.")
        return 0

    print(f"Target: {label}  ({len(targets)} file(s))\n")

    edited_notes = 0
    edited_entries = 0
    already = 0
    cited = set()     # target keys some note cites
    blocked = set()   # target keys with a citing entry left unretired
    changed = {}      # target key -> recorded sha that differs from the file

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
        human = is_human_owned(fm_lines)
        changes, new_entries = [], []
        for entry in entries:
            indent, recorded = entry_recorded_path(entry)
            if indent is None or recorded is None:
                new_entries.append(entry)
                continue
            key = source_key(vault, note, recorded)
            if key not in targets:
                new_entries.append(entry)
                continue
            cited.add(key)
            if entry_is_retired(entry):
                already += 1
                new_entries.append(entry)
                continue
            current = targets[key][1]
            recorded_sha = entry_recorded_sha(entry)
            if recorded_sha and recorded_sha != current:
                changed[key] = recorded_sha
            replacement = rewrite_entry(entry, indent, recorded, recorded_sha or current)
            changes.append((entry, replacement))
            new_entries.append(replacement)
            if human:
                blocked.add(key)

        if not changes:
            continue

        edited_notes += 1
        edited_entries += len(changes)
        print(f"{rel}" + ("  (owner: human — NOT written, change shown only)" if human else ""))
        for old, new in changes:
            for line in old:
                print(f"  - {line.rstrip()}")
            for line in new:
                print(f"  + {line.rstrip()}")
        print()

        if args.apply and not human:
            rebuilt = fm_lines[:start + 1] + [l for e in new_entries for l in e] + fm_lines[end:]
            note.write_text(pre + "".join(rebuilt) + post, encoding="utf-8")

    missing = sorted(key[1] for key in targets if key not in cited)
    print("—" * 60)
    print(f"Notes to edit:     {edited_notes}")
    print(f"Entries to retire: {edited_entries}")
    if already:
        print(f"Already retired:   {already} (left untouched)")
    if blocked:
        print(f"\nWARNING: {len(blocked)} file(s) are cited by an owner: human note, which was not written:")
        for key in sorted(blocked):
            print(f"  - {key[1]}")
        print("  Retire those entries by hand, or leave the files in place.")
    if changed:
        print(f"\nWARNING: {len(changed)} file(s) changed after ingest:")
        for key in sorted(changed):
            print(f"  - {key[1]}")
        print("  The recorded hash is kept. Deleting the file loses content that never reached a note.")
        print("  Ingest it again first.")
    if missing:
        print(f"\nWARNING: {len(missing)} file(s) in {label} are cited by NO note:")
        for name in missing:
            print(f"  - {name}")
        print("  Deleting these loses their content with no lineage recorded anywhere.")
        print("  Ingest them first, or move them out of the vault deliberately.")

    if args.apply and args.delete:
        deletable = [k for k in targets if k in cited and k not in blocked and k not in changed]
        for key in sorted(deletable):
            path = targets[key][0]
            path.unlink()
            print(f"Deleted: {os.path.relpath(path, vault)}")
        kept = len(targets) - len(deletable)
        if kept:
            print(f"Kept {kept} file(s) because of the warnings above.")
    elif args.apply:
        print("\nApplied. No file was deleted — re-run with --delete, or remove them manually.")
    else:
        print("\nDry run — nothing written. Re-run with --apply to make these changes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
