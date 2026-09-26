---
name: obsidian-fetch-export
description: Move a Claude data export from ~/Downloads into _sources/, skipping content that is already there or already ingested. The download itself needs the user's signed-in browser, so the skill prints the links, waits for the user, then places the zips. Use when the user has a new Claude export to bring in, or types /obsidian-fetch-export.
---

# obsidian-fetch-export

Bring a new Claude data export into the vault with `fetch_export.py`.

## How a Claude export works

1. The user requests an export in Claude (Settings → Privacy → Export data).
2. Claude sends a link to a small manifest file, `manifest-*.json`. The user downloads it to `~/Downloads`.
3. The manifest holds one download link per category. This vault uses `conversations` and `projects`.
4. Each link works only in a browser that is signed in to claude.ai, and only once.

The agent cannot download the zips. A request from a script gets HTTP 403. The user opens each link in their browser. The script does the rest.

## Inputs

Pass these through to the script when the user gives them:

- `--category <name> [...]`: default `conversations projects`. Also accepts `memories` and `light_metadata`.
- `--manifest <path>`: use this manifest instead of the newest one in `~/Downloads`.
- `--manifest-dir <dir>`: look for manifests and downloads in another folder.
- `--dry-run`: show what the script would do, and place nothing.

## 1. Run the script

From the vault root:

```bash
python3 .agents/skills/obsidian-ingest/fetch_export.py [flags]
```

- If it prints `no manifest-*.json found`, tell the user to request an export and download the manifest to `~/Downloads`. Stop.
- If it exits with code 3, some categories are not downloaded yet. Go to step 2.
- Otherwise, go to step 3.

## 2. Hand the links to the user

For each category that prints `Not downloaded yet`, show the user its link. Tell the user:

- Open each link in a browser that is signed in to claude.ai.
- Each link works once.
- Say when the downloads are finished.

Do not open the links yourself with `curl`, a web fetch, or browser automation, unless the user asks you to use their browser. When the user says the downloads are finished, run the script again.

## 3. Report the result

The script reports one result per category:

| Output | Meaning |
|---|---|
| `Wrote _sources/<file>` | A new export is in place. |
| `Identical to ...` | This export is already in `_sources/`. Nothing added. |
| `Already ingested` | A note records this hash. The export was ingested and retired. Nothing added. |
| `... is taken by different content` | The export went in under a date-stamped name. |

If the script wrote at least one file, offer to run `obsidian-ingest`.

## Rules

- Do not copy, move, or delete files in `_sources/` yourself. The script is the only writer, and it never overwrites a file.
- If the script reports a leftover `.part` file, show the message to the user and stop. Do not delete it.
