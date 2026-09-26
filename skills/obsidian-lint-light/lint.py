#!/usr/bin/env python3
"""
obsidian-lint-light — read-only audit of an Obsidian vault.

Run by the obsidian-lint-light skill. Walks up from this script's location
to find the vault root, then audits every .md note in scope (excluding
_sources/ and index.md files).

Usage:
    python3 lint.py [scope] [--rebuild-indexes]

    scope               vault-relative project path; omit for full vault
    --rebuild-indexes   regenerate per-folder and master index.md after audit

Exit code is always 0; issues are reported, not raised. Output: a timestamped
report at <vault>/_meta/lint-report-<YYYY-MM-DD-HHMMSS>.md plus a one-line
terminal summary.
"""

import argparse
import hashlib
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.stderr.write("PyYAML required. Install with: pip install pyyaml\n")
    sys.exit(2)


def index_folder_for(folder_relpath):
    """Map a note's folder to the folder whose index.md should list it."""
    for root in INDEX_ROLLUP_ROOTS:
        if folder_relpath == root or folder_relpath.startswith(root + os.sep):
            return root
    return folder_relpath


# ----- Obsidian CLI helpers ---------------------------------------------------

_cli_available = None  # cached after first check


def _check_cli() -> bool:
    global _cli_available
    if _cli_available is not None:
        return _cli_available
    try:
        r = subprocess.run(["obsidian", "vault"], capture_output=True, timeout=10)
        _cli_available = True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        _cli_available = False
    return _cli_available


def _run_obsidian(args: list[str]) -> str | None:
    """Run `obsidian <args>` and return stdout, or None on failure."""
    if not _check_cli():
        return None
    try:
        r = subprocess.run(
            ["obsidian"] + args, capture_output=True, text=True, timeout=30
        )
        return r.stdout if r.returncode == 0 else None
    except Exception:
        return None


def get_orphan_notes_cli() -> list[str] | None:
    """Return vault-wide list of orphan note paths via CLI, or None if unavailable.
    Scope filtering is done on the Python side after intersection with audited notes."""
    out = _run_obsidian(["orphans"])
    if out is None:
        return None
    return [line.strip() for line in out.splitlines() if line.strip()]


# ----- Vault root discovery ---------------------------------------------------

def find_vault_root():
    """Walk up from this script's location to find AGENTS.md or CLAUDE.md next to _meta/."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if ((parent / 'AGENTS.md').exists() or (parent / 'CLAUDE.md').exists()) and (parent / '_meta').is_dir():
            return parent
    raise SystemExit('Could not locate vault root (no AGENTS.md or CLAUDE.md + _meta found above this script)')


def load_vault_config(vault):
    """Read _meta/vault-config.yml: the folder lists that the audit and the index rebuild use."""
    path = vault / '_meta' / 'vault-config.yml'
    if not path.is_file():
        raise SystemExit(f'Missing {path}. It lists the folders to audit. '
                         'See audited_roots and index_rollup_roots in the package vault/_meta/.')
    try:
        data = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
    except yaml.YAMLError as e:
        raise SystemExit(f'{path} is not valid YAML: {e}')
    if not isinstance(data, dict):
        raise SystemExit(f'{path} must be a YAML mapping')
    config = {}
    for key in ('audited_roots', 'index_rollup_roots'):
        value = data.get(key)
        if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
            raise SystemExit(f'{path}: `{key}` must be a list of folder names')
        config[key] = value
    return config


VAULT = find_vault_root()
CONFIG = load_vault_config(VAULT)
# Top-level folders the audit walks.
ALLOWED_TOP = tuple(CONFIG['audited_roots'])
# Vault-relative folders whose descendants share ONE index at the root instead of
# each getting their own. For scaffold trees where every subfolder holds a single
# note (e.g. one folder per game), per-folder indexes would be pure clutter — an
# index listing one note says nothing the note doesn't. Notes living directly in
# the root itself are indexed there as usual.
INDEX_ROLLUP_ROOTS = CONFIG['index_rollup_roots']


# ----- Frontmatter / body parsing --------------------------------------------

def parse_frontmatter(path):
    """Returns (data_dict_or_None, error_str_or_None)."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            text = f.read()
    except Exception as e:
        return None, f'read error: {e}'
    if not text.startswith('---\n'):
        return {}, None
    end = text.find('\n---\n', 4)
    if end < 0:
        return None, 'unterminated frontmatter (missing closing ---)'
    fm_text = text[4:end]
    try:
        data = yaml.safe_load(fm_text)
        if data is None:
            return {}, None
        if not isinstance(data, dict):
            return None, f'frontmatter is not a YAML mapping (got {type(data).__name__})'
        return data, None
    except yaml.YAMLError as e:
        return None, f'YAML parse error: {e}'


