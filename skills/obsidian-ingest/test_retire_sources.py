import hashlib
import json
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import extract_conversations as ec  # noqa: E402
import retire_sources as rs  # noqa: E402


def make_vault(root):
    (root / "_meta").mkdir(parents=True)
    (root / "_meta" / "vault-config.yml").write_text("writable_roots: [Projects]\n")
    (root / "_sources").mkdir()
    (root / "Projects" / "P").mkdir(parents=True)
    return root


def write_note(vault, name, sources, owner="shared"):
    lines = [f"owner: {owner}", "sources:"]
    for s in sources:
        lines.append(f"  - path: {s['path']}")
        if "sha256" in s:
            lines.append(f"    sha256: {s['sha256']}")
    note = vault / "Projects" / "P" / name
    note.write_text("---\n" + "\n".join(lines) + "\n---\n\n# Note\n")
    return note


def export_zip(path, name="chat"):
    data = [{"uuid": "u1", "name": name, "chat_messages": []}]
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("conversations.json", json.dumps(data))
    return path


def content_hash(path):
    return hashlib.sha256(ec.export_content_bytes(path)).hexdigest()


@pytest.fixture
def vault(tmp_path):
    return make_vault(tmp_path / "v")


def test_file_mode_retires_only_named_file(vault):
    (vault / "_sources" / "a.md").write_text("a")
    (vault / "_sources" / "b.md").write_text("b")
    note = write_note(vault, "n.md", [{"path": "_sources/a.md"}, {"path": "_sources/b.md"}])
    assert rs.main(["_sources/a.md", "--apply"], vault=vault) == 0
    text = note.read_text()
    a_block, b_block = text.split("_sources/b.md")
    assert "retired: true" in a_block
    assert "retired: true" not in b_block


def test_zip_gets_content_hash_not_file_hash(vault):
    z = export_zip(vault / "_sources" / "export.zip")
    assert content_hash(z) != hashlib.sha256(z.read_bytes()).hexdigest()
    note = write_note(vault, "n.md", [{"path": "_sources/export.zip"}])
    rs.main(["_sources/export.zip", "--apply"], vault=vault)
    assert f"sha256: {content_hash(z)}" in note.read_text()


def test_existing_hash_is_kept(vault):
    z = export_zip(vault / "_sources" / "export.zip")
    note = write_note(vault, "n.md", [{"path": "_sources/export.zip", "sha256": content_hash(z)}])
    rs.main(["_sources/export.zip", "--apply"], vault=vault)
    assert note.read_text().count(content_hash(z)) == 1


def test_changed_file_warns_and_is_not_deleted(vault, capsys):
    (vault / "_sources" / "a.md").write_text("new content")
    write_note(vault, "n.md", [{"path": "_sources/a.md", "sha256": "0" * 64}])
    rs.main(["_sources/a.md", "--apply", "--delete"], vault=vault)
    out = capsys.readouterr().out
    assert "changed after ingest" in out
    assert (vault / "_sources" / "a.md").exists()


def test_uncited_file_warns_and_is_not_deleted(vault, capsys):
    (vault / "_sources" / "orphan.md").write_text("x")
    rs.main(["_sources/orphan.md", "--apply", "--delete"], vault=vault)
    assert "cited by NO note" in capsys.readouterr().out
    assert (vault / "_sources" / "orphan.md").exists()


def test_delete_removes_fully_retired_file(vault):
    (vault / "_sources" / "a.md").write_text("a")
    write_note(vault, "n1.md", [{"path": "_sources/a.md"}])
    write_note(vault, "n2.md", [{"path": "_sources/a.md"}])
    assert rs.main(["_sources/a.md", "--apply", "--delete"], vault=vault) == 0
    assert not (vault / "_sources" / "a.md").exists()


def test_human_note_blocks_write_and_delete(vault, capsys):
    (vault / "_sources" / "a.md").write_text("a")
    note = write_note(vault, "n.md", [{"path": "_sources/a.md"}], owner="human")
    before = note.read_text()
    rs.main(["_sources/a.md", "--apply", "--delete"], vault=vault)
    assert note.read_text() == before
    assert "owner: human" in capsys.readouterr().out
    assert (vault / "_sources" / "a.md").exists()


def test_dry_run_writes_nothing(vault):
    (vault / "_sources" / "a.md").write_text("a")
    note = write_note(vault, "n.md", [{"path": "_sources/a.md"}])
    before = note.read_text()
    rs.main(["_sources/a.md"], vault=vault)
    assert note.read_text() == before


def test_delete_requires_apply(vault):
    (vault / "_sources" / "a.md").write_text("a")
    with pytest.raises(SystemExit):
        rs.main(["_sources/a.md", "--delete"], vault=vault)


def test_file_outside_vault_sources_is_rejected(vault):
    (vault / "Projects" / "P" / "x.md").write_text("x")
    assert rs.main(["Projects/P/x.md"], vault=vault) == 1


def test_scope_mode_still_works(vault):
    src = vault / "Projects" / "P" / "_sources"
    src.mkdir()
    (src / "old.md").write_text("old")
    note = write_note(vault, "n.md", [{"path": "_sources/old.md"}])
    assert rs.main(["--scope", "Projects/P", "--apply"], vault=vault) == 0
    assert "retired: true" in note.read_text()
