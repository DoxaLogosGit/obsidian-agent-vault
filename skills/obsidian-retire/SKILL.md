---
name: obsidian-retire
description: Retire and delete sources in _sources/ whose content already reached notes, so _sources/ does not grow without limit. Records each file's hash as retired in the citing notes, then deletes the file. Use when the user asks to clean up, purge, or retire sources or old exports, or types /obsidian-retire [file ...].
---

# obsidian-retire

Delete ingested sources without losing their lineage. `retire_sources.py` marks each citing `sources:` entry `retired: true` and keeps its hash. Then it deletes the file. A re-download of the same export is still recognized, because the hash stays in the notes.

Chat exports are the main target. Each Claude or ChatGPT export holds the full history, so older exports add nothing once they are ingested.

## Inputs

- Zero or more file names in the vault-root `_sources/`. Optional.

## 1. Choose the files

If the user named files, use them and go to step 2.

Otherwise, run the inventory from the vault root:

```bash
python3 .agents/skills/obsidian-ingest/extract_conversations.py inventory
```

From its `sources` list, a file is a candidate if `verdict` is `unchanged` and `notes` is not empty. Show one line per candidate: filename, `type`, size (from `ls -l`), and the number of citing notes. Then show each other file on one line with its verdict and the words "not eligible".

- If there are no candidates, say so and stop.
- Ask the user which candidates to retire. Offer "all candidates" as a choice. If there are more than four, number the list and ask for the numbers in text.

## 2. Dry run

```bash
python3 .agents/skills/obsidian-ingest/retire_sources.py _sources/<file> [_sources/<file> ...]
```

The dry run writes nothing. Report:

- the number of notes to edit and entries to retire
- every `WARNING` block, word for word

Do not paste the per-note diff unless the user asks for it.

## 3. Confirm, then apply

Ask the user to confirm that the files can be deleted. Name each file. After a yes:

```bash
python3 .agents/skills/obsidian-ingest/retire_sources.py _sources/<file> [...] --apply --delete
```

Report each `Deleted:` line, and each file the script kept with its reason.

## Rules

- Do not delete a file in `_sources/` yourself. The script is the only deleter. It keeps a file that no note cites, that changed after ingest, or that an `owner: human` note cites.
- If the script keeps a file, report the reason and stop. Do not retry with other flags.
- A stray per-project `<project>/_sources/` directory takes `--scope <project>` instead of file names. Read the anomaly rules in `obsidian-ingest` before you touch one.
- A retired entry is not a lint error. `obsidian-lint-light` counts it under "Retired sources".

## 4. Report

End with one line: the number of files deleted, and the space freed. Suggest `obsidian-lint-light` to confirm that the vault is clean.