def read_body(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            text = f.read()
    except Exception:
        return ''
    if text.startswith('---\n'):
        end = text.find('\n---\n', 4)
        if end >= 0:
            return text[end + 5:]
    return text


def _load_export_content_bytes():
    """Borrow the ingest skill's zip-aware hashing so lint and ingest agree.

    A Claude export `.zip` is hashed by its *inner* export member, not the outer
    zip bytes — member mod-times, compression and ordering differ between
    downloads of an identical export. Notes record that inner hash, so hashing
    the packaging here reports every `.zip` source as permanently drifted.

    Located by scanning sibling skill dirs rather than a hardcoded name, so
    renaming the ingest skill does not silently reintroduce the false drift.
    """
    import importlib.util
    for cand in sorted(Path(__file__).resolve().parent.parent.glob('*/extract_conversations.py')):
        try:
            spec = importlib.util.spec_from_file_location('_ingest_extract', cand)
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            fn = getattr(mod, 'export_content_bytes', None)
            if fn is not None:
                return fn
        except Exception:
            continue
    return None


_export_content_bytes = _load_export_content_bytes()


def sha256_file(path):
    try:
        if str(path).lower().endswith('.zip'):
            if _export_content_bytes is None:
                # Can't reach the ingest hasher — outer-byte hashing would emit a
                # bogus drift entry, so report "unknown" and skip the drift check.
                return None
            return hashlib.sha256(_export_content_bytes(Path(path))).hexdigest()
        h = hashlib.sha256()
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(65536), b''):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def first_h1(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                m = re.match(r'^#\s+(.+?)\s*$', line)
                if m:
                    return m.group(1).strip()
    except Exception:
        pass
    return None


def best_summary(abs_path):
    fm, _ = parse_frontmatter(abs_path)
    fm = fm or {}
    s = fm.get('summary')
    if isinstance(s, str) and s.strip():
        return s.strip()
    return first_h1(abs_path)


def title_from_path(abs_path):
    return os.path.splitext(os.path.basename(abs_path))[0]


# ----- Vault traversal --------------------------------------------------------

def collect_notes(scope_rel=None):
    """Yield (relpath, abspath) for every audited .md note in scope.
    Skips _-prefixed directories (e.g. _sources/, _reference/) — those are reference
    content that shouldn't be audited or indexed."""
    if scope_rel:
        roots = [VAULT / scope_rel]
    else:
        roots = [VAULT / top for top in ALLOWED_TOP]
    for root in roots:
        if not root.is_dir():
            continue
        for r, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if not d.startswith('_')]
            for fn in files:
                if not fn.endswith('.md') or fn == 'index.md':
                    continue
                ab = os.path.join(r, fn)
                yield os.path.relpath(ab, VAULT), ab


def collect_reference_titles(scope_rel=None):
    """Yield filename stems of .md files inside _-prefixed reference folders
    (e.g. _reference/). These are wikilink targets but not audited content — the
    rest of the lint logic stays away from them."""
    if scope_rel:
        roots = [VAULT / scope_rel]
    else:
        roots = [VAULT / top for top in ALLOWED_TOP]
    for root in roots:
        if not root.is_dir():
            continue
        # Walk only into _-prefixed dirs (and their children)
        for top_entry in os.listdir(root):
            full = os.path.join(root, top_entry)
            if os.path.isdir(full) and top_entry.startswith('_') and top_entry != '_sources':
                for r, _, files in os.walk(full):
                    for fn in files:
                        if fn.endswith('.md') and fn != 'index.md':
                            yield os.path.splitext(fn)[0]
            elif os.path.isdir(full) and not top_entry.startswith('_'):
                # Recurse into normal dirs to find nested _-prefixed folders
                for r, dirs, _ in os.walk(full):
                    for d in dirs:
                        if d.startswith('_') and d != '_sources':
                            for _, _, ff in os.walk(os.path.join(r, d)):
                                for fn in ff:
                                    if fn.endswith('.md') and fn != 'index.md':
                                        yield os.path.splitext(fn)[0]


