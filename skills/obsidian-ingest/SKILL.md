---
name: obsidian-ingest
description: Universal entry point for ingesting any source file from the vault-root _sources/ folder into derived Obsidian notes. Auto-detects Claude export files — raw .json or the .zip archive as downloaded (read transparently, no manual extraction) — and routes to the appropriate workflow. Handles .md, .html, .pdf, and other documents with SHA256 change tracking. Routes conversations and documents across all projects, prompting interactively for anything unmatched. One command for all ingest operations.
---

# obsidian-ingest

Universal entry point for ingesting source files from the **vault-root `_sources/`** folder into derived notes. Auto-detects file types and routes accordingly. Never requires a second command.

**One mode, one source directory.** Everything is dropped in `_sources/` at the vault root; routing spans the whole vault and unmatched items are routed interactively. There is no project-scoped variant and no project path argument.

> **`_sources/` at the vault root is the single source of truth.** Per-project `<project>/_sources/` directories are **not** a thing: never scanned, never created, never ingested from. If one appears, it is an anomaly, and `obsidian-lint-light` will surface its contents as orphan sources.
>
> **Two kinds of anomaly. Tell them apart before you touch anything.** Check whether the notes that cite these files record them as `retired: true`.
>
> - **Retired — a restore or a sync brought back deleted files.** Do not move the files, and do not ingest them. Retirement means the content already reached the notes and the hashes are kept as lineage. A move makes the inventory read them as `verdict: new` and re-ingest what the notes already hold. Report the directory to the user and stop.
> - **Not retired — genuinely uningested files.** Move them to the vault-root `_sources/` and ingest from there. Delete the directory afterward, retiring the entries first with `retire_sources.py --scope <project>`.
>
> If some files in the directory are retired and some are not, treat each file on its own terms. Do not apply one verdict to the whole directory.

## Inputs

- **`--status` flag** *(optional)*: report-only mode. When present, the command prints the timestamp of the last ingest run and does nothing else — no source reads, no inventory scan, no writes.

- **`--files-backlog` flag** *(optional)*: backfill mode. Lists already-ingested conversations whose files never reached a note, lets the human pick, and writes only those files. It runs `obsidian-claude-export-ingest` §3b and nothing else — no inventory scan, no normal ingest.

There are no other arguments. Any path supplied is ignored; the source directory is always the vault-root `_sources/`.

## Entry Point Workflow

### Status short-circuit (`--status`)

If invoked with `--status`, do **only** this and then stop — before any preflight or source access:

1. Read `_meta/last-ingest.md`.
2. If it exists, print its timestamp (local time):

   ```
   Last ingest: <YYYY-MM-DD HH:MM ZZZ>
   ```

   Print the human-readable body line verbatim (it is already local time). Do not print the rest of the file.
3. If it does not exist, print:

   ```
   No ingest recorded yet — run /obsidian:ingest to ingest sources.
   ```

4. If any other argument was supplied alongside `--status`, `--status` wins: print the status as above, then note that the other arguments were ignored.

Perform no inventory scan, no `_sources/` reads, and no writes in this mode.

### Files backlog short-circuit (`--files-backlog`)

If invoked with `--files-backlog`, run the preflight (§0), then follow `obsidian-claude-export-ingest` §3b. Then append a design-log line and regenerate indexes (§6, §7) if a note changed. Do not update `_meta/last-ingest.md` — a backfill is not an ingest of new sources.

### 0. Preflight

**Obsidian CLI check (non-fatal):** Run once at session start:
```bash
python3 .agents/skills/obsidian-ingest/extract_conversations.py check-cli
```
If it exits non-zero, note that topic matching will fall back to grep and continue — do not abort the ingest run.

There is no mode to detect. The source directory is always the vault-root `_sources/`, routing always spans the whole vault, and unmatched conversations are always routed interactively by the sub-skill (§2a). Proceed with Steps 1–8.

### 1. Verify _sources/ exists

If the vault-root `_sources/` directory does not exist, stop:

```
No _sources/ directory found at _sources/
Nothing to ingest.
```

### 2–3. Build inventory, discover and classify source files — one command

