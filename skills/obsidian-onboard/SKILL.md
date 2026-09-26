---
name: obsidian-onboard
description: Set up an existing Obsidian vault for the vault skills after `install.py into`. Confirms which folders ingest may write to, protects the user's existing notes with owner frontmatter, and builds the first indexes. Use once after installing into an existing vault, or when the user types /obsidian-onboard.
---

# obsidian-onboard

Walk the user through the three things an existing vault needs before the first ingest: a folder config, owners on existing notes, and indexes. Ask before every write. Run all commands from the vault root.

## 1. Folders

Read `_meta/vault-config.yml`. List the top-level folders of the vault, and skip every folder whose name starts with `_` or `.`. For each folder, count its `.md` notes.

Show one line per folder: name, note count, and its current role:

- **writable** — in `writable_roots` and `audited_roots`. Ingest may create and update notes here.
- **read-only** — in `audited_roots` only. Lint audits and indexes it, and ingest never writes to it. Use this for journals, dashboards, and other folders the user keeps by hand.
- **ignored** — in neither list. No skill looks at it. Use this for attachments, templates, and exports.

Ask the user to confirm or change the role of each folder. Batch the questions, four folders per question. Then rewrite only the `writable_roots` and `audited_roots` lines of `_meta/vault-config.yml`. Keep its comments. Every writable folder must also be in `audited_roots`.

If a folder holds one note per subfolder, for example one folder per game or per person, offer to add it to `index_rollup_roots`. It then gets one index instead of many small ones.

## 2. Ownership

A note without an `owner:` field counts as `owner: shared`, so ingest may edit it. The user's existing notes were written by hand, so ask how to treat them. Offer:

- **Protect everything (recommended)** — mark every existing note `owner: human`. Ingest cites these notes and creates new ones beside them, but never changes them.
- **Choose per folder** — ask for each writable folder: `human` or `shared`.
- **Leave them shared** — ingest may update existing notes. Sections the user wrote are kept, but the text can change around them.

If the vault is a git repository, suggest that the user commits first, so the change is easy to undo. Do not commit yourself.

Run a dry run for the chosen folders:

```bash
python3 .agents/skills/obsidian-onboard/set_owner.py <folder> [<folder> ...] --owner human
```

Report the number of notes it will mark. After the user confirms, run it again with `--apply`. The script changes only notes without `owner:`, adds one line to the frontmatter, and leaves the rest of each note as it was.

Never write `owner:` into notes by hand. The script is the only writer for this step.

## 3. Indexes and audit

```bash
python3 .agents/skills/obsidian-lint-light/lint.py --rebuild-indexes
```

This writes an `index.md` in each audited folder that holds notes, and the master index `_meta/index.md`. Then summarize the lint report in `_meta/`: the issue count and the top issue types. An existing vault often has broken links. Report them, and do not fix them. The user decides.

## 4. Next steps

Tell the user:

- Put a source in `_sources/`, or run `/obsidian-fetch-export` or `/obsidian-youtube <url>`.
- Run `/obsidian-ingest`.
- Rules the user wants to add go in `AGENTS.md`, outside the `obsidian-agent-vault` markers. `install.py update` replaces the text between the markers.
