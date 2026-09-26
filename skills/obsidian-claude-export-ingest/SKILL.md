---
name: obsidian-claude-export-ingest
description: INTERNAL — called by obsidian-ingest. Processes Claude Desktop conversation export JSON files in _sources/, routes conversations to existing or new notes, and tracks ingestion state by UUID. Never invoke directly.
---

# obsidian-claude-export-ingest (Internal Sub-skill)

Do NOT invoke this directly. It is called by `obsidian-ingest` when Claude export JSON files are detected in `_sources/`.

## Inputs (passed from entry point)

- **Source directory**: the vault-root `_sources/` — always. There is no project-scoped variant.
- **UUID inventory**: map of `{uuid → {note_path, message_count, last_ingested}}`
- **SHA256 inventory**: map of `{file_path → {sha256, note_path}}`
- **Ignored UUIDs**: set of `{uuid}` to skip silently
- **Owners**: map of `{note_path → human|agent|shared}` from the inventory — used by the §2b owner filter, so no note frontmatter is re-read
- **Search paths**: list of vault-relative folder paths to search for topic matching — all scanned project folders, since routing always spans the whole vault
- **Export files**: list of `*.json` (or `*.zip` archives wrapping one) paths in the source directory confirmed as Claude export format. The extraction script reads the export JSON transparently, so a `.zip` is passed through the same subcommands (`list`/`classify`/`extract`) as a bare `.json` — no manual extraction step.

> These inventories are built by the entry point (`obsidian-ingest`) before calling this sub-skill. It reads `ingested_uuids:` and `sources:` frontmatter from all relevant `.md` notes and passes the populated maps in.

## Workflow

### 1. Enumerate conversations in each JSON file

Run the extraction script to get a conversation list:

```bash
python3 .agents/skills/obsidian-ingest/extract_conversations.py <file> list
```

Output is tab-separated rows: `uuid | name | message_count | created_at | updated_at`. Conversations with empty names or no message text are already excluded by the script.

### 2. Route each conversation

First, build a classification against the UUID inventory. Construct the inventory as a JSON string (`{"uuid": message_count, ...}`) from the maps passed in by the entry point, then run:

```bash
python3 .agents/skills/obsidian-ingest/extract_conversations.py <file> classify '{"uuid1": N, ...}'
```

Output rows: `uuid | action | name | stored_count | current_count`
- `unchanged` — skip; add to skipped count
- `decreased` — skip; log as "skipped (count decreased)"
- `update` — append update section; no re-prompt needed
- `new` — topic-match and route (see below)

Additionally filter against the ignored UUIDs set before processing any `new` rows.

| Classification | Ignored set | Action |
|---|---|---|
| `unchanged` | — | Skip |
| `decreased` | — | Skip, log |
| `update` | — | Append update section |
| `new` | UUID in ignored set | Skip silently |
| `new` | UUID not in ignored set, topic match found | Ask user to confirm, then ingest |
| `new` | UUID not in ignored set, topic ambiguous (2+ candidates) | Ask user to choose target note |
| `new` | UUID not in ignored set, no match anywhere in the vault | **Interactive routing** — see §2a. Never auto-create and never silently drop. |

**Topic matching for unknown conversations:**
1. Extract keywords from conversation `name` and first 300 chars of the first assistant message (nouns, domain terms — ignore filler words like "the", "is", "how")
2. Search across all **search paths** using the Obsidian CLI (preferred) or grep (fallback):
   - **CLI (preferred):** `obsidian search "<keywords>"` — uses Obsidian's full-text index; handles aliases and frontmatter fields natively. Parse results for note paths within the search paths.
   - **Fallback (CLI unavailable):** grep note titles and `summary:` frontmatter fields within the search paths.
3. Score by keyword overlap. One clear winner = strong match. Two or more candidates where the top two scores are within 20% of each other = ambiguous — ask user to choose.

### 2a. Interactive Unmatched Routing

For each conversation with no topic match anywhere in the vault, prompt the user with a brief summary:

