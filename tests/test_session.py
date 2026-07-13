"""IndexSession — hold, patch, and atomic-swap, with patches re-resolving *globally*.

Deterministic: changes are simulated by writing files and calling ``patch()`` directly, never
via the timing-sensitive filesystem watcher (which is smoke-verified separately). The last test
is the load-bearing one — a patch must re-bind references in files that did not themselves change.
"""

from pathlib import Path

from claude_ast.index import IndexSession


def _session(root: Path) -> IndexSession:
    return IndexSession(root, use_store=False)


def test_patch_picks_up_a_new_file(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def one():\n    return 1\n")
    session = _session(tmp_path)
    assert session.current.find_definition("b.two") == []

    (tmp_path / "b.py").write_text("def two():\n    return 2\n")
    session.patch()
    assert {d.id for d in session.current.find_definition("b.two")} == {"b.two"}


def test_patch_reflects_a_deletion(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def one():\n    return 1\n")
    (tmp_path / "b.py").write_text("def two():\n    return 2\n")
    session = _session(tmp_path)
    assert session.current.find_definition("b.two")

    (tmp_path / "b.py").unlink()
    session.patch()
    assert session.current.find_definition("b.two") == []


def test_patch_reflects_a_modification(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def one():\n    return 1\n")
    session = _session(tmp_path)
    assert session.current.find_definition("a.two") == []

    (tmp_path / "a.py").write_text("def one():\n    return 1\n\n\ndef two():\n    return 2\n")
    session.patch()
    assert {d.id for d in session.current.find_definition("a.two")} == {"a.two"}


def test_patch_swaps_in_a_fresh_index_object(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n")
    session = _session(tmp_path)
    before = session.current
    (tmp_path / "b.py").write_text("y = 2\n")
    after = session.patch()
    assert after is session.current and after is not before  # atomic swap to a new Index


def test_patch_reresolves_globally_across_unchanged_files(tmp_path: Path) -> None:
    # b.py references a.hub; a.py doesn't exist yet, so b's edge points to an EXTERNAL a.hub.
    # Adding a.py must newly-bind that edge IN-TREE even though b.py itself never changed.
    (tmp_path / "b.py").write_text("from a import hub\n\n\ndef use():\n    return hub()\n")
    session = _session(tmp_path)
    before = {(d.id, d.external) for d in session.current.find_dependencies("b.use")}
    assert ("a.hub", True) in before  # external — a.py is absent

    (tmp_path / "a.py").write_text("def hub():\n    return 1\n")
    session.patch()
    after = {(d.id, d.external) for d in session.current.find_dependencies("b.use")}
    assert ("a.hub", False) in after  # now in-tree: the patch re-resolved globally
