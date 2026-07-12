"""Neutral ingest orchestration — discovery and dispatch, independent of language.

These tests must not know anything Python-specific: they exercise the seam
(``Indexer`` protocol) and the orchestrator (``ingest_project`` /
``iter_source_files``). A fake backend proves the routing is protocol-driven,
not hardcoded to Python.
"""

from collections.abc import Sequence
from pathlib import Path

from claude_ast.ingest import FileIndex, ResolveResult, ingest_project, iter_source_files
from claude_ast.model import Span, Symbol, SymbolKind


class FakeBackend:
    """A minimal Indexer for a made-up language — structurally satisfies the seam."""

    name = "fake"
    extensions = frozenset({".fake"})

    def ingest_file(self, path: Path, root: Path) -> FileIndex | None:
        return self.ingest_text(path, root, "")

    def ingest_text(self, path: Path, root: Path, source: str) -> FileIndex | None:
        sid = path.stem
        return FileIndex(
            path=str(path),
            module=sid,
            symbols=[Symbol(sid, sid, SymbolKind.MODULE, Span(str(path), 1))],
        )

    def resolve(self, files: Sequence[FileIndex]) -> ResolveResult:
        return ResolveResult(edges=[], externals=[])


def test_iter_source_files_skips_excluded_dirs(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("x = 1\n")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "junk.py").write_text("y = 2\n")
    found = {p.name for p in iter_source_files(tmp_path, frozenset({".py"}))}
    assert found == {"a.py"}  # .venv excluded by default


def test_default_backend_dispatches_python_and_ignores_others(tmp_path):
    (tmp_path / "a.py").write_text("def f():\n    ...\n")
    (tmp_path / "notes.md").write_text("# not claimed by any backend\n")
    result = ingest_project(tmp_path)
    # routing only (neutral): the .py file is ingested, the .md is not
    assert {Path(fi.path).name for fi in result.files} == {"a.py"}


def test_orchestrator_routes_to_any_backend_by_protocol(tmp_path):
    # A non-Python backend is honoured purely via the Indexer protocol; the
    # orchestrator has no Python knowledge of its own.
    (tmp_path / "thing.fake").write_text("whatever\n")
    (tmp_path / "ignored.py").write_text("z = 1\n")  # no python backend passed
    result = ingest_project(tmp_path, indexers=[FakeBackend()])
    ids = {sym.id for fi in result.files for sym in fi.symbols}
    assert ids == {"thing"}  # only the fake backend's files; .py ignored


def test_warm_start_reuses_unchanged_files(tmp_path):
    (tmp_path / "a.py").write_text("def f():\n    ...\n")
    cold = ingest_project(tmp_path)
    assert set(cold.fresh)  # cold run parses everything fresh

    cache = dict(cold.fresh)
    warm = ingest_project(tmp_path, cache=cache)
    assert warm.fresh == {}  # unchanged file reused, not reparsed
    assert {s.id for fi in warm.files for s in fi.symbols} == {"a", "a.f"}


def test_changed_file_is_reparsed_and_deletion_pruned(tmp_path):
    a = tmp_path / "a.py"
    a.write_text("def f():\n    ...\n")
    (tmp_path / "b.py").write_text("def g():\n    ...\n")
    cold = ingest_project(tmp_path)
    cache = dict(cold.fresh)

    a.write_text("def f():\n    ...\n\n\ndef h():\n    ...\n")  # size changes -> stamp differs
    (tmp_path / "b.py").unlink()  # deletion

    warm = ingest_project(tmp_path, cache=cache)
    assert str(a) in warm.fresh  # changed file reparsed (stamp differs)
    assert warm.present == {str(a)}  # deleted b.py no longer present -> prunable