Run the `inventory` subcommand. It performs **all** of Steps 2 and 3 — note scanning, UUID inventory, SHA256 inventory, ignored-UUID set, source discovery, type classification, hashing, and the change verdict — and emits one JSON blob:

```bash
python3 .agents/skills/obsidian-ingest/extract_conversations.py inventory
```

Take no arguments. (The script accepts a `--scope` flag, but it is a diagnostic left for `retire_sources.py` and stray-directory checks — ingest never passes it.)

> **Dependency:** `inventory` needs PyYAML (`pip install pyyaml`), the same dependency `obsidian-lint-light` already has. It is imported lazily, so every other subcommand — including the Step 0 `check-cli` — still works without it. If `inventory` exits 2 with an install hint, that is the cause.

> **Do not hand-write a scan script, and do not reimplement any of this inline.** Earlier runs wrote throwaway Python to walk notes and parse frontmatter every single time — that is pure token waste and it drifts from the rules below. One call replaces it. Equally: **never use an `ls`/`find` listing already visible in the conversation.** The user may have listed `_sources/` before invoking the skill; that snapshot is stale the moment a file is added. The `inventory` call *is* the discovery step, so running it satisfies this requirement.

**Output fields:**

| Field | Meaning |
|---|---|
| `mode` / `scope` | Always `vault` / `null` for an ingest run |
| `source_dir` / `source_dir_exists` | The vault-root `_sources/` (use for the Step 1 check) |
| `notes_scanned` | Count of notes contributing to the inventory |
| `uuid_inventory` | `{uuid: {note, message_count, last_ingested}}` — pass to the sub-skill |
| `ignored_uuids` | `[uuid]` from `_meta/ignored-uuids.md` |
| `sources[]` | One entry per discovered file: `filename`, `path`, `type`, `sha256`, `verdict`, `notes`, `recorded_sha256`, `missing_uuids` |
| `missing_uuids` | Claude exports only. Conversations present in the file that no note has ingested and that are not in `ignored_uuids`. Non-empty means `verdict` is `incomplete`. Empty for every other type and verdict. |
| `owners` | `{note_path: human\|agent\|shared}` — use for the Step 5d owner check without re-reading frontmatter |

**`type`** is `claude-export` (a `.json` or `.zip` holding a valid conversations export), `claude-projects` (a projects export zip — see §4b), `chatgpt-export` (an OpenAI data export zip — see §4c), or `document` (everything else, including a `.zip` with neither — report those unprocessable).

**`verdict`** is the Step 5b decision, precomputed:

| `verdict` | Meaning | Action |
|---|---|---|
| `unchanged` | Hash matches the value recorded in `notes` | Skip. Count as unchanged. Run nothing further against this file. |
| `incomplete` | Claude export only. Hash matches, but conversations inside it were never ingested | **Do not skip.** Process exactly the uuids in `missing_uuids` and no others. |
| `changed` | Hash differs from the recorded value | Update — append an `## Update` section (§5e) |
| `untracked` | Cited by a note as a flat `sources:` string with no sha256 | Treat as an update; target note is already known |
| `new` | Not cited by any note | New ingest; discover a target via §5c |

> **Why `incomplete` exists.** A multi-conversation export is not one unit of content. The first note that records the export's sha256 used to mark the whole file `unchanged`, so the next run skipped it and every conversation that had not yet been written was dropped — permanently, with nothing in the report saying so. A partial run is ordinary: a crash, a lost network, a context compaction, or the user stopping the run. This was measured on 2026-08-09, when an export read `unchanged` with 5 of 16 conversations ingested. The `inventory` command now checks conversation coverage, not just the hash.

**`notes`** lists *every* note citing that source, not just one — a single export routes to many notes, and a document whose decision touches several notes is cited by each. When `verdict` is not `new`, these are the known targets and §5c discovery is skipped.

> **Why the inventory is keyed on (project folder, filename), not the bare `_sources/<name>` path:** recorded paths are project-relative by convention, so a bare basename key collides across projects. A collision does not merely cause a wrong skip — it can append an `## Update` section to a note in an entirely different project. The script resolves each recorded path project-relative first, then vault-root, matching `lint.py`.