def collect_source_files(scope_rel=None):
    """Yield (project_folder_relpath, source_filename, abs_path) for every _sources/* file in scope.

    Vault-root _sources/ (VAULT/_sources/) is always included and yielded with
    project_folder_relpath='' (empty string). These are shared multi-project sources
    placed there by vault-root ingest runs.
    """
    # Vault-root _sources/ — always included regardless of scope
    vault_root_src = VAULT / '_sources'
    if vault_root_src.is_dir():
        for fn in os.listdir(vault_root_src):
            if not fn.startswith('.'):
                yield '', fn, str(vault_root_src / fn)

    # Per-project _sources/ directories
    if scope_rel:
        roots = [VAULT / scope_rel]
    else:
        roots = [VAULT / top for top in ALLOWED_TOP]
    for root in roots:
        if not root.is_dir():
            continue
        for r, _, files in os.walk(root):
            if os.path.basename(r) == '_sources':
                project_folder = os.path.relpath(os.path.dirname(r), VAULT)
                for fn in files:
                    if fn.startswith('.'):
                        continue
                    yield project_folder, fn, os.path.join(r, fn)


# ----- Main audit -------------------------------------------------------------

# matches [[target]], [[target|alias]], [[target#heading]], [[target^block]]
WIKILINK_RE = re.compile(r'\[\[([^\]\n|#^]+?)(?:[#^][^\]\n|]+)?(?:\|[^\]\n]+)?\]\]')


def strip_code_preserving_offsets(text):
    """Blank out fenced code blocks and inline code spans so wikilink/provenance
    scanners ignore them, while preserving newlines so reported line numbers
    still match the original body."""
    def blank(m):
        return ''.join(c if c == '\n' else ' ' for c in m.group(0))
    # Fenced blocks first (greedy across newlines)
    text = re.sub(r'```.*?```', blank, text, flags=re.DOTALL)
    # Inline code spans — don't cross newlines
    text = re.sub(r'`[^`\n]*`', blank, text)
    return text


def collect_link_targets():
    """Every file a wikilink may legitimately point at, in every shape Obsidian
    accepts: a bare stem `[[note]]`, a vault-relative path `[[folder/note]]`,
    and a name with extension `[[file.base]]`.

    This deliberately walks wider than `collect_notes`. `index.md` files and
    `_`-prefixed directories are excluded from auditing, but notes still link to
    them, so they must resolve here."""
    skip_dirs = {'.git', '.obsidian', '.trash', '.stversions', '.stfolder',
                 '_sources', '__pycache__'}
    exts = {'.md', '.base', '.canvas'}
    targets = set()
    for r, dirs, files in os.walk(VAULT):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fn in files:
            stem, ext = os.path.splitext(fn)
            if ext not in exts:
                continue
            rel = os.path.relpath(os.path.join(r, fn), VAULT).replace(os.sep, '/')
            targets.add(stem)                      # [[note]]
            targets.add(fn)                        # [[file.base]]
            targets.add(rel.rsplit('.', 1)[0])     # [[folder/note]]
            targets.add(rel)                       # [[folder/file.base]]
    return targets