```
Unmatched conversation: "<conversation name>"
  Preview: <first 150 chars of first assistant message>

Route this conversation:
  1. Add to an existing project note
  2. Create a new note (you'll name it and choose a project folder)
  3. Ignore — skip now and never ask again
  4. Skip for now — ask again next run

Choice:
```

**Option 1 — Route to existing note:** show a numbered list of all project notes from the search paths. User picks one. Ingest the full conversation into that note.

**Option 2 — Create new note:** ask for a project folder (show available `Projects/`, `Ideas/`, etc.) and a note name. Create the note with `owner: shared` and ingest the full conversation.

**Option 3 — Ignore permanently:** add the UUID to the pending ignored list (persisted in §Ignored UUID Persistence). Skip this conversation.

**Option 4 — Skip for now:** do nothing. The UUID is not recorded anywhere, so the next ingest run will ask again.

**Batch the prompts. This is mandatory, not a preference.** Resolve the routing of every unmatched conversation before you write any note. Use `AskUserQuestion`, which carries up to 4 questions per call, in two passes:

1. **Pass 1 — the four-way choice.** One question per unmatched conversation, four per call. The question text is the conversation name plus the preview. The options are the four choices above.
2. **Pass 2 — destinations.** Ask only for the conversations that chose option 1 or option 2, again four per call. For option 1, the options are the top candidate notes, and the user can type any other note through "Other". For option 2, ask for the folder, and take the note name from "Other".

A 16-conversation export must cost about 4 to 8 prompt calls, not 16. If you find yourself asking about one conversation, stopping, then asking about the next, you are doing this wrong. The one-at-a-time pattern is what made the 2026-08-09 run tedious.

### 2b. Choose the execution path — inline or parallel subagents

Routing is now settled. Every conversation that will be written has a target note. Before you extract anything, decide how to execute the writes.

**Build the work list.** One item per conversation with an action of `new` (that survived §2a as routed) or `update`. Conversations that are unchanged, decreased, ignored, or declined are not work items.

**Filter by owner first, in this session.** The entry point passed an `owners` map from the inventory. For any work item whose target note has `owner: human`, do not dispatch it. Handle it here: extract the conversation, print the diff of what you would have added, and record it as an owner conflict. A subagent cannot do this, because its report never reaches the user.

> **A target absent from `owners` is not an `owner: shared` target.** The map covers only the `writable_roots` in `_meta/vault-config.yml`. A folder outside that list can hold `owner: human` notes. If §2a routed a conversation to a note or folder that the map does not list, read that note's frontmatter here, in this session, before you decide. Never let a missing key default to writable.

**Group the remaining items by target note.** One group per distinct note. A group can hold several conversations. Grouping prevents two writers from touching one note's frontmatter at the same time.

**Apply the size floor first, then the threshold:**

```
MIN_DISPATCH_MESSAGES    = 10     # a group at or below this always writes inline
PARALLEL_INGEST_THRESHOLD = 5     # dispatchable groups; retune these two numbers
```

Split the groups in two before you count anything:

- **Small groups** — total `current_count` across the group is at or below
  `MIN_DISPATCH_MESSAGES`. These **always** write inline, however many items the
  run holds. They never count toward the threshold.
- **Dispatchable groups** — everything else. Only these are counted and only
  these are ever dispatched.

| Dispatchable groups | Path |
|---|---|
| Fewer than the threshold | **Inline.** Run §3–§7 in this session for everything. |
| At or above the threshold | **Parallel.** Dispatch the dispatchable groups in waves; write the small groups inline in this session. |