> **Underscore directories are excluded zones** (see AGENTS.md): never scanned for inventory, never used as routing/topic-match targets (§2a / §5c), and never listed as note content in indexes. The `inventory` command already enforces this. Read-only ones — `_sources/` (read only as ingest *input*) and pure-reference dirs like `Reference/_reference/` — are never written to. `_meta/` is the exception: it is the writable operational store (indexes, `last-ingest.md`, design-log, ignored-uuids) and is still excluded from scanning/routing.

> A dropped `.zip` needs no manual extraction — `detect`/`list`/`classify`/`extract` read the export JSON from inside the archive (preferring a `conversations.json` member). Hashing uses the **inner** export content, not the zip packaging — so an unchanged re-download hashes the same (the unchanged-skip actually fires) even though the zip's outer bytes vary, and a zip and a hand-extracted `conversations.json` with identical content share one hash.

If `source_dir_exists` is false, stop with the Step 1 message. If `sources` is empty:
```
No source files found in <source-directory>
Nothing to ingest.
```

### 4. Process Claude export files first

> Claude export files are `.json` exports **or** `.zip` archives that wrap one — both are handled by the same flow (the script reads the export JSON from inside a zip transparently).

> **Skip unchanged exports first.** The Step 2–3 inventory already hashed every file. Any entry with `type: claude-export` and `verdict: unchanged` is byte-identical to the last ingest **and** every conversation inside it is already accounted for: **stop immediately — run no further commands against it.** Do NOT run `sha256`, `list`, `classify`, `detect`, or any other subcommand. Increment the "files skipped (unchanged)" counter by 1 and move on. Only exports with `verdict` of `incomplete`, `changed`, `untracked`, or `new` reach the sub-skill.

> **`incomplete` is not `unchanged`.** The hash matches, but `missing_uuids` names conversations no note holds. Pass the file to the sub-skill with its `missing_uuids` list, and process **only** those uuids — every other conversation in that export is already ingested and must not be touched. Report the count in the run summary so that the user learns a previous run ended early. Never let `incomplete` fall through the unchanged-skip: that is the data-loss path the verdict exists to close.

If any Claude export files remain after that filter, delegate to `obsidian-claude-export-ingest` sub-skill logic, passing:
- Source directory (`source_dir` from the inventory)
- UUID inventory (`uuid_inventory`)
- SHA256 inventory (the `sources` array)
- Ignored UUIDs set (`ignored_uuids`)
- Owners map (`owners`) — needed by the sub-skill §2b owner filter
- Search paths: all scanned project folders (routing always spans the whole vault)
- List of Claude export file paths with a non-`unchanged` verdict

Collect the run summary data (updated count, skipped count, ignored count, routed count, declined list, owner conflicts, failures, execution path) for the final report.

> **Delegation mechanism:** Read `obsidian-claude-export-ingest/SKILL.md` and execute its **control flow** — enumeration, classification, and interactive routing — inline within this same context. Do not use the Skill tool for the sub-skill itself: a fresh context would lose the inventory maps. The inventory JSON from Steps 2–3 is passed to the sub-skill as in-memory context — it does not re-scan.
>
> **This does not forbid subagents for the writes.** Once routing is settled, sub-skill §2b counts the work items and branches. Below the threshold it writes the notes inline. At or above it, this session dispatches one subagent per target note and aggregates their reports. The rule above is about the *sub-skill*, which needs the maps. A per-note write subagent needs none of them — it gets a self-contained prompt. Both statements hold at once. Do not read the "execute inline" rule as a ban on §2b.
>
> The threshold branch is **pre-authorized** by the user (2026-08-09). Do not stop to ask before spawning when it fires.

### 4b. Process Claude projects exports

A `type: claude-projects` source is the `projects` category of a data export: a zip of `projects/<uuid>.json`, one per Claude project, each with `docs[]` holding full text. **The doc is the unit of change, not the zip.** Inventory lists every doc that no note holds at its current content in `pending_docs`, and sets `verdict: unchanged` when that list is empty. Skip an `unchanged` projects zip like any other.

Each pending doc carries: `doc_uuid`, `filename`, `project`, `project_uuid`, `starter_project`, `chars`, `sha256`, `state` (`new` or `changed`), `notes` (notes that hold an older version), `route`, `recorded_as_source`, and `chat_copies`.

**a. Route by project, once.** `route` comes from `_meta/project-routes.md`, a YAML list in the body:

