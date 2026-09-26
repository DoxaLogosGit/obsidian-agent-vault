import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import set_owner  # noqa: E402


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / "v"
    (root / "_meta").mkdir(parents=True)
    notes = root / "Notes"
    (notes / "_attachments").mkdir(parents=True)
    (notes / "plain.md").write_text("# Plain\n\nbody\n")
    (notes / "fm.md").write_text("---\ntags:\n  - a\ntitle: \"X: y\"\n---\n\nbody\n")
    (notes / "owned.md").write_text("---\nowner: agent\n---\nbody\n")
    (notes / "index.md").write_text("# Index\n")
    (notes / "_attachments" / "skip.md").write_text("skip\n")
    return root


def test_adds_owner_only_where_missing(vault):
    assert set_owner.main(["Notes", "--owner", "human", "--apply"], vault=vault) == 0
    notes = vault / "Notes"
    assert (notes / "plain.md").read_text() == "---\nowner: human\n---\n\n# Plain\n\nbody\n"
    assert (notes / "fm.md").read_text() == (
        "---\nowner: human\ntags:\n  - a\ntitle: \"X: y\"\n---\n\nbody\n")
    assert (notes / "owned.md").read_text() == "---\nowner: agent\n---\nbody\n"


def test_skips_index_and_underscore_dirs(vault):
    set_owner.main(["Notes", "--owner", "human", "--apply"], vault=vault)
    assert (vault / "Notes" / "index.md").read_text() == "# Index\n"
    assert (vault / "Notes" / "_attachments" / "skip.md").read_text() == "skip\n"


def test_dry_run_writes_nothing(vault, capsys):
    before = (vault / "Notes" / "plain.md").read_text()
    set_owner.main(["Notes", "--owner", "human"], vault=vault)
    assert (vault / "Notes" / "plain.md").read_text() == before
    out = capsys.readouterr().out
    assert "Notes/plain.md" in out and "Dry run" in out


def test_rejects_unknown_owner(vault):
    with pytest.raises(SystemExit):
        set_owner.main(["Notes", "--owner", "boss"], vault=vault)


def test_rejects_folder_outside_vault(vault, tmp_path):
    (tmp_path / "elsewhere").mkdir()
    assert set_owner.main([str(tmp_path / "elsewhere"), "--owner", "human"], vault=vault) == 1