> **Why the floor exists.** A subagent's cost is dominated by a fixed cold start of
> roughly 33k tokens — reading `CLAUDE.md`, orienting, locating the note — which it
> pays in full whether the conversation holds 4 messages or 46. Measured on the
> 2026-09-04 run of 15 work items:
>
> | Group size | Agents | Messages | Tokens | Per message |
> |---|---|---|---|---|
> | ≤9 messages | 7 | 45 | 338,804 | 7,528 |
> | >9 messages | 7 | 180 | 420,613 | 2,336 |
>
> The small fifth of the content took nearly half the spend. A 4-message
> conversation cost 40,799 tokens; the 46-message one cost 66,735. Writing the
> small ones inline on that run would have saved about 280k, roughly 35%, and
> would have cost nothing in compaction risk — 45 messages of extracts is a
> rounding error in the context window.
>
> The same reasoning applies to a **retry**: never re-dispatch a small group. The
> MoE retry on that run spent 55,741 tokens on 6 messages.

> **Why a threshold, and what it does not buy.** Both sides are now measured, from the 2026-08-09 replay.
>
> - A conversation extract medians about 16k tokens. Inline, every extract stays in context for the rest of the run, so 16 of them cost roughly 2.2M raw input tokens.
> - A subagent averaged **49k tokens**, across ten measured runs. Sixteen project to about 780k. Raw tokens therefore favor subagents by roughly 2.8x.
>
> **This is not a cost saving.** Inline, the re-sent context is a prompt cache read at a steep discount; subagent tokens are almost all fresh, uncached input, because each agent starts cold and shares no cache with the parent. Adjusted for caching the two are close, and inline can be cheaper. Do not sell §2b as a way to spend less.
>
> What it does buy is **context-window survival and failure isolation**. At 16 conversations the extracts alone reach about 256k tokens and the run compacts partway through. The 2026-08-09 run did, and its gapped commit sequence (3, then 7, then 9, 10, 11 of 16) is what that looks like from outside.
>
> Note also that per-agent cost barely tracks conversation size — a 6-message conversation still cost 31k, because the cold start dominates. Small conversations are the worst dispatch candidates.
>
> So the threshold is a spend-versus-completion dial, not a free win. Set it at 5 if finishing a large run intact matters most. Raise it toward 10 if the binding constraint is a usage limit, which keeps ordinary runs inline and cheap. Retune the constants, not the design.
>
> `MIN_DISPATCH_MESSAGES` is the other half of that dial, and it is the one that pays first. The threshold trades spend for completion; the floor is close to free, because the groups it keeps inline are the ones that were never going to fill a context window.

**Dispatch is pre-authorized.** The user asked for this behavior on 2026-08-09. Do not stop to ask permission to spawn agents when the threshold fires. This is the one place in the vault where subagents are expected.

**Dispatch rules:**

- Use `subagent_type: general-purpose`.
- Dispatch in **waves of 4 to 6 groups**. Put every `Agent` call of one wave in a single assistant turn so that wave runs concurrently. Wait for the whole wave to report, aggregate it, then start the next. Serial dispatch one-at-a-time keeps the wall-clock tedium and wins nothing; dispatching all 16 at once makes any bad moment cost the whole run.

> **Wave size is a reliability control, not a throughput knob.** On 2026-08-09 a 12-wide dispatch met a wi-fi outage: 8 agents died with nothing written, 2 stalled for 600 seconds, and 1 wrote a note it never reported. That produced the `incomplete` state this skill now detects. A wave of 4 to 6 bounds that damage to one wave, and the parent sees the failure and can stop before spending the next. The same run produced **no evidence of a concurrency ceiling** — every failure traced to the network — so do not justify the wave size by rate limits.
- Give each subagent exactly one target note.
- Carry `source`, `sha256`, and `total_messages` on **every** work item, not once per prompt. One group can hold conversations from two different export files. The parent already has all three: `source` and `sha256` come from the inventory `sources[]` array, and `total_messages` is the `current_count` column of the §2 `classify` output.
- A subagent must never write to `_meta/`. `design-log.md`, `ignored-uuids.md`, and `last-ingest.md` are shared append targets, and concurrent appends lose writes. Subagents return their design-log line as text. This session appends it.
- A subagent must never regenerate an index, run `git`, or read or write any note other than its target.

**Subagent prompt template.** The subagent starts cold. Project `AGENTS.md` reaches it (Claude Code loads it through the `CLAUDE.md` import), but this skill does not, so the prompt must carry every rule it needs:

