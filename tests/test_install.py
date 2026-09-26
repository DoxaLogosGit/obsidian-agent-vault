import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import install  # noqa: E402

REAL_PKG = Path(__file__).resolve().parents[1]
CONFIG = """# comment
writable_roots: [Projects, Ideas, Reference]
audited_roots: [Projects, Ideas, Reference]
index_rollup_roots: []
"""


@pytest.fixture
def pkg(tmp_path):
    """A minimal package: vault/ with AGENTS.md and _meta/, skills/ with one skill."""
    root = tmp_path / "pkg"
    (root / "vault" / "_meta").mkdir(parents=True)
    (root / "vault" / "_sources").mkdir()
    (root / "vault" / "AGENTS.md").write_text("package rules\n")
    (root / "vault" / "CLAUDE.md").write_text("@AGENTS.md\n")
    (root / "vault" / "_meta" / "vault-config.yml").write_text(CONFIG)
    (root / "vault" / "_meta" / "schema.md").write_text("schema v1\n")
    (root / "skills" / "demo").mkdir(parents=True)
    (root / "skills" / "demo" / "SKILL.md").write_text("# Demo\n")
    return root


@pytest.fixture
def existing(tmp_path):
    """An existing vault with two note folders and one folder without notes."""
    vault = tmp_path / "mine"
    for folder, note in [("Notes", "a.md"), ("Work Log", "b.md")]:
        (vault / folder).mkdir(parents=True)
        (vault / folder / note).write_text("x\n")
    (vault / "Attachments").mkdir()
    (vault / "Attachments" / "pic.png").write_bytes(b"\0")
    (vault / ".obsidian").mkdir()
    (vault / ".obsidian" / "x.md").write_text("x\n")
    return vault


def block(text):
    return f"{install.START}\n{text.rstrip()}\n{install.END}\n"


# ----- new --------------------------------------------------------------------

def test_new_creates_vault_and_symlink(pkg, tmp_path):
    dest = tmp_path / "v"
    assert install.cmd_new(pkg, dest) == []
    assert (dest / "AGENTS.md").read_text() == block("package rules")
    assert (dest / ".agents" / "skills" / "demo" / "SKILL.md").is_file()
    link = dest / ".claude" / "skills"
    assert link.is_symlink() and os.readlink(link) == os.path.join("..", ".agents", "skills")


def test_new_refuses_nonempty_folder(pkg, tmp_path):
    dest = tmp_path / "v"
    dest.mkdir()
    (dest / "note.md").write_text("x")
    with pytest.raises(SystemExit):
        install.cmd_new(pkg, dest)


def test_symlink_failure_copies_skills(pkg, tmp_path, monkeypatch):
    def no_symlink(*args, **kwargs):
        raise OSError("symlinks not allowed")
    monkeypatch.setattr(install.os, "symlink", no_symlink)
    dest = tmp_path / "v"
    install.cmd_new(pkg, dest)
    link = dest / ".claude" / "skills"
    assert not link.is_symlink()
    assert (link / "demo" / "SKILL.md").is_file()


# ----- into: folder config ----------------------------------------------------

def test_detect_note_folders(existing):
    assert install.detect_note_folders(existing) == ["Notes", "Work Log"]


def test_into_writes_detected_folders_on_yes(pkg, existing):
    install.cmd_into(pkg, existing, confirm=True)
    text = (existing / "_meta" / "vault-config.yml").read_text()
    assert "writable_roots: [Notes, Work Log]" in text
    assert "audited_roots: [Notes, Work Log]" in text
    assert "index_rollup_roots: []" in text and "# comment" in text


def test_into_keeps_defaults_on_no(pkg, existing):
    install.cmd_into(pkg, existing, confirm=False)
    assert (existing / "_meta" / "vault-config.yml").read_text() == CONFIG


def test_into_never_touches_existing_config(pkg, existing):
    (existing / "_meta").mkdir()
    (existing / "_meta" / "vault-config.yml").write_text("writable_roots: [Mine]\n")
    install.cmd_into(pkg, existing, confirm=True)
    assert (existing / "_meta" / "vault-config.yml").read_text() == "writable_roots: [Mine]\n"


def test_prompt_eof_counts_as_no(pkg, existing, monkeypatch):
    def eof(_):
        raise EOFError
    monkeypatch.setattr("builtins.input", eof)
    install.cmd_into(pkg, existing)
    assert (existing / "_meta" / "vault-config.yml").read_text() == CONFIG


# ----- into: AGENTS.md and CLAUDE.md ------------------------------------------

def test_into_appends_rules_to_existing_agents_md(pkg, existing):
    (existing / "AGENTS.md").write_text("my rules\n")
    assert install.cmd_into(pkg, existing, confirm=False) == []
    text = (existing / "AGENTS.md").read_text()
    assert text.startswith("my rules\n") and text.endswith(block("package rules"))
    assert (existing / "AGENTS.md.bak").read_text() == "my rules\n"


def test_into_writes_wrapped_agents_md_when_missing(pkg, existing):
    install.cmd_into(pkg, existing, confirm=False)
    assert (existing / "AGENTS.md").read_text() == block("package rules")


