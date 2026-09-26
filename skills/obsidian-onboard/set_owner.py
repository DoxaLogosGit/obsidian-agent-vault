#!/usr/bin/env python3
"""Add an `owner:` field to every note in a folder that does not have one yet.

A note without `owner:` counts as `owner: shared`, so ingest may edit it. Run
this on an existing vault to protect the notes you wrote before the skills
arrived.

Usage:
  set_owner.py <folder> [<folder> ...] --owner human            # dry run (default)
  set_owner.py <folder> [<folder> ...] --owner human --apply    # write the changes

Folders are vault-relative. Notes that already have `owner:` are never changed.
The script skips `index.md` and every `_`-prefixed directory, like the other
skills. It inserts one line at the top of the frontmatter and never reparses
the rest, so key order, quoting, and formatting survive. A note without
frontmatter gets a new block with only `owner:`.
"""
import argparse
import os
import re
import sys
from pathlib import Path

OWNERS = ("human", "agent", "shared")


def find_vault_root():
    """Walk up from this script to the vault root (AGENTS.md or CLAUDE.md + _meta/ marker)."""
    for parent in Path(__file__).resolve().parents:
        marker = (parent / "AGENTS.md").exists() or (parent / "CLAUDE.md").exists()
        if marker and (parent / "_meta").is_dir():
            return parent
    print("ERROR: could not locate vault root (no AGENTS.md or CLAUDE.md + _meta above this script)",
          file=sys.stderr)
    sys.exit(1)


def iter_notes(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(("_", ".")))
        for fn in sorted(filenames):
            if fn.endswith(".md") and fn != "index.md":
                yield Path(dirpath) / fn


def with_owner(text, owner):
    """Return text with `owner:` added, or None if the note already has one."""
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end >= 0:
            if re.search(r"(?m)^owner:", text[4:end + 1]):
                return None
            return f"---\nowner: {owner}\n" + text[4:]
    return f"---\nowner: {owner}\n---\n\n" + text


def main(argv=None, vault=None):
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("folders", nargs="+", metavar="FOLDER", help="vault-relative folders")
    ap.add_argument("--owner", required=True, choices=OWNERS)
    ap.add_argument("--apply", action="store_true", help="write the changes (default is a dry run)")
    args = ap.parse_args(argv)

    vault = (vault or find_vault_root()).resolve()
    roots = []
    for folder in args.folders:
        root = Path(folder)
        root = (root if root.is_absolute() else vault / root).resolve()
        if not root.is_dir() or not root.is_relative_to(vault):
            print(f"Not a folder inside the vault: {folder}", file=sys.stderr)
            return 1
        roots.append(root)

    changed = skipped = 0
    for root in roots:
        for note in iter_notes(root):
            text = note.read_text(encoding="utf-8", errors="replace")
            new = with_owner(text, args.owner)
            rel = note.relative_to(vault).as_posix()
            if new is None:
                skipped += 1
                continue
            changed += 1
            print(f"+ owner: {args.owner}   {rel}")
            if args.apply:
                note.write_text(new, encoding="utf-8")

    print("—" * 60)
    print(f"Notes to mark:          {changed}")
    print(f"Already have an owner:  {skipped} (left untouched)")
    print("Applied." if args.apply else "Dry run — nothing written. Re-run with --apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