```
Ingest Claude conversations into one Obsidian note. Vault root: <absolute vault path>.

Target note: <vault-relative path>
Owner: <agent|shared>   (already verified — it is not `human`)
Exists: <yes|no>

Conversations to ingest:
  - uuid: <uuid>  action: new     source: _sources/<file>  sha256: <sha>
    total_messages: <current_count>
  - uuid: <uuid>  action: update  source: _sources/<file>  sha256: <sha>
    total_messages: <current_count>  from_message: <stored_count>

Get the text for each one:
  python3 .agents/skills/obsidian-ingest/extract_conversations.py <source> extract <uuid>
For an update, add: --from <from_message>

Write rules:
- Paraphrase. Do not transcribe. Capture the load-bearing claims, decisions, and
  concrete facts. Drop the exploratory back-and-forth. Complete, not exhaustive.
- Structure with H2 headings, one per topic cluster.
- Mark provenance inline: no marker for extracted or paraphrased content,
  `^[inferred]` for synthesis beyond the source, `^[ambiguous]` where the source
  is unclear or self-contradictory. Never drop a marker for prose flow.
- For action `update`, append a section at the bottom of the note:
      ## Update — <YYYY-MM-DD>

      <!-- source: <source>, uuid: <uuid>,
           messages: <from_message>–<total_messages> -->
- Files. The extract prints file content between <<<FILE and FILE>>>:
    - `[attached file: ...]` is material the human gave Claude. `[created file: ...]`
      is a file Claude wrote, printed at the end in its final state. Both are
      source content. Paraphrase them like messages. A created file is often the
      main output of the conversation, so give it its own H2 when it is a plan,
      brief, or tracker.
    - `INCOMPLETE REBUILD` means some edits could not be replayed. Say in the note
      that the final file could not be fully rebuilt. Mark details that later
      edits could have changed `^[ambiguous]`.
    - `excerpt ends` means the file was cut. If the file carries the conversation,
      read the rest with `extract_conversations.py <source> files <uuid> --full`.
      Otherwise say in the note that only part of the file was read.
    - `[uploaded file with no content in the export: <name>]` — add a line
      `Files not in the export: <name>` at the end of the section, so the human
      knows to fetch it.
- For `owner: shared`, preserve any section not traceable to these conversations.
- Frontmatter: append to `sources:` and `ingested_uuids:` — never replace either
  list. Each `sources:` entry is `path: <source>` plus `sha256: <sha>`, both
  given above. Each `ingested_uuids:` entry is the uuid, `message_count:
  <total_messages>`, and `last_ingested:` set to now. For action `new`, also
  write `files_ingested: true`. For action `update`, do not add or change
  `files_ingested` — an update reads only the new messages.
  Write `message_count` exactly as `total_messages` is given. Do not count the
  messages yourself and do not derive the value — a wrong count makes the next
  run either skip real messages or ingest the same ones twice.
  For a uuid already in `ingested_uuids:`, update `message_count` and
  `last_ingested` on that entry in place. Set `updated:` to now (UTC, ISO-8601).
  On a new note, set `owner: shared`.
- `summary:` rules, by case:
    - New note: write one, 200 characters or less.
    - Existing note with no `summary:`: add one.
    - Existing note that already has one: leave it exactly as it is, even if it
      is over length and even if your new content makes it stale. Say so in
      `design_log` and let the human decide.

WRITE ORDER — this matters more than it looks. Write in this sequence:
  1. the body content
  2. the `ingested_uuids:` entry
  3. the `sources:` entry with the sha256, LAST
The sha256 is the claim "this source is fully absorbed." Written first, a note
that dies mid-write asserts more than it holds, and the next run reads the whole
export as already ingested. Written last, a dead write asserts less than it
holds, which the next run detects and repairs. Prefer one Write call for a new
note; use this order whenever you make more than one edit.
- If the note does not exist, create it. Name it `<Folder Prefix> - <Topic>.md`,
  where the prefix is the destination folder name.

Do not do any of the following:
- Write to `_sources/` or to `_meta/` — both are off limits.
- Touch any note other than the target.
- Regenerate an index.
- Run any `git` command.

Return this JSON as the last thing in your report, and nothing after it:
{"note": "<path>", "created": <bool>, "uuids": [{"uuid": "...", "messages": N}],
 "design_log": "<one sentence, or null if the routing was routine>",
 "status": "ok" | "failed", "error": "<text, or null>"}
```