def audit(scope_rel=None):
    notes = list(collect_notes(scope_rel))
    source_files = list(collect_source_files(scope_rel))
    # Wikilink resolution targets include both audited notes AND reference-only
    # files (for example chapters under Reference/_reference/) so [[chapter10#5]]
    # resolves even though that file lives in a _-prefixed reference folder.
    note_titles = collect_link_targets()
    note_titles |= {title_from_path(ab) for _, ab in notes}
    note_titles |= set(collect_reference_titles(scope_rel))

    issues = {
        'frontmatter': [],
        'broken_wikilinks': [],
        'stale_sources': [],
        'drifted_sources': [],
        'retired_sources': [],
        'orphan_sources': [],
        'orphan_notes': [],
        'provenance_drift': [],
        'index_drift': [],
    }
    inferred_claims = []
    referenced_sources = {}  # project_folder -> set of source filenames

    for rel, ab in notes:
        fm, fm_err = parse_frontmatter(ab)
        if fm_err:
            issues['frontmatter'].append((rel, fm_err))
            continue
        fm = fm or {}

        # owner
        owner = fm.get('owner')
        if owner is not None and owner not in ('human', 'agent', 'shared'):
            issues['frontmatter'].append((rel, f'unknown owner value: {owner!r}'))

        # updated timestamp
        upd = fm.get('updated')
        if upd is not None:
            if isinstance(upd, str):
                try:
                    s = upd.replace('Z', '+00:00') if upd.endswith('Z') else upd
                    datetime.fromisoformat(s)
                except Exception:
                    issues['frontmatter'].append((rel, f'unparseable updated timestamp: {upd!r}'))
            elif not hasattr(upd, 'isoformat'):
                issues['frontmatter'].append((rel, f'updated should be ISO 8601 string, got {type(upd).__name__}'))

        # sources
        project_folder = os.path.dirname(rel)
        sources = fm.get('sources') or []
        if not isinstance(sources, list):
            issues['frontmatter'].append((rel, f'sources is not a list: {type(sources).__name__}'))
            sources = []
        referenced_sources.setdefault(project_folder, set())
        for entry in sources:
            if isinstance(entry, str):
                src_path, sha = entry, None
            elif isinstance(entry, dict):
                src_path = entry.get('path')
                sha = entry.get('sha256')
                if not src_path:
                    issues['frontmatter'].append((rel, f'sources entry missing path: {entry!r}'))
                    continue
                if entry.get('retired'):
                    # The source file was deliberately deleted after ingest (see
                    # obsidian-ingest/retire_sources.py). `sources:` is append-only,
                    # so the entry stays as the lineage record — but there is no
                    # file left to find or hash, which would otherwise report as a
                    # permanent stale/drift issue. Counted so the deletion stays
                    # auditable instead of silently vanishing.
                    issues['retired_sources'].append((rel, src_path))
                    continue
            else:
                issues['frontmatter'].append((rel, f'sources entry not str or dict: {type(entry).__name__}'))
                continue
            ab_src = os.path.join(VAULT, project_folder, src_path)
            # Fallback: vault-root _sources/ for multi-project ingest runs
            # (e.g. VAULT/_sources/conversations_1.json referenced from any project note)
            src_owner = project_folder
            if not os.path.exists(ab_src):
                ab_src_vaultroot = os.path.join(VAULT, src_path)
                if os.path.exists(ab_src_vaultroot):
                    ab_src = ab_src_vaultroot
                    src_owner = ''  # track under vault-root key
            referenced_sources.setdefault(src_owner, set())
            referenced_sources[src_owner].add(os.path.basename(src_path))
            if not os.path.exists(ab_src):
                issues['stale_sources'].append((rel, src_path))
                continue
            if sha:
                actual = sha256_file(ab_src)
                if actual and actual != sha:
                    issues['drifted_sources'].append((rel, src_path, sha, actual))

        # ingested_uuids
        iu = fm.get('ingested_uuids')
        if iu is not None:
            if not isinstance(iu, list):
                issues['frontmatter'].append((rel, f'ingested_uuids is not a list: {type(iu).__name__}'))
            else:
                for i, e in enumerate(iu):
                    if not isinstance(e, dict):
                        issues['frontmatter'].append((rel, f'ingested_uuids[{i}] not a dict'))
                        continue
                    for req in ('uuid', 'message_count', 'last_ingested'):
                        if req not in e:
                            issues['frontmatter'].append((rel, f'ingested_uuids[{i}] missing {req}'))

        # body checks (strip code spans/blocks first so wikilinks and provenance
        # markers inside `code` or ```fenced``` blocks are not flagged)
        body = read_body(ab)
        body_clean = strip_code_preserving_offsets(body)
        for m in WIKILINK_RE.finditer(body_clean):
            target = m.group(1).strip()
            if not target:
                continue
            if target not in note_titles:
                line_no = body_clean[:m.start()].count('\n') + 1
                issues['broken_wikilinks'].append((rel, target, line_no))

        # provenance drift
        prov = fm.get('provenance')
        if isinstance(prov, dict):
            decl_inferred = prov.get('inferred', 0) or 0
            decl_amb = prov.get('ambiguous', 0) or 0
            paragraphs = [p for p in body.split('\n\n') if p.strip()]
            denom = max(len(paragraphs), 1)
            n_inf = len(re.findall(r'\^\[inferred\]', body_clean))
            n_amb = len(re.findall(r'\^\[ambiguous\]', body_clean))
            obs_inf = n_inf / denom
            obs_amb = n_amb / denom
            if abs(obs_inf - decl_inferred) > 0.10:
                issues['provenance_drift'].append((rel, 'inferred', decl_inferred, round(obs_inf, 2)))
            if abs(obs_amb - decl_amb) > 0.10:
                issues['provenance_drift'].append((rel, 'ambiguous', decl_amb, round(obs_amb, 2)))

        # inferred-claim audit list
        for m in re.finditer(r'([^.\n]+\^\[(?:inferred|ambiguous)\][^.\n]*\.?)', body_clean):
            snippet = m.group(1).strip()
            if len(snippet) > 200:
                snippet = snippet[:200] + '...'
            inferred_claims.append((rel, snippet))

    # orphan sources
    for project_folder, fn, ab_src in source_files:
        refs = referenced_sources.get(project_folder, set())
        if fn not in refs:
            issues['orphan_sources'].append(os.path.relpath(ab_src, VAULT))

    # orphan notes (no incoming wikilinks) — Obsidian CLI only; skipped if unavailable
    orphan_paths = get_orphan_notes_cli()
    if orphan_paths is not None:
        note_abspaths = {ab for _, ab in notes}
        for p in orphan_paths:
            abs_p = str(VAULT / p) if not os.path.isabs(p) else p
            if abs_p in note_abspaths:
                issues['orphan_notes'].append(p)

    return notes, source_files, issues, inferred_claims, referenced_sources