```yaml
project_routes:
  - project_uuid: <uuid>
    name: <project name, for humans>
    target: Projects/Example/Widget   # a folder, a note path, or `ignore`
```

For every project with pending docs and no route, ask the human where it goes, batched four projects per `AskUserQuestion` call. Offer the best folder or note from `_meta/index.md`, a new folder, and `ignore`. Anthropic's starter project (`starter_project: true`) defaults to `ignore`. Append each answer to `_meta/project-routes.md` (create it with `owner: agent` frontmatter if missing). A single doc the human wants skipped goes in `_meta/ignored-uuids.md` by its `doc_uuid`.

**b. Decide what each doc needs, cheapest first:**

| Signal | Action |
|---|---|
| `recorded_as_source` is not empty | The content is already in that note, ingested from a hand-copied source. Add only the `ingested_project_docs:` entry to it. No body change. |
| a `chat_copies` item is `identical: true` and its `note` is set | The note already holds this file through its chat. Add only the entry. |
| a `chat_copies` item has a `note`, but is not identical or is an `incomplete rebuild` | The doc is the better copy. Read the note section and the doc, then add a `## Project file — <filename>` section with only what the note lacks or has wrong. Correct any `^[inferred]` or `^[ambiguous]` marker the doc now settles. |
| `state: changed` | Append `## Update — YYYY-MM-DD` to the note in `notes` with only the changes. |
| otherwise | Ingest as a document (§5c–5e) into the route target: a new note in the folder, or a section in the note. |

Read a doc with:

```bash
python3 .agents/skills/obsidian-ingest/extract_conversations.py _sources/<projects zip> project-doc <doc_uuid>
```

Add `--full` when the output says the excerpt ends and the doc matters. The header prints the project's description and instructions, which help routing and framing.

A doc that is a finished artifact — a rulebook, a design spec — may be better recorded than paraphrased. Ask the human if unsure.

**c. Frontmatter.** Append to the note's `sources:` (path of the projects zip and its sha256 from inventory) and to `ingested_project_docs:`, last, after the body:

```yaml
ingested_project_docs:
  - uuid: <doc_uuid>
    project: <project name>
    filename: <filename>
    sha256: <doc sha256 from inventory — the doc's content, not the zip>
    last_ingested: <YYYY-MM-DDTHH:MM:SSZ>
```

For a `changed` doc already in `ingested_project_docs:`, update `sha256` and `last_ingested` on that entry in place. Check owner before every write (§5d).

### 4c. Process ChatGPT exports

A `type: chatgpt-export` source is an OpenAI data export zip: `conversations.json`, `chat.html`, attachments as `file_*.dat`, and small metadata files. **The conversation is the unit of change, not the zip.** A ChatGPT export is a full dump, so the file hash moves every time while most conversations do not. Inventory lists what is new or longer in `pending_chats`, and sets `verdict: unchanged` when that list is empty.

Each pending chat carries `uuid` (the `conversation_id`), `app: chatgpt`, `name`, `message_count` (visible messages only), `created_at`, `updated_at`, `archived`, `chars`, `attachments`, and `state` (`new` or `update`, with `from_message` on an update).

**a. Let the human pick.** Show the list with title, date, message count, and size. Do not ingest all of it by default. Route each pick like any unmatched conversation (`obsidian-claude-export-ingest` §2a), and batch the prompts.

**b. Read one conversation:**

```bash
python3 .agents/skills/obsidian-ingest/extract_conversations.py <chatgpt zip> chatgpt-extract <conversation_id>
```

Add `--from <from_message>` for an update. The command prints only the branch ending at `current_node`, so abandoned regenerations never reach the note. Reasoning content (`thoughts`, `reasoning_recap`) is dropped.

**c. Attachments.** The export carries no text for them.

- **Images never enter the vault.** Record the filename and what the human said with it. Never copy an image file, and never link one.
- **A PDF is extracted only when the human approves it, one file at a time.** Ask before reading any of it. The content is often already in the conversation.
- **Never ingest the text of a published work.** See the copyright rule in `AGENTS.md`. Record the title, the edition, and the decision instead.

**d. Frontmatter.** ChatGPT conversations share `ingested_uuids:` with Claude, tagged by app:

