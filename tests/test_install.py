import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import install  # noqa: E402


@pytest.fixture
def pkg(tmp_path):
    """A minimal package: vault/ with AGENTS.md and _meta/, skills/ with one skill."""
    root = tmp_path / "pkg"
    (root / "vault" / "_meta").mkdir(parents=True)
    (root / "vault" / "_sources").mkdir()
    (root / "vault" / "AGENTS.md").write_text("package rules\n")
    (root / "vault" / "CLAUDE.md").write_text("@AGENTS.md\n")
    (root / "vault" / "_meta" / "vault-config.yml").write_text("writable_roots: []\n")
    (root / "skills" / "demo").mkdir(parents=True)
    (root / "skills" / "demo" / "SKILL.md").write_text("# Demo\n")
    return root


def test_new_creates_vault_and_symlink(pkg, tmp_path):
    dest = tmp_path / "v"
    assert install.cmd_new(pkg, dest) == []
    assert (dest / "AGENTS.md").read_text() == "package rules\n"
    assert (dest / ".agents" / "skills" / "demo" / "SKILL.md").is_file()
    link = dest / ".claude" / "skills"
    assert link.is_symlink() and os.readlink(link) == os.path.join("..", ".agents", "skills")


def test_new_refuses_nonempty_folder(pkg, tmp_path):
    dest = tmp_path / "v"
    dest.mkdir()
    (dest / "note.md").write_text("x")
    with pytest.raises(SystemExit):
        install.cmd_new(pkg, dest)


def test_into_keeps_existing_file(pkg, tmp_path):
    vault = tmp_path / "mine"
    vault.mkdir()
    (vault / "AGENTS.md").write_text("my rules\n")
    conflicts = install.cmd_into(pkg, vault)
    assert (vault / "AGENTS.md").read_text() == "my rules\n"
    assert (vault / "AGENTS.md.new").read_text() == "package rules\n"
    assert conflicts == [vault / "AGENTS.md.new"]
    assert (vault / "_meta" / "vault-config.yml").is_file()


def test_into_skips_identical_file(pkg, tmp_path):
    vault = tmp_path / "mine"
    vault.mkdir()
    (vault / "AGENTS.md").write_text("package rules\n")
    assert install.cmd_into(pkg, vault) == []
    assert not (vault / "AGENTS.md.new").exists()


def test_symlink_failure_copies_skills(pkg, tmp_path, monkeypatch):
    def no_symlink(*args, **kwargs):
        raise OSError("symlinks not allowed")
    monkeypatch.setattr(install.os, "symlink", no_symlink)
    dest = tmp_path / "v"
    install.cmd_new(pkg, dest)
    link = dest / ".claude" / "skills"
    assert not link.is_symlink()
    assert (link / "demo" / "SKILL.md").is_file()


REAL_PKG = Path(__file__).resolve().parents[1]


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
def test_into_real_package_skips_example_content(tmp_path):
    vault = tmp_path / "mine"
    vault.mkdir()
    install.cmd_into(REAL_PKG, vault)
    assert (vault / "_sources").is_dir()
    assert list((vault / "_sources").iterdir()) == []
    assert not (vault / "_meta" / "index.md").exists()
    assert (vault / "_meta" / "vault-config.yml").is_file()
    assert (vault / "_meta" / "schema.md").is_file()