def check_index_drift(notes):
    """Compare existing index.md per folder vs current note state. Returns list of (folder, issue)."""
    issues = []
    folders = {}
    for rel, ab in notes:
        # same roll-up the rebuild uses, or every rolled-up subfolder reports a
        # missing index.md that is never supposed to exist
        folders.setdefault(index_folder_for(os.path.dirname(rel)), []).append((rel, ab))

    for folder, fnotes in folders.items():
        idx_path = os.path.join(VAULT, folder, 'index.md')
        if not os.path.exists(idx_path):
            issues.append((folder, 'index.md missing entirely'))
            continue
        try:
            with open(idx_path, 'r', encoding='utf-8') as f:
                idx_text = f.read()
        except Exception as e:
            issues.append((folder, f'cannot read index.md: {e}'))
            continue

        indexed_titles = set()
        indexed_summaries = {}
        for line in idx_text.split('\n'):
            m = re.match(r'^- \[\[([^\]|#^]+)\]\](?:\s+—\s+(.+))?$', line.strip())
            if m:
                t = m.group(1).strip()
                indexed_titles.add(t)
                if m.group(2):
                    indexed_summaries[t] = m.group(2).strip()

        actual_titles = {title_from_path(ab) for _, ab in fnotes}
        for t in sorted(actual_titles - indexed_titles):
            issues.append((folder, f'missing entry: {t}'))
        for t in sorted(indexed_titles - actual_titles):
            issues.append((folder, f'stale entry (note no longer exists): {t}'))
        for rel, ab in fnotes:
            title = title_from_path(ab)
            expected = best_summary(ab)
            actual = indexed_summaries.get(title)
            if expected and actual and expected != actual:
                issues.append((folder, f'summary mismatch for {title}'))

    return issues


# ----- Index regeneration (opt-in) -------------------------------------------