```yaml
ingested_uuids:
  - uuid: <conversation_id>
    app: chatgpt
    name: <title>
    message_count: <visible message count from inventory>
    last_ingested: <YYYY-MM-DDTHH:MM:SSZ>
```

Ids are uuids and cannot collide with Claude ids. Write `message_count` exactly as inventory reports it, because the next run compares against it.

**e. Purge the export when the run ends.** Decided 2026-09-16: a full dump every time means `_sources/` would grow by about 15 MB per export for content the notes already hold. After the human confirms the notes look right:

```bash
python3 .agents/skills/obsidian-ingest/retire_sources.py _sources/<export zip>                   # dry run
python3 .agents/skills/obsidian-ingest/retire_sources.py _sources/<export zip> --apply --delete  # retire, then delete
```

Show the human the dry run first. `--delete` removes the zip only when every citing entry is retired and the file has not changed since ingest. If the script prints a warning, it keeps the file. Report the warning and stop. The hash stays in the notes, so a re-download of the same export is still recognized. The tracking that matters lives in `ingested_uuids:`, not in the zip.

### 5. Process document files

For each document file (`.md`, `.html`, `.pdf`, or other), in the order found:

**File type extraction:**
- `.md` files: read directly as text
- `.html` files: strip tags to extract readable text before paraphrasing
- `.pdf` files: use `pdftotext <file> -` or equivalent CLI tool to extract plain text before paraphrasing
- Other binary formats: use whatever text extraction tool is available; if none, report the file as unprocessable and skip it

**a–b. Read the verdict from the Step 2–3 inventory.** The hash and change decision are already computed — do **not** run `sha256sum` or recompute anything:

- `verdict: unchanged` → skip. Note as "unchanged".
- `verdict: changed` → file changed since last ingest. Proceed as update.
- `verdict: untracked` → cited without a recorded hash. Proceed as update, and upgrade the flat `sources:` string to `path`/`sha256` on write (§5f).
- `verdict: new` → new file. Proceed as new ingest.

> **If `verdict` is anything other than `new`,** the target note(s) are already known from the entry's `notes` list — skip §5c discovery entirely and proceed directly to §5d. §5c applies only to `verdict: new`. If `notes` holds more than one entry, the source feeds several notes: update each, or ask the user which if the new content clearly belongs to only one.

**c. Find or propose a target note** (`verdict: new` only):
1. Read `_meta/index.md`, then the relevant `<folder>/index.md` — the retrieval hierarchy in AGENTS.md puts these before grep, and an index read usually settles the target in one call
2. Only if the indexes do not settle it: grep for topic terms from the file title/headings (for `.md`) or filename (for binary formats), then scan `title:`/`summary:` frontmatter of candidates
3. Full read only if needed to confirm a match

- One strong match, file is new → ask user to confirm before updating
- One strong match, file has changed → ask user to confirm before appending update section
- If user declines either confirmation, skip the file and note it as 'declined' in the run summary.
- Multiple strong matches → ask user to choose
- No match → create new note automatically using naming convention `<Project Prefix> - <Topic>.md`

**d. Check owner before writing:**

This check applies only when updating an existing note. When creating a new note (§5c, no match), write `owner: shared` as the default — do not perform an owner check on a note that does not yet exist.

| `owner` | Action |
|---|---|
| `human` | **Stop.** Print diff. Do not write. Tell user to merge manually. |
| `agent` | Update freely. |
| `shared` | Update freely. Preserve sections not traceable to the source. |

Default `owner` is `shared` if the field is absent.

**e. Write content** with provenance markers:

| Source relationship | Marker |
|---|---|
| Direct paraphrase or extraction | _(none — default)_ |
| Synthesis beyond what source states | `^[inferred]` |
| Source unclear or self-contradictory | `^[ambiguous]` |

Paraphrase tight — load-bearing claims and decisions only, not a blow-by-blow narration of the source. Complete, not exhaustive.

For **changed files** (SHA256 differs from stored), append rather than replace:

```markdown
## Update — YYYY-MM-DD

<!-- source: _sources/<filename>, sha256: <new_sha256> -->

<paraphrased new or changed content with provenance markers>
```

**f. Update frontmatter:**