**Collect the results.** Read each subagent's JSON. Then, in this session:

1. Append every non-null `design_log` line to `_meta/design-log.md` (§7).
2. Apply §6a and §6b — ignored UUIDs and the last-ingest marker.
3. Carry every `status: failed` item into the run summary by name (§8). Do not retry it silently.

> **A failed report does not mean an unwritten note.** Measured on 2026-08-09: an agent wrote a complete, correct note — right sha256, right `message_count`, full body — and then died before returning its JSON. Before you retry any failed group, read the target note and check whether the uuid is already in `ingested_uuids:`. If it is, the work is done: count it as ingested, not failed, and do not dispatch again. A blind retry duplicates content.
>
> Check the other direction too. A note whose body is missing but whose `sources:` already carries the sha256 is the partial-write state from a dead agent. Revert that note or finish it by hand — do not leave it, because it makes the export read as fully absorbed.

> **Do not advance the watermark past a failure without saying so.** §6b fires on any successful ingest, so a run with one failed group still moves the marker. Name the failed items in the summary so that the user knows to re-run.

### 3. Ingesting a full conversation into a matched note

> This section and §4 describe the inline path. A subagent performs the same work under §2b, driven by the prompt template rather than by reading this file.

First, fetch the full conversation text:

```bash
python3 .agents/skills/obsidian-ingest/extract_conversations.py <file> extract <uuid>
```

Paraphrase claims from all assistant messages. Use human messages as context for what topic each assistant response addresses.

Apply provenance markers:
- No marker = paraphrased or extracted directly from the conversation
- `^[inferred]` = synthesis or conclusion beyond what the conversation states
- `^[ambiguous]` = conversation is unclear or self-contradictory on a point

Structure the note with H2 headings per major topic cluster in the conversation. Do not transcribe verbatim. Paraphrase tight — load-bearing claims and decisions only, not a blow-by-blow narration of the conversation's exploratory back-and-forth. Complete, not exhaustive.

The extract also prints file content: attached files under their message, and files Claude created at the end, rebuilt through their edits. Follow the "Files" rule in the §2b subagent prompt template. It applies inline too. On a `new` conversation, write `files_ingested: true` on its `ingested_uuids:` entry.

### 3b. Backfilling files into already-ingested conversations

Before 2026-09-14, extraction read only message text and dropped every file. Those conversations still read `unchanged`, so a normal run never returns to them. Run this only when the human asks for it.

1. List the backlog. The output is JSON, one row per conversation, with its note, source, and each file's name, kind, and size:
   ```bash
   python3 .agents/skills/obsidian-ingest/extract_conversations.py files-backlog
   ```
2. Show the human the list with sizes, and let them pick. Do not backfill everything by default. File text is about 670k characters across the whole backlog.
3. For each pick, check the note owner first (§5). Then read only the files:
   ```bash
   python3 .agents/skills/obsidian-ingest/extract_conversations.py <source> files <uuid>
   ```
4. Append one section at the bottom of the note, under the same "Files" rules:
   ```markdown
   ## Files from the conversation — YYYY-MM-DD

   <!-- source: <source>, uuid: <uuid>, files backfill -->
   ```
   If the note body already covers a file's content, write one line that says so. Do not repeat it.
5. On that uuid's `ingested_uuids:` entry, add `files_ingested: true` and set `updated:`. Do not change `message_count`, `last_ingested`, or `sources:`.

A conversation with only no-content uploads still appears in the backlog. Add the `Files not in the export:` line and set the flag.

### 4. Update Section Format (for grown conversations)

