---
name: obsidian-files-backlog
description: Backfill chat files into Claude conversations that were ingested before file support existed. Lists the conversations whose attached or created files never reached a note, lets the user pick, and writes only those files. Use when the user asks to backfill chat files, or types /obsidian-files-backlog.
---

# obsidian-files-backlog

Add the files from already-ingested conversations to their notes. This command is the same as `obsidian-ingest --files-backlog`.

Invoke the `obsidian-ingest` skill with the argument `--files-backlog`, and follow its "Files backlog short-circuit" section. If your tool cannot pass an argument to a skill, read `.agents/skills/obsidian-ingest/SKILL.md` and follow that section directly.

The backfill does not ingest new sources and does not update `_meta/last-ingest.md`.