```yaml
sources:
  - path: _sources/<filename>
    sha256: <sha256>
updated: <YYYY-MM-DDTHH:MM:SSZ>
```

Upgrade any existing flat `sources:` strings to `path/sha256` objects on the note being written. Never touch other notes solely to upgrade their format.

Preserve all other existing frontmatter fields. Append to `sources:` — never replace.

### 6. Log non-obvious decisions

Append one dated sentence to `_meta/design-log.md` for:
- Routing a file to an unexpected note
- Choosing between ambiguous matches
- Owner conflict (human-owned note blocked)

Routine ingestions (clear match, no conflict) do not need a log entry.

### 7. Regenerate indexes

Indexes are derived artifacts that mirror current note state. Regenerate after every ingest run that wrote at least one note. If no notes were written (e.g., everything was unchanged or declined), skip this step.

Index regeneration always runs here, in this session, and only once. If the run took the §2b parallel path, wait until every subagent has reported, then regenerate. A subagent never touches an index.

**a. Project index** for every folder written to:

Regenerate `index.md` for **each distinct folder** that received a written or created note this run. A single run routinely writes across several — `Ideas/Example/Gadget/`, `Reference/Example Topic/`, and others. Each needs its own index, including newly created folders that had no index before.

- `owner: agent` (always — these are fully regenerated, never hand-edited)
- One entry per `.md` note in that folder, excluding `_sources/`, the index itself, and any subdirectories
- Entry format: `- [[Note Title]] — <summary line from frontmatter>`
- If a note has no `summary:` field, fall back to the first H1 heading or the note's filename stem
- Sort entries alphabetically by note title

```yaml
---
owner: agent
updated: <YYYY-MM-DDTHH:MM:SSZ>
summary: Index of <Project Name> notes (regenerated by obsidian-ingest).
---

# <Project Name> — Index

- [[Note A]] — summary line
- [[Note B]] — summary line
```

**b. Vault-wide master index** at `_meta/index.md`:

- `owner: agent`
- Group entries by project folder path (one H2 per project)
- Within each section, alphabetical by note title; same `[[wikilink]] — summary` format
- Include every note under the `audited_roots` in `_meta/vault-config.yml` — exclude `_sources/`, `_meta/` itself, and any `index.md` files
- Walk the vault top-down; skip any directory whose name starts with `_` except via the explicit allowlist above

```yaml
---
owner: agent
updated: <YYYY-MM-DDTHH:MM:SSZ>
summary: Vault-wide master index of all derived notes (regenerated by obsidian-ingest and obsidian-lint-light).
---

# Vault Master Index

## Ideas/Example/Gadget

- [[Note A]] — summary line
...

## Projects/Example/Widget

- [[Note B]] — summary line
...
```

**Both indexes are fully regenerated each run** — there is no append behavior. Wikilinks stay flat (no folder paths) since Obsidian resolves them by title across the vault.

### 8. Print run summary

After all processing completes:

```
Ingest complete — <source-directory>

  Documents
    Processed:  N  (N new notes, N updated, N unchanged, N declined)

  Claude export conversations
    Updated:    N conversations (N new messages total)
    Skipped:    N files unchanged (SHA256 match — no processing)
    Ignored:    N — previously marked ignore
    Declined:   N — user chose not to include this run
    Routed new: N — new conversations routed interactively
    Path:       inline | parallel (N subagents, N work items)

Declined conversations (user skipped — re-run and confirm to include):
  - "<name>" (uuid: <uuid>...)

Blocked — note is owner: human (diff printed above, merge by hand):
  - "<name>" → <note path>

FAILED — not ingested, re-run to retry:
  - <note path>: <error>
```

Omit the `N declined`, `N ignored`, and `N routed new` counts if zero. Omit each section if its count is zero. If no Claude export files were processed, omit the Claude export conversations section.

Print the `Path:` line on every run that processed conversations. Print the `FAILED` section whenever any group failed, even if other groups succeeded. The last-ingest watermark advances on partial success, so an unreported failure hides work that never happened.

## Naming Convention for New Notes

`<Folder Prefix> - <Topic>.md`

Folder Prefix = the name of the destination folder the note is routed into, spaces preserved.

