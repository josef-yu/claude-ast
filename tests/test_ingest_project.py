"""Neutral ingest orchestration — discovery and dispatch, independent of language.

These tests must not know anything Python-specific: they exercise the seam
(``Indexer`` protocol) and the orchestrator (``ingest_project`` /
``iter_source_files``). A fake backend proves the routing is protocol-driven,
not hardcoded to Python.
"""

from collections.abc import Sequence
from pathlib import Path

from claude_ast.ingest import FileIndex, ingest_project, iter_source_files
from claude_ast.model import Edge, Span, Symbol, SymbolKind


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

    def resolve(self, files: Sequence[FileIndex]) -> list[Edge]:
        return []


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
    ids = {sym.id for fi in ingest_project(tmp_path).files for sym in fi.symbols}
    assert ids == {"a", "a.f"}


def test_orchestrator_routes_to_any_backend_by_protocol(tmp_path):
    # A non-Python backend is honoured purely via the Indexer protocol; the
    # orchestrator has no Python knowledge of its own.
    (tmp_path / "thing.fake").write_text("whatever\n")
    (tmp_path / "ignored.py").write_text("z = 1\n")  # no python backend passed
    result = ingest_project(tmp_path, indexers=[FakeBackend()])
    ids = {sym.id for fi in result.files for sym in fi.symbols}
    assert ids == {"thing"}  # only the fake backend's files; .py ignored
