#!/usr/bin/env python3
"""Fetch YouTube transcripts into the vault-root _sources/ as Markdown files.

Ingest reads only files that already sit in _sources/. A YouTube link is not a
file, so this script turns one into a file: metadata plus a cleaned transcript.
The normal ingest then treats it as a `document` source. Ingest itself does not
change.

Usage:
    python3 fetch_youtube.py URL [URL ...] [--lang en] [--dry-run]

The script uses yt-dlp and downloads captions only, never video. It prefers
human-written subtitles over auto-captions. If a video has neither in the
requested language, the script reports it and writes nothing. Local
speech-to-text is not supported.

Output: _sources/youtube-<upload date>-<slug>-<video id>.md

The file content is deterministic (no fetch timestamp), so a re-fetch of an
unchanged video produces identical bytes.

Create-only contract (AGENTS.md): the script only ever creates a new path. It
skips a video when any file in _sources/ already carries its id, or when any
note cites such a file. The second check covers a source that was retired and
deleted, whose filename survives only in a note's `sources:` list.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

PARAGRAPH_SECONDS = 60


def find_vault_root(start: Path) -> Path:
    """Walk up until an AGENTS.md or CLAUDE.md sits next to a _meta/ directory."""
    for d in [start, *start.parents]:
        marker = (d / "AGENTS.md").is_file() or (d / "CLAUDE.md").is_file()
        if marker and (d / "_meta").is_dir():
            return d
    print("ERROR: vault root not found (no AGENTS.md or CLAUDE.md next to _meta/).", file=sys.stderr)
    sys.exit(2)


def yt_dlp(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["yt-dlp", "--no-warnings", "--no-playlist", *args],
        capture_output=True, text=True,
    )


def fetch_info(url: str) -> dict:
    r = yt_dlp("-J", "--skip-download", url)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "yt-dlp failed")
    return json.loads(r.stdout)


def pick_track(info: dict, lang: str) -> tuple[str, str] | None:
    """Return (kind, language key). Manual subtitles win over auto-captions."""
    def match(tracks: dict) -> str | None:
        keys = list(tracks)
        for k in (lang, f"{lang}-orig"):
            if k in keys:
                return k
        for k in keys:
            if k.split("-")[0] == lang:
                return k
        return None

    manual = match(info.get("subtitles") or {})
    if manual:
        return "manual", manual
    auto = match(info.get("automatic_captions") or {})
    if auto:
        return "auto", auto
    return None


def download_json3(url: str, kind: str, key: str) -> dict:
    flag = "--write-subs" if kind == "manual" else "--write-auto-subs"
    with tempfile.TemporaryDirectory() as tmp:
        r = yt_dlp("--skip-download", flag, "--sub-langs", key, "--sub-format", "json3",
                   "-o", f"{tmp}/sub.%(ext)s", url)
        files = list(Path(tmp).glob("sub*.json3"))
        if not files:
            raise RuntimeError(r.stderr.strip() or "caption download produced no file")
        return json.loads(files[0].read_text(encoding="utf-8"))


def transcript_paragraphs(sub: dict) -> list[tuple[int, str]]:
    """Group caption events into (start seconds, text) paragraphs of about a minute."""
    paras: list[tuple[int, list[str]]] = []
    for ev in sub.get("events", []):
        segs = ev.get("segs")
        if not segs:
            continue
        text = "".join(s.get("utf8", "") for s in segs).replace("\n", " ").strip()
        if not text:
            continue
        start = int(ev.get("tStartMs", 0) / 1000)
        if not paras or start - paras[-1][0] >= PARAGRAPH_SECONDS:
            paras.append((start, []))
        paras[-1][1].append(text)
    return [(t, re.sub(r"\s+", " ", " ".join(words)).strip()) for t, words in paras]


def stamp(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def slugify(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return s[:60].rstrip("-") or "video"


def yaml_str(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def render(info: dict, kind: str, key: str, paras: list[tuple[int, str]]) -> str:
    vid = info["id"]
    url = f"https://www.youtube.com/watch?v={vid}"
    upload = info.get("upload_date") or ""
    upload_iso = f"{upload[:4]}-{upload[4:6]}-{upload[6:]}" if len(upload) == 8 else ""
    channel = info.get("channel") or info.get("uploader") or ""
    lines = [
        "---",
        "source_type: youtube",
        f"video_id: {vid}",
        f"url: {url}",
        f"title: {yaml_str(info.get('title', ''))}",
        f"channel: {yaml_str(channel)}",
        f"upload_date: {upload_iso}",
        f"duration: {stamp(int(info.get('duration') or 0))}",
        f"captions: {kind}",
        f"caption_language: {key}",
        "---",
        "",
        f"# {info.get('title', vid)}",
        "",
        f"[{channel}]({url}) · {upload_iso} · {stamp(int(info.get('duration') or 0))}",
        "",
    ]
    desc = (info.get("description") or "").strip()
    if desc:
        lines += ["## Description", "", desc, ""]
    lines += ["## Transcript", ""]
    if kind == "auto":
        lines += ["> Auto-generated captions. Expect misheard words and no punctuation.", ""]
    for t, text in paras:
        lines += [f"[{stamp(t)}]({url}&t={t}s) {text}", ""]
    return "\n".join(lines).rstrip() + "\n"


def already_known(vault: Path, src: Path, vid: str) -> str | None:
    """Return the reason a video is already known, or None."""
    for f in src.glob(f"youtube-*-{vid}.md"):
        return f"already in _sources/ as {f.name}"
    needle = f"-{vid}.md"
    for note in vault.rglob("*.md"):
        rel = note.relative_to(vault)
        if any(p.startswith((".", "_")) for p in rel.parts[:-1]):
            continue
        try:
            if needle in note.read_text(encoding="utf-8", errors="ignore"):
                return f"cited by {rel} (source retired or moved)"
        except OSError:
            continue
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("urls", nargs="+")
    ap.add_argument("--lang", default="en", help="caption language (default: en)")
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    args = ap.parse_args()

    vault = find_vault_root(Path(__file__).resolve().parent)
    src = vault / "_sources"
    if not src.is_dir():
        print(f"ERROR: {src} does not exist.", file=sys.stderr)
        return 2

    failures = 0
    for url in args.urls:
        try:
            info = fetch_info(url)
        except Exception as e:  # noqa: BLE001
            print(f"FAIL  {url}: {e}")
            failures += 1
            continue
        vid = info["id"]
        reason = already_known(vault, src, vid)
        if reason:
            print(f"SKIP  {vid}: {reason}")
            continue
        track = pick_track(info, args.lang)
        if not track:
            print(f"FAIL  {vid}: no '{args.lang}' subtitles or auto-captions")
            failures += 1
            continue
        kind, key = track
        upload = info.get("upload_date") or datetime.now().strftime("%Y%m%d")
        name = f"youtube-{upload[:4]}-{upload[4:6]}-{upload[6:]}-{slugify(info.get('title', ''))}-{vid}.md"
        target = src / name
        if args.dry_run:
            print(f"DRY   {vid}: would write _sources/{name} ({kind} captions, {key})")
            continue
        try:
            paras = transcript_paragraphs(download_json3(url, kind, key))
        except Exception as e:  # noqa: BLE001
            print(f"FAIL  {vid}: {e}")
            failures += 1
            continue
        if not paras:
            print(f"FAIL  {vid}: caption track is empty")
            failures += 1
            continue
        if target.exists():  # create-only, even against a race
            print(f"SKIP  {vid}: _sources/{name} already exists")
            continue
        target.write_text(render(info, kind, key, paras), encoding="utf-8")
        print(f"OK    {vid}: wrote _sources/{name} ({kind} captions, {len(paras)} paragraphs)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