Examples:
- Routed into `Ideas/Example/Gadget/` → `Gadget - Architecture.md`
- Routed into `Projects/Example/Widget/` → `Widget - Repository Structure.md`

## Source Types

| Type | Typical target | Watch for |
|---|---|---|
| Claude Desktop export JSON or `.zip` | Routed via `obsidian-claude-export-ingest` (zip read transparently — no manual extraction) | Contains all chats — conversations with no topic match are routed interactively (§2a), never auto-filed |
| Structured reference `.md` | Architecture or survey note | Usually maps 1:1 to an existing note |
| Decision summary | Note with `status: decided`, `decision: <value>` | Preserve the `decision:` frontmatter field |
| `.html`, `.pdf` | Topic note | Extract text content; treat as document file |
| YouTube transcript `.md` (`youtube-*.md`, made by `fetch_youtube.py`) | Topic note | Paraphrase claims. Never paste the transcript into a note. Cite with the `[m:ss]` timestamp link. |

## Common Mistakes

- **Writing anything to `_sources/`** — never. Read only, always. Fetching a new export is a separate
  operation with its own tool (`fetch_export.py`), never something the ingest flow does. The download
  itself needs a browser signed in to claude.ai; `fetch_export.py` prints the links and places the files.
  A YouTube link is fetched the same way, before ingest (the `obsidian-youtube` skill wraps both steps): `python3 .agents/skills/obsidian-ingest/fetch_youtube.py <url> [...]`
  writes a transcript `.md` to `_sources/`. Videos with no captions fail; there is no speech-to-text.
- **Treating a projects zip as one source** — the doc is the unit. Every export makes a new zip even
  when nothing changed. Work from `pending_docs`, and record each doc in `ingested_project_docs:`.
- **Dropping provenance markers** — ugly markers are the audit trail. Never remove for prose flow.
- **Overwriting human annotations in `owner: shared` notes** — scan for sections with no corresponding source claim before replacing content.
- **Creating a new note when an existing one covers the topic** — read `_meta/index.md` and the folder index before concluding nothing covers it.
- **Replacing `sources:` or `ingested_uuids:` lists** — always append. History is permanent.
- **Auto-filing an unmatched Claude conversation** — never guess a destination. Unmatched conversations go through §2a interactive routing in `obsidian-claude-export-ingest`, where the user picks the folder and name.
- **Upgrading flat `sources:` strings in notes you are not otherwise touching** — only upgrade on first touch. A banner or `updated:`-only edit is not an ingest touch: stamping a hash there would falsely assert the note reflects the current source.
- **Using file timestamps instead of content for change detection** — the `inventory` command hashes content. Never substitute mtime.
- **Hand-writing an inventory or scan script** — Steps 2–3 are one `inventory` call. Do not walk notes, parse frontmatter, or `sha256sum` files yourself; that is token waste and it drifts from the spec.
- **Using a prior `ls` or file listing from the conversation context** — the `inventory` call performs discovery itself. The user may have listed `_sources/` before invoking the skill; that snapshot is stale the moment a file is added.
- **Running any subcommand against an `unchanged` file** — that verdict means byte-identical to the last ingest. Stop. Do not run `sha256`, `list`, `classify`, or `detect`. The only valid action is incrementing the skipped counter and moving on.
- **Counting export conversations instead of work items when applying the §2b threshold** — an export with 200 conversations of which 198 are unchanged holds 2 work items and stays inline. Count only conversations classified `new` (and routed in §2a) or `update`.
- **Applying the threshold before routing is settled** — the order is inventory, classify, batch the §2a prompts, count, then branch. Extracting a conversation before that costs about 16k tokens in this context for nothing.
- **Regenerating indexes inside a subagent** — Step 7 runs in this session, once, after every group reports back.
- **Treating a `<project>/_sources/` directory as ingestable** — per-project source dirs were removed on 2026-07-29; the vault-root `_sources/` is the only one. A nested one is an anomaly. Never add a project-path argument back to this skill.
- **Moving a resurrected `<project>/_sources/` file into `_sources/`** — check `retired: true` on the citing notes first. A restore reinstated four such directories on 2026-08-09, and every file in them was already retired. Moving them would have re-ingested content the notes already held. Retired means report and stop, not relocate. Only genuinely uningested files get moved.
