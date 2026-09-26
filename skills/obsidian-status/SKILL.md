---
name: obsidian-status
description: Show when the vault was last ingested. Reads _meta/last-ingest.md only, with no source reads and no writes. Use when the user asks when they last ingested, or types /obsidian-status.
---

# obsidian-status

Print the time of the last ingest. This command is the same as `obsidian-ingest --status`.

Invoke the `obsidian-ingest` skill with the argument `--status`, and follow its "Status short-circuit" section. If your tool cannot pass an argument to a skill, read `.agents/skills/obsidian-ingest/SKILL.md` and follow that section directly.

Do nothing else. Do not scan the inventory, read `_sources/`, or write any file.
