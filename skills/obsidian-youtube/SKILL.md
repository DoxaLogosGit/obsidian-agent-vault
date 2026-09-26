---
name: obsidian-youtube
description: Ingest one or more YouTube video links into the Obsidian vault. Fetches each video's captions into a transcript file in _sources/ with fetch_youtube.py, then runs obsidian-ingest to route and write notes. Use when the user gives YouTube URLs to ingest, or types /obsidian-youtube <url> [...].
---

# obsidian-youtube

Turn YouTube links into vault notes. The skill has two stages: fetch, then ingest.

## Inputs

- One or more YouTube URLs (`youtube.com/watch?v=`, `youtu.be/`, `/shorts/`). Required.
- `--lang <code>` *(optional)*: caption language. Default: `en`.
- `--fetch-only` *(optional)*: stop after stage 1. Do not ingest.

If the user gives no URL, ask for one and stop.

## Stage 1 — Fetch

Run the fetcher from the vault root, with every URL in one call:

```bash
python3 .agents/skills/obsidian-ingest/fetch_youtube.py <url> [<url> ...] [--lang <code>]
```

The script prints one line per video:

| Prefix | Meaning | Action |
|---|---|---|
| `OK` | A transcript file was written to `_sources/` | Continue to stage 2 |
| `SKIP` | The video is already in `_sources/` or already cited by a note | Tell the user. Ingest still picks it up if it is not yet ingested. |
| `FAIL` | No captions in that language, or yt-dlp failed | Report the line. Do not retry. There is no speech-to-text fallback. |

Do not write or edit anything in `_sources/` yourself. The script is the only writer, and it is create-only.

If every line is `FAIL`, stop and report. If `--fetch-only` was given, report the lines and stop.

## Stage 2 — Ingest

Invoke the `obsidian-ingest` skill with no arguments, and follow it in full. The transcripts arrive as `type: document`, `verdict: new`. Ingest routes them like any other document.

Rules for transcript sources, on top of the ingest rules:

- Summarize the claims, decisions, and facts the user cares about. Never paste the transcript into a note.
- Cite a specific claim with the timestamp link from the transcript, for example `[12:34](https://www.youtube.com/watch?v=<id>&t=754s)`.
- If the file says `captions: auto`, the captions may have misheard words. Mark a claim `^[ambiguous]` if it depends on a word the captions may have gotten wrong.
- Ingest also processes any other pending sources in `_sources/`. Tell the user if it found more than the videos.

## Report

After ingest prints its summary, add one line per video: `<title> → <note path>`, or the `FAIL`/`SKIP` reason.
