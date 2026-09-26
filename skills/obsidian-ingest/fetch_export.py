#!/usr/bin/env python3
"""Place Claude data-export zips from ~/Downloads into the vault-root _sources/.

Anthropic changed the export format on 2026-09-04. An export no longer produces
one zip. It produces a small manifest JSON that lists one single-use download URL
per category (light_metadata, projects, memories, conversations). The
conversations and projects categories feed this vault.

The download itself must happen in a browser. Each URL is
`claude.ai/export/<org>/download/<id>` with no token in it, so it only works for a
signed-in claude.ai session. A plain request gets HTTP 403. Tested 2026-09-14:
opening the URL in signed-in Chrome downloaded the zip, and a 403 from a plain
request did not use up the link.

Usage:
    python3 fetch_export.py [--dry-run] [--category conversations projects]
                            [--manifest PATH] [--manifest-dir DIR]
                            [--local-zip PATH]

Default behavior: pick the newest manifest-*.json in ~/Downloads by its internal
`created_at` field (not mtime). For each category, print its URL if the zip is not
yet downloaded. If ~/Downloads holds the category's file, newer than the manifest,
compare it and place it in _sources/.

Every export lands on the same filename, so a collision is the normal case. The
script copies the download to a temporary file, then compares it against *every* export
already in _sources/ — not just the target name — and takes one of four paths:

  - matches any existing file  — discard the download, keep what is there
  - matches a hash in a note   — discard it; the source was ingested and retired
  - target name is free        — move it into place
  - target name is taken       — move it in under a date-stamped name

Comparing the whole directory matters three times over: a re-download of an older
export matches a date-stamped file rather than the base name, a rename on
Anthropic's side would move the target away from everything already stored, and a
source that was retired and deleted leaves no bytes to compare at all. The last
case is why note frontmatter is read too — `retire_sources.py` keeps the sha256 in
the `sources:` entries of every note the export fed, so the hash outlives the file.

Comparison uses `export_content_bytes` from extract_conversations.py, which hashes
the inner conversations.json rather than the zip packaging. Outer zip bytes vary
between downloads even when the conversations are identical. For a category whose
zip holds no valid Claude export (memories, projects, light_metadata), that helper
falls back to the outer bytes, so those almost always compare as different and get
a date-stamped name. Bumping is the safe outcome.

Create-only contract (AGENTS.md): the script only ever creates a new path. It never
modifies, renames, or deletes a file already in _sources/. Overwriting would change
the hash of a file that a note may already cite, which is the drift the ingest
verdict system exists to prevent.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from extract_conversations import export_content_bytes  # noqa: E402

CATEGORIES = ("conversations", "projects", "memories", "light_metadata")
DEFAULT_CATEGORIES = ("conversations", "projects")


def find_vault_root(start: Path) -> Path:
    """Walk up until an AGENTS.md or CLAUDE.md sits next to a _meta/ directory."""
    for d in [start, *start.parents]:
        marker = (d / "AGENTS.md").is_file() or (d / "CLAUDE.md").is_file()
        if marker and (d / "_meta").is_dir():
            return d
    print("ERROR: vault root not found (no AGENTS.md or CLAUDE.md next to _meta/).", file=sys.stderr)
    sys.exit(2)


def parse_created_at(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return datetime.min


def newest_manifest(directory: Path) -> Path:
    """Return the manifest with the latest internal created_at.

    File mtime is not the authority. Two manifests downloaded minutes apart can
    land in either mtime order, and picking the wrong one wastes a single-use URL.
    """
    candidates = sorted(directory.glob("manifest-*.json"))
    if not candidates:
        print(f"ERROR: no manifest-*.json found in {directory}", file=sys.stderr)
        print("Request an export, then download the manifest.", file=sys.stderr)
        sys.exit(1)

    dated: list[tuple[datetime, Path]] = []
    for path in candidates:
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        dated.append((parse_created_at(data.get("created_at", "")), path))

    if not dated:
        print(f"ERROR: no readable manifest in {directory}", file=sys.stderr)
        sys.exit(1)

    dated.sort(key=lambda pair: pair[0])
    return dated[-1][1]


def read_manifest(manifest: Path) -> dict:
    try:
        return json.loads(manifest.read_text())
    except (json.JSONDecodeError, OSError) as err:
        print(f"ERROR: cannot read {manifest}: {err}", file=sys.stderr)
        sys.exit(1)


def read_entry(manifest: Path, data: dict, category: str) -> dict:
    files = data.get("data_files")
    if not isinstance(files, list) or not files:
        print(f"ERROR: {manifest.name} has no data_files list.", file=sys.stderr)
        print("The export format changed again. Inspect the file by hand.", file=sys.stderr)
        sys.exit(1)

    for entry in files:
        if entry.get("category") == category:
            if not entry.get("export_url") or not entry.get("filename"):
                print(f"ERROR: the {category} entry has no export_url or filename.",
                      file=sys.stderr)
                sys.exit(1)
            return entry

    available = ", ".join(sorted({f.get("category", "?") for f in files}))
    print(f"ERROR: no '{category}' entry in {manifest.name}.", file=sys.stderr)
    print(f"Available categories: {available}", file=sys.stderr)
    sys.exit(1)


def content_hash(path: Path) -> str:
    """SHA256 of the authoritative content, matching the ingest inventory."""
    return hashlib.sha256(export_content_bytes(path)).hexdigest()


def existing_hashes(sources: Path) -> dict[str, Path]:
    """Map content hash to file for every export candidate already in _sources/.

    The whole directory is scanned, not just the target filename. Two reasons:
    a re-download of an older export matches a date-stamped file rather than the
    base name, and Anthropic can rename the file, which would move the target
    away from everything already stored. Only .zip and .json can hold an export,
    so the scan skips the rest.
    """
    seen: dict[str, Path] = {}
    for path in sorted(sources.iterdir()):
        if not path.is_file() or path.suffix.lower() not in (".zip", ".json"):
            continue
        try:
            seen.setdefault(content_hash(path), path)
        except (OSError, ValueError):
            continue
    return seen


def recorded_hashes(vault: Path) -> dict[str, str]:
    """Map every sha256 recorded in note frontmatter to a short description.

    A file that has been retired and deleted leaves no bytes in _sources/, but its
    hash survives in the `sources:` entries of the notes it fed. Checking those
    catches a re-download of an export that was already ingested and cleaned up —
    the case the directory scan alone cannot see.

    Returns an empty map if PyYAML is missing. The directory scan still runs, so a
    missing dependency degrades the check rather than blocking the fetch.
    """
    try:
        import extract_conversations as ec
        import yaml  # noqa: F401
        ec.yaml = yaml
    except ImportError:
        print("WARNING: PyYAML missing — skipping the recorded-hash check.",
              file=sys.stderr)
        print("         Install with: pip install pyyaml", file=sys.stderr)
        return {}

    found: dict[str, str] = {}
    for note in ec.iter_notes(vault):
        for item in ec.parse_frontmatter(note).get("sources") or []:
            if not isinstance(item, dict) or not item.get("sha256"):
                continue
            state = "retired" if item.get("retired") else "recorded"
            label = f"{item.get('path', '?')} ({state}, cited by {note.name})"
            found.setdefault(str(item["sha256"]), label)
    return found


def stamped_path(target: Path, created_at: datetime) -> Path:
    """Return a free date-stamped path beside target.

    The date comes from the manifest, so the name says which export produced the
    file. Do not bump the numeric suffix instead: `conversations-000.zip` carries
    Anthropic's `part` number, so `-001` is a real part 1 of a multi-part export.
    """
    date = created_at.strftime("%Y-%m-%d") if created_at != datetime.min else "undated"
    candidate = target.with_name(f"{target.stem}-{date}{target.suffix}")
    n = 2
    while candidate.exists():
        candidate = target.with_name(f"{target.stem}-{date}-{n}{target.suffix}")
        n += 1
    return candidate


def downloaded_copy(directory: Path, filename: str, manifest: Path) -> Path | None:
    """Return the browser-downloaded file for this category, if it is newer than the manifest.

    A browser saves a second download of the same name as `name (1).zip`, so the
    newest match of the stem wins. A file older than the manifest belongs to an
    earlier export and is ignored.
    """
    stem, suffix = Path(filename).stem, Path(filename).suffix
    candidates = [p for p in directory.glob(f"{stem}*{suffix}")
                  if p.is_file() and p.stat().st_mtime >= manifest.stat().st_mtime]
    return max(candidates, key=lambda p: p.stat().st_mtime) if candidates else None


def place(tmp: Path, target: Path, created_at: datetime, vault: Path,
          before: dict[str, Path], recorded: dict[str, str]) -> None:
    """Move the downloaded file into _sources/, or discard it if it duplicates.

    Four outcomes, reported plainly: discarded because a file already present holds
    the same content, discarded because a note records the same hash (the source was
    ingested and retired), placed at the target name, or placed under a date-stamped
    name because the target name is taken by different content.
    """
    new_hash = content_hash(tmp)

    duplicate = before.get(new_hash)
    if duplicate is not None:
        tmp.unlink()
        print(f"Identical to {duplicate.relative_to(vault)} — nothing added.")
        print("You already have this export.")
        print(f"sha256: {new_hash}")
        return

    cited = recorded.get(new_hash)
    if cited is not None:
        tmp.unlink()
        print("Already ingested — nothing added.")
        print(f"No file in _sources/, but a note records this hash: {cited}")
        print("The content reached your notes and the source was retired.")
        print(f"sha256: {new_hash}")
        return

    if not target.exists():
        tmp.replace(target)
        print(f"Wrote {target.relative_to(vault)} ({target.stat().st_size:,} bytes)")
        print("\nNext: run /obsidian-ingest")
        return

    stamped = stamped_path(target, created_at)
    tmp.replace(stamped)
    print(f"{target.relative_to(vault)} is taken by different content.")
    print(f"Wrote {stamped.relative_to(vault)} ({stamped.stat().st_size:,} bytes)")
    print("\nNext: run /obsidian-ingest")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch a category zip from a Claude data-export manifest into _sources/.")
    parser.add_argument("--dry-run", action="store_true",
                        help="resolve the manifest and target, print them, download nothing")
    parser.add_argument("--category", nargs="+", default=list(DEFAULT_CATEGORIES),
                        choices=CATEGORIES,
                        help="export categories to place (default: conversations projects)")
    parser.add_argument("--manifest", type=Path,
                        help="use this manifest instead of the newest one")
    parser.add_argument("--manifest-dir", type=Path, default=Path.home() / "Downloads",
                        help="where to look for manifests (default: ~/Downloads)")
    parser.add_argument("--local-zip", type=Path,
                        help="place this file (one category only) instead of looking in "
                             "--manifest-dir")
    args = parser.parse_args()

    vault = find_vault_root(Path(__file__).resolve().parent)
    sources = vault / "_sources"
    if not sources.is_dir():
        print(f"ERROR: {sources} does not exist.", file=sys.stderr)
        sys.exit(1)

    if args.manifest:
        manifest = args.manifest
        if not manifest.is_file():
            print(f"ERROR: {manifest} not found.", file=sys.stderr)
            sys.exit(1)
    else:
        manifest = newest_manifest(args.manifest_dir)

    data = read_manifest(manifest)
    created_at = parse_created_at(data.get("created_at", ""))
    if args.local_zip and len(args.category) != 1:
        print("ERROR: --local-zip needs exactly one --category.", file=sys.stderr)
        sys.exit(1)

    print(f"Manifest:  {manifest.name}")
    before = existing_hashes(sources)
    recorded = recorded_hashes(vault)
    print(f"Compare:   {len(before)} file(s) in _sources/, "
          f"{len(recorded)} hash(es) recorded in notes")
    sys.stdout.flush()

    missing = False
    for category in args.category:
        entry = read_entry(manifest, data, category)
        target = sources / entry["filename"]
        local = args.local_zip or downloaded_copy(args.manifest_dir, entry["filename"], manifest)
        print(f"\n[{category}] target {target.relative_to(vault)}"
              + ("  (taken — different content gets a date-stamped name)" if target.exists() else ""))
        if local is None or not Path(local).is_file():
            missing = True
            print(f"  Not downloaded yet. Open this link in a browser signed in to claude.ai:")
            print(f"  {entry['export_url']}")
            print("  Each link works once. Then run this script again.")
            continue
        print(f"  Found {local}")
        if args.dry_run:
            print("  Dry run — nothing placed.")
            continue
        tmp = target.with_suffix(target.suffix + ".part")
        if tmp.exists():
            print(f"ERROR: {tmp.relative_to(vault)} already exists — a dead partial copy.",
                  file=sys.stderr)
            print("Delete it, then run this again.", file=sys.stderr)
            sys.exit(1)
        shutil.copyfile(local, tmp)
        place(tmp, target, created_at, vault, before, recorded)
        before = existing_hashes(sources)
    sys.stdout.flush()
    if missing:
        sys.exit(3)


if __name__ == "__main__":
    main()
