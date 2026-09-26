import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lint  # noqa: E402


def write_config(vault, text):
    (vault / "_meta").mkdir()
    (vault / "_meta" / "vault-config.yml").write_text(text, encoding="utf-8")


def test_reads_both_keys(tmp_path):
    write_config(tmp_path, "audited_roots: [Notes, Archive]\nindex_rollup_roots: [Archive]\n")
    assert lint.load_vault_config(tmp_path) == {
        "audited_roots": ["Notes", "Archive"],
        "index_rollup_roots": ["Archive"],
    }


def test_missing_file_exits(tmp_path):
    with pytest.raises(SystemExit, match="vault-config.yml"):
        lint.load_vault_config(tmp_path)


def test_missing_key_exits(tmp_path):
    write_config(tmp_path, "audited_roots: [Notes]\n")
    with pytest.raises(SystemExit, match="index_rollup_roots"):
        lint.load_vault_config(tmp_path)


def test_key_must_be_a_list(tmp_path):
    write_config(tmp_path, "audited_roots: Notes\nindex_rollup_roots: []\n")
    with pytest.raises(SystemExit, match="audited_roots"):
        lint.load_vault_config(tmp_path)


def test_module_globals_come_from_config():
    config = lint.load_vault_config(lint.VAULT)
    assert lint.ALLOWED_TOP == tuple(config["audited_roots"])
    assert lint.INDEX_ROLLUP_ROOTS == config["index_rollup_roots"]