def write_index(folder_relpath, fnotes, ts_iso):
    folder_abs = os.path.join(VAULT, folder_relpath) if folder_relpath else str(VAULT)
    folder_name = os.path.basename(folder_relpath) if folder_relpath else 'Vault'
    notes_sorted = sorted(fnotes, key=lambda x: title_from_path(x[1]).lower())

    lines = [
        '---',
        'owner: agent',
        f'updated: {ts_iso}',
        f'summary: Index of notes in {folder_relpath} (regenerated by obsidian-ingest / obsidian-lint-light).',
        '---',
        '',
        f'# {folder_name} — Index',
        '',
    ]
    for _, ab in notes_sorted:
        title = title_from_path(ab)
        summary = best_summary(ab)
        lines.append(f'- [[{title}]] — {summary}' if summary else f'- [[{title}]]')
    lines.append('')

    idx_path = os.path.join(folder_abs, 'index.md')
    with open(idx_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    return idx_path


def write_master_index(notes, ts_iso):
    by_folder = {}
    for rel, ab in notes:
        # roll up scaffold trees so they get one section, not one per subfolder
        by_folder.setdefault(index_folder_for(os.path.dirname(rel)), []).append((rel, ab))

    lines = [
        '---',
        'owner: agent',
        f'updated: {ts_iso}',
        'summary: Vault-wide master index of all derived notes (regenerated by obsidian-ingest and obsidian-lint-light).',
        '---',
        '',
        '# Vault Master Index',
        '',
    ]
    total = 0
    for folder in sorted(by_folder):
        fnotes = sorted(by_folder[folder], key=lambda x: title_from_path(x[1]).lower())
        if not fnotes:
            continue
        lines.append(f'## {folder}')
        lines.append('')
        for rel, ab in fnotes:
            title = title_from_path(ab)
            summary = best_summary(ab)
            lines.append(f'- [[{title}]] — {summary}' if summary else f'- [[{title}]]')
            total += 1
        lines.append('')

    master_path = os.path.join(VAULT, '_meta', 'index.md')
    with open(master_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    return master_path, total


# ----- Report writing --------------------------------------------------------

def write_report(scope_rel, notes, source_files, issues, inferred_claims, rebuilt):
    ts_local = datetime.now(timezone.utc).strftime('%Y-%m-%d-%H%M%S')
    ts_iso = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    report_path = os.path.join(VAULT, '_meta', f'lint-report-{ts_local}.md')

    counts = {k: len(v) for k, v in issues.items()}
    # retired_sources is informational, not a defect — a retired entry is the
    # intended end state for a deliberately deleted source, so counting it would
    # mean the vault could never reach "0 issues" again.
    total = sum(v for k, v in counts.items() if k != 'retired_sources')
    scope_label = scope_rel or 'vault'

    lines = [
        '---',
        'owner: agent',
        f'generated: {ts_iso}',
        f'scope: {scope_label}',
        f'rebuilt_indexes: {str(rebuilt).lower()}',
        '---',
        '',
        f'# Lint Report — {ts_local}',
        '',
        f'**Scope:** {scope_label}',
        f'**Notes audited:** {len(notes)}',
        f'**Source files checked:** {len(source_files)}',
        '',
        '## Summary',
        '',
        f'- Broken wikilinks: {counts["broken_wikilinks"]}',
        f'- Stale sources (file missing): {counts["stale_sources"]}',
        f'- Drifted sources (sha256 mismatch): {counts["drifted_sources"]}',
        f'- Retired sources (intentionally deleted): {counts["retired_sources"]} _(informational)_',
        f'- Orphan source files: {counts["orphan_sources"]}',
        f'- Orphan notes (no incoming links): {counts["orphan_notes"]}' +
            ('' if counts["orphan_notes"] >= 0 and _check_cli() else ' _(Obsidian CLI not available — skipped)_'),
        f'- Frontmatter issues: {counts["frontmatter"]}',
        f'- Provenance drift flags: {counts["provenance_drift"]}',
    ]
    if not rebuilt:
        lines.append(f'- Index drift flags: {counts["index_drift"]}')
    lines.append('')

    def section(title, entries, fmt):
        lines.append(f'## {title}')
        lines.append('')
        if not entries:
            lines.append('_None._')
        else:
            for e in entries:
                lines.append(fmt(e))
        lines.append('')

    section('Broken Wikilinks', issues['broken_wikilinks'],
        lambda e: f'- `{e[0]}`: `[[{e[1]}]]` (line {e[2]})')
    section('Stale Sources', issues['stale_sources'],
        lambda e: f'- `{e[0]}` references `{e[1]}` — file does not exist')
    section('Drifted Sources', issues['drifted_sources'],
        lambda e: f'- `{e[0]}` references `{e[1]}` — sha256 changed (recorded `{e[2][:12]}…`, actual `{e[3][:12]}…`); re-ingest to refresh')
    section('Retired Sources', issues['retired_sources'],
        lambda e: f'- `{e[0]}` references `{e[1]}` — file deliberately deleted; hash retained as lineage')
    section('Orphan Source Files', issues['orphan_sources'],
        lambda e: f'- `{e}` — not referenced by any note in its project')
    if _check_cli():
        section('Orphan Notes (no incoming links)', issues['orphan_notes'],
            lambda e: f'- `{e}`')
    section('Frontmatter Issues', issues['frontmatter'],
        lambda e: f'- `{e[0]}`: {e[1]}')
    section('Provenance Drift', issues['provenance_drift'],
        lambda e: f'- `{e[0]}`: declared {e[1]}={e[2]}, observed≈{e[3]} (approximate)')
    if not rebuilt:
        section('Index Drift', issues['index_drift'],
            lambda e: f'- `{e[0]}/index.md`: {e[1]}')

    lines.append('## Inferred-Claim Audit List')
    lines.append('')
    lines.append('> Spot-check these synthesis claims for continued accuracy.')
    lines.append('')
    if not inferred_claims:
        lines.append('_None._')
    else:
        for rel, snippet in inferred_claims:
            lines.append(f'- `{rel}`: {snippet}')
    lines.append('')

    with open(report_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    return report_path, total, counts


# ----- Entrypoint ------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description='Read-only audit of an Obsidian vault.')
    p.add_argument('scope', nargs='?', default=None,
                   help='vault-relative project path; omit for full vault audit')
    p.add_argument('--rebuild-indexes', action='store_true',
                   help='regenerate per-folder and master index.md after audit')
    p.add_argument('--keep', type=int, default=5, metavar='N',
                   help='keep the N most recent lint reports; older ones are deleted '
                        'after writing the new one (default: 5; pass 0 to keep all)')
    args = p.parse_args()

    # Obsidian CLI preflight — non-fatal, degrades gracefully
    if not _check_cli():
        print(
            "NOTE: Obsidian CLI not available — orphan-notes check will be skipped.\n"
            "      Open Obsidian and ensure 'obsidian' is in your PATH for full audit.",
            file=sys.stderr,
        )

    # Validate scope
    if args.scope:
        scope_abs = VAULT / args.scope
        if not scope_abs.is_dir():
            sys.exit(f'Scope path does not exist or is not a directory: {args.scope}')

    notes, source_files, issues, inferred_claims, _ = audit(args.scope)

    project_paths = []
    if args.rebuild_indexes:
        ts_iso = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        # Regenerate per-folder indexes for every folder in the audit scope
        by_folder = {}
        for rel, ab in notes:
            by_folder.setdefault(index_folder_for(os.path.dirname(rel)), []).append((rel, ab))
        for folder, fnotes in sorted(by_folder.items()):
            project_paths.append(write_index(folder, fnotes, ts_iso))
        # Master index always reflects the full vault, not just the scope
        all_notes = list(collect_notes(None))
        write_master_index(all_notes, ts_iso)
    else:
        issues['index_drift'] = check_index_drift(notes)

    report_path, total, _ = write_report(
        args.scope, notes, source_files, issues, inferred_claims, args.rebuild_indexes)

    rel_report = os.path.relpath(report_path, VAULT)
    print(f'Lint complete — {args.scope or "vault"}. Issues: {total}. Report: {rel_report}')
    if args.rebuild_indexes:
        print(f'Rebuilt indexes: {len(project_paths)} folders + 1 master')

    # Prune older reports. Filenames are timestamp-based so alphabetical sort
    # equals chronological sort.
    if args.keep > 0:
        meta_dir = VAULT / '_meta'
        reports = sorted(meta_dir.glob('lint-report-*.md'))
        if len(reports) > args.keep:
            to_delete = reports[:-args.keep]
            for old in to_delete:
                old.unlink()
            print(f'Pruned {len(to_delete)} older report(s); keeping {args.keep} most recent.')


if __name__ == '__main__':
    main()
