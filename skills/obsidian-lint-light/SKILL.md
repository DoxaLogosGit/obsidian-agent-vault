---
name: obsidian-lint-light
description: Read-only audit of an Obsidian vault — broken wikilinks, orphan/stale sources, SHA256 drift, malformed frontmatter, provenance drift. Optionally rebuilds project and master indexes. Reports issues to _meta/lint-report-<timestamp>.md without auto-fixing anything else.
---

# obsidian-lint-light

Audit the vault for structural issues. Lint **reports**; it does not auto-fix content. The single exception is the optional index-rebuild step, which regenerates fully agent-managed `index.md` files from current note state.

## Implementation

The audit logic lives in [`lint.py`](./lint.py) — a checked-in script. Don't regenerate the workflow inline; invoke the script:

```bash
python3 .agents/skills/obsidian-lint-light/lint.py [scope] [--rebuild-indexes]
```

The script auto-discovers the vault root by walking up from its own location until it finds an `AGENTS.md` or `CLAUDE.md` next to a `_meta/` directory, so it works regardless of the current working directory. It reads `audited_roots` and `index_rollup_roots` from `_meta/vault-config.yml`.

## Inputs

- **Scope** (optional positional arg): vault-relative project path (e.g., `Projects/Example/Widget`). Omit for a full-vault audit.
- **`--rebuild-indexes`** (optional flag): also regenerate per-folder `index.md` files in scope plus the vault-wide `_meta/index.md` after the audit. Off by default.
- **`--keep N`** (optional, default `5`): after writing the new report, prune older `_meta/lint-report-*.md` files so only the `N` most recent remain. Pass `--keep 0` to disable pruning entirely.

## What It Checks

Per `.md` note (excluding any underscore-prefixed directory — `_sources/`, `_reference/`, etc. — and `index.md`). Notes in `_`-prefixed dirs are not audited but remain valid wikilink targets, so links *into* them (e.g. sermon → Bible verse) still resolve:

1. **Frontmatter validity** — parses as YAML; `owner` is `human`/`agent`/`shared`/absent; `sources:` entries are well-formed; `updated:` is ISO 8601; `ingested_uuids:` entries have `uuid`+`message_count`+`last_ingested`.
2. **Wikilinks** — every `[[Target]]`, `[[Target|Alias]]`, `[[Target#heading]]` resolves to a note in the vault (heading-only `[[#Section]]` is skipped).
3. **Source existence + SHA256 drift** — every `sources:` entry's file exists at the recorded path under the project's `_sources/`; if `sha256:` is recorded, it matches current file content. Drift is a re-ingest signal, not an error.
4. **Orphan source files** — every file in any `_sources/` is referenced by at least one note in the same project folder.
5. **Provenance drift** — if a note declares `provenance:` fractions, observed marker counts (`^[inferred]`, `^[ambiguous]`) over paragraphs are within 10% of declared. Code blocks are excluded. Reports are marked "approximate."
6. **Index drift** (only when `--rebuild-indexes` is NOT set) — each folder's `index.md` matches current note titles and `summary:` lines.
7. **Inferred-claim audit list** — every sentence containing `^[inferred]` or `^[ambiguous]` is collected for human spot-check.

## What It Optionally Writes

When `--rebuild-indexes` is passed:

- **Per-folder `<folder>/index.md`** for every folder in scope that contains at least one note. `owner: agent`, fully regenerated each run.
- **Vault-wide `_meta/index.md`** always reflects the entire vault (not just the scope), grouped by folder path.

Format and rules match step §7 of [`obsidian-ingest`](../obsidian-ingest/SKILL.md).

## Output

A timestamped report at `_meta/lint-report-<YYYY-MM-DD-HHMMSS>.md` containing summary counts, per-category issue lists, and the inferred-claim audit list. The script also prints a one-line terminal summary:

```
Lint complete — <scope>. Issues: <N>. Report: _meta/lint-report-<timestamp>.md
[Rebuilt indexes: <project-count> folders + 1 master]   ← only when --rebuild-indexes
```

## What This Skill Will Never Do

- Modify note content (frontmatter, body, anything inside a note)
- Modify or delete files in `_sources/`
- Auto-correct typos, malformed YAML, broken wikilinks, or any other flagged issue
- Rebuild indexes unless `--rebuild-indexes` is explicitly passed
- Touch `_meta/schema.md`, `_meta/design-log.md`, or any other vault metadata file beyond writing its own timestamped report (and indexes when opted in)

The contract: **report issues, let the human fix them**. Index regeneration is the only opt-in write, and only because indexes are fully agent-managed derived artifacts with no human authorship.

## When to Edit the Logic

Edit `lint.py` directly. Bug fixes, new check categories, or output-format changes belong there. This SKILL.md describes the contract; the script implements it. After editing, commit both files together so the contract and implementation stay in sync.