When `message_count` has increased (action = `update`), fetch only the new messages using the stored count as the offset:

```bash
python3 .agents/skills/obsidian-ingest/extract_conversations.py <file> extract <uuid> --from <stored_count>
```

Then append:

```markdown
## Update — YYYY-MM-DD

<!-- source: _sources/<filename>, uuid: <uuid>, messages: <old_count>–<new_count> -->

<paraphrased content from new messages with provenance markers>
```

Apply the same provenance rules as full ingestion. Append the Update section at the bottom of the note.

### 5. Owner enforcement

> Check owner before any write operation — this is a precondition, not a post-step.

Same as `obsidian-ingest` main skill:

| `owner` | Action |
|---|---|
| `human` | **Stop.** Print a diff of what you would have added. Do not write. Tell user to merge manually. |
| `agent` | Update freely. |
| `shared` | Update freely. Preserve sections not traceable to the source conversation. |

### 6. Update frontmatter after each write

Append to `sources:` — never replace:
```yaml
sources:
  - path: _sources/<filename>
    sha256: <sha256 from `extract_conversations.py <filename> sha256` — the inner export content for a .zip, the file itself for a bare .json>
```

Append to `ingested_uuids:` — never replace:
```yaml
ingested_uuids:
  - uuid: <conversation uuid>
    message_count: <len(chat_messages)>
    last_ingested: <YYYY-MM-DDTHH:MM:SSZ>
    files_ingested: true   # the conversation's files are in the note (§3, §3b); `skipped` = the human chose not to backfill
```

> **For a UUID already present in `ingested_uuids:`:** update `message_count` and `last_ingested` on the existing entry in place. The "never replace" rule prohibits overwriting the entire list, not field-level updates to an existing entry.

**Backward compatibility:** if `sources:` contains flat strings (e.g., `- _sources/foo.md`), upgrade them to `path/sha256` objects only on notes you are already writing to. Never touch notes solely to upgrade their frontmatter.

### 6a. Persist ignored UUIDs

After all conversations are processed, if any were marked "ignore permanently" in §2a, append them to `_meta/ignored-uuids.md`. Create the file if it doesn't exist.

```yaml
---
owner: agent
summary: UUIDs of Claude conversations permanently excluded from vault ingest.
updated: <YYYY-MM-DDTHH:MM:SSZ>
---

# Ignored Conversations
```

Append each newly ignored conversation:
```yaml
ignored_uuids:
  - uuid: <uuid>
    name: <conversation name>
    ignored_at: <YYYY-MM-DDTHH:MM:SSZ>
```

If the file already has an `ignored_uuids:` list, append to it — never replace. Preserve existing entries.

### 6b. Persist last-ingest marker

**Only if ≥1 conversation was ingested this run** (at least one note was created or had messages appended). If the run ingested nothing — all conversations unchanged, or only declined/ignored — **do not touch this file**; leave the previous marker in place.

> **Why this is deliberately narrow:** this marker tells the user the start date for their next Claude export pull. It is a *conversation-ingest watermark*, not a general "last run" timestamp. A document-only ingest run (`.md`/`.html`/`.pdf` via entry-point §5) writes notes but must **never** update it — doing so would advance the watermark past conversations that were never ingested, and the next export pull would silently skip them. The asymmetry is the feature; do not "fix" it.

Overwrite `_meta/last-ingest.md` (create if absent) with the run timestamp in **local time**. This file records only *when* the vault was last ingested — no per-conversation detail. Regenerate the whole file each qualifying run; never append.

Get the local timestamp from the machine — do not hand-compute it:
```bash
date '+%Y-%m-%dT%H:%M:%S%:z'   # run: value, e.g. 2026-07-05T18:19:53-05:00
date '+%Y-%m-%d %H:%M %Z'      # body line, e.g. 2026-07-05 18:19 CDT
```

```yaml
---
owner: agent
summary: Timestamp of the most recent Claude chat ingest run.
run: <YYYY-MM-DDTHH:MM:SS±HH:MM>
---

# Last Ingest

<YYYY-MM-DD HH:MM ZZZ>
```

