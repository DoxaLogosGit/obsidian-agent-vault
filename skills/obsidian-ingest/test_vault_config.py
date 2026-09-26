import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import extract_conversations as ec  # noqa: E402


def write_config(vault, text):
    (vault / "_meta").mkdir()
    (vault / "_meta" / "vault-config.yml").write_text(text, encoding="utf-8")


def test_reads_writable_roots(tmp_path):
    write_config(tmp_path, "writable_roots: [Projects, Notes]\n")
    assert ec.load_writable_roots(tmp_path) == ("Projects", "Notes")


def test_missing_file_exits(tmp_path, capsys):
    with pytest.raises(SystemExit):
        ec.load_writable_roots(tmp_path)
    assert "vault-config.yml" in capsys.readouterr().err


def test_key_must_be_a_list(tmp_path, capsys):
    write_config(tmp_path, "writable_roots: Projects\n")
    with pytest.raises(SystemExit):
        ec.load_writable_roots(tmp_path)
    assert "writable_roots" in capsys.readouterr().err


def test_iter_notes_walks_only_writable_roots(tmp_path):
    write_config(tmp_path, "writable_roots: [Notes]\n")
    for folder, name in [("Notes", "a.md"), ("Other", "b.md")]:
        (tmp_path / folder).mkdir()
        (tmp_path / folder / name).write_text("x", encoding="utf-8")
    assert [p.name for p in ec.iter_notes(tmp_path)] == ["a.md"]