def test_into_adds_import_to_existing_claude_md(pkg, existing):
    (existing / "CLAUDE.md").write_text("my claude notes")
    install.cmd_into(pkg, existing, confirm=False)
    assert (existing / "CLAUDE.md").read_text() == "my claude notes\n\n@AGENTS.md\n"
    assert (existing / "CLAUDE.md.bak").read_text() == "my claude notes"


def test_into_leaves_claude_md_with_import(pkg, existing):
    (existing / "CLAUDE.md").write_text("x\n@AGENTS.md\n")
    install.cmd_into(pkg, existing, confirm=False)
    assert (existing / "CLAUDE.md").read_text() == "x\n@AGENTS.md\n"
    assert not (existing / "CLAUDE.md.bak").exists()


def test_into_keeps_differing_meta_file_as_new(pkg, existing):
    (existing / "_meta").mkdir()
    (existing / "_meta" / "schema.md").write_text("my schema\n")
    conflicts = install.cmd_into(pkg, existing, confirm=False)
    assert conflicts == [existing / "_meta" / "schema.md.new"]
    assert (existing / "_meta" / "schema.md").read_text() == "my schema\n"


# ----- update -----------------------------------------------------------------

def test_update_replaces_package_skills_only(pkg, tmp_path):
    dest = tmp_path / "v"
    install.cmd_new(pkg, dest)
    (dest / ".agents" / "skills" / "demo" / "stale.md").write_text("old\n")
    (dest / ".agents" / "skills" / "mine").mkdir()
    (dest / ".agents" / "skills" / "mine" / "SKILL.md").write_text("# Mine\n")
    (pkg / "skills" / "demo" / "SKILL.md").write_text("# Demo v2\n")
    install.cmd_update(pkg, dest)
    assert (dest / ".agents" / "skills" / "demo" / "SKILL.md").read_text() == "# Demo v2\n"
    assert not (dest / ".agents" / "skills" / "demo" / "stale.md").exists()
    assert (dest / ".agents" / "skills" / "mine" / "SKILL.md").is_file()


def test_update_refreshes_marked_block_and_keeps_config(pkg, existing):
    (existing / "AGENTS.md").write_text("my rules\n")
    install.cmd_into(pkg, existing, confirm=True)
    config = (existing / "_meta" / "vault-config.yml").read_text()
    (pkg / "vault" / "AGENTS.md").write_text("package rules v2\n")
    install.cmd_update(pkg, existing)
    text = (existing / "AGENTS.md").read_text()
    assert text.startswith("my rules\n") and text.endswith(block("package rules v2"))
    assert text.count(install.START) == 1
    assert (existing / "_meta" / "vault-config.yml").read_text() == config


def test_update_writes_schema_new_when_changed(pkg, tmp_path):
    dest = tmp_path / "v"
    install.cmd_new(pkg, dest)
    (pkg / "vault" / "_meta" / "schema.md").write_text("schema v2\n")
    conflicts = install.cmd_update(pkg, dest)
    assert conflicts == [dest / "_meta" / "schema.md.new"]


def test_update_refreshes_copied_claude_skills(pkg, tmp_path, monkeypatch):
    def no_symlink(*args, **kwargs):
        raise OSError("symlinks not allowed")
    monkeypatch.setattr(install.os, "symlink", no_symlink)
    dest = tmp_path / "v"
    install.cmd_new(pkg, dest)
    (pkg / "skills" / "demo" / "SKILL.md").write_text("# Demo v2\n")
    install.cmd_update(pkg, dest)
    assert (dest / ".claude" / "skills" / "demo" / "SKILL.md").read_text() == "# Demo v2\n"


def test_update_needs_an_install(pkg, existing):
    with pytest.raises(SystemExit):
        install.cmd_update(pkg, existing)


# ----- against the real package -----------------------------------------------

@pytest.mark.skipif(not (REAL_PKG / "skills").is_dir(), reason="run the export first")
def test_fresh_vault_lints_clean(tmp_path):
    dest = tmp_path / "v"
    install.cmd_new(REAL_PKG, dest)
    lint = dest / ".agents" / "skills" / "obsidian-lint-light" / "lint.py"
    # An empty PATH hides the Obsidian CLI, so lint never queries a real vault.
    (tmp_path / "bin").mkdir()
    env = {**os.environ, "PATH": str(tmp_path / "bin")}
    result = subprocess.run([sys.executable, str(lint), "--keep", "0"],
                            capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stderr
    assert "Issues: 0." in result.stdout, result.stdout


@pytest.mark.skipif(not (REAL_PKG / "skills").is_dir(), reason="run the export first")
def test_into_real_package_then_lint_builds_indexes(existing, tmp_path):
    install.cmd_into(REAL_PKG, existing, confirm=True)
    assert list((existing / "_sources").iterdir()) == []
    assert not (existing / "_meta" / "index.md").exists()
    assert (existing / "_meta" / "schema.md").is_file()
    lint = existing / ".agents" / "skills" / "obsidian-lint-light" / "lint.py"
    (tmp_path / "bin").mkdir()
    env = {**os.environ, "PATH": str(tmp_path / "bin")}
    result = subprocess.run([sys.executable, str(lint), "--rebuild-indexes", "--keep", "0"],
                            capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stderr
    assert (existing / "Notes" / "index.md").is_file()