`run:` is the machine-readable ISO-8601 timestamp **with local offset**; the body line is the human-readable local time (with zone abbreviation) the `--status` flag prints. Both are the same instant.

Writing this marker is **non-fatal**: the ingest already succeeded (note frontmatter is the source of truth). If the write fails, warn and continue.

### 7. Design log

Append one dated sentence to `_meta/design-log.md` for non-obvious decisions:
- Routing a conversation to an unexpected note
- User resolved an ambiguous topic match (chose between candidates)
- Owner conflict

Routine ingestions (clear match, no conflict) do not need a log entry.

### 8. Build and return run summary data

> New notes may be created, but only via Option 2 of §2a interactive routing where the user supplies the folder and name. Never create one from a guess.

Return to the entry point:
- Count of notes updated (with message delta)
- Count of conversations skipped (unchanged)
- Count of conversations skipped (ignored permanently)
- Count of new conversations routed interactively
- List of declined conversations: `[{name, uuid}]`
- List of owner conflicts: `[{name, uuid, note}]` — targets with `owner: human`, held back in §2b
- List of failures: `[{note, error}]` — groups whose subagent returned `status: failed`
- Execution path: `inline` or `parallel (<N> subagents)`

Every conversation ends in exactly one of these states — updated, unchanged, ignored, routed, declined, blocked by owner, or failed. There is no "unmatched" bucket: a conversation with no topic match goes through §2a and comes out routed, ignored, or declined.

## Common Mistakes

- **Auto-creating a note for an unmatched conversation** — routing is the user's call. Take it through §2a and let them pick the folder and name; never infer a destination and write it.
- **Transcribing verbatim assistant responses** — extract and paraphrase. The note is a knowledge artifact, not a chat log.
- **Dropping provenance markers** — ugly markers are the audit trail. Never remove them for prose flow.
- **Writing anything to `_sources/`** — read only, always.
- **Replacing `ingested_uuids:` or `sources:` lists** — always append.
- **Asking about unmatched conversations one at a time** — §2a requires batched `AskUserQuestion` calls, four questions per call. Sixteen separate prompts is the failure this skill exists to prevent.
- **Extracting conversations before routing is settled** — an extract costs about 16k tokens and stays in context. Classify, route, then decide the execution path, then extract.
- **Dispatching a small group** — a subagent pays roughly 33k of cold start whether the conversation holds 4 messages or 46. Groups at or below `MIN_DISPATCH_MESSAGES` write inline, always, no matter how large the run is. On 2026-09-04 seven such groups took 45% of the run's tokens for 20% of its content.
- **Counting small groups toward the §2b threshold** — the floor is applied first. A run of eight 4-message conversations holds zero dispatchable groups and stays entirely inline.
- **Re-dispatching a small group on retry** — the 2026-09-04 MoE retry spent 55,741 tokens on 6 messages. A failed small group is rewritten inline.
- **Dispatching subagents one after another** — put every `Agent` call in one turn. Serial dispatch pays the coordination cost and keeps the wall-clock wait.
- **Dispatching two subagents at one note** — group work items by target note first. Two writers on one note's frontmatter lose each other's appends.
- **Dispatching a note with `owner: human`** — the owner rule needs a diff shown to the user, and a subagent's report never reaches them. Filter these out in §2b and handle them in the main session.
- **Dispatching without `sha256` or `total_messages`** — the subagent writes both fields and can derive neither. A missing `sha256` makes the next inventory read the source as `untracked` and re-ingest it. A guessed `message_count` makes the next `classify` mis-read the conversation.
- **Treating a target that is missing from the `owners` map as writable** — the map covers only the `writable_roots` in `_meta/vault-config.yml`. A folder outside that list can be entirely `owner: human`. Read the frontmatter instead of assuming.
- **Letting a subagent write to `_meta/`** — design-log lines come back as text and the main session appends them. Concurrent appends to a shared file lose writes.
