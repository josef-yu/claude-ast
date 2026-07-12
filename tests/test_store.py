"""Neutral store tests — round-trip parse products through SQLite."""

from claude_ast.ingest import FileIndex, RawRef
from claude_ast.model import EdgeKind, Span, Symbol, SymbolKind
from claude_ast.store import SqliteStore


def _file_index() -> FileIndex:
    return FileIndex(
        path="m.py",
        module="m",
        symbols=[
            Symbol("m", "m", SymbolKind.MODULE, Span("m.py", 1), doc="Module."),
            Symbol(
                "m.f", "f", SymbolKind.FUNCTION, Span("m.py", 3, 0, 5, 4),
                signature="def f()", parent="m",
            ),
        ],
        refs=[RawRef("m.f", EdgeKind.CALL, "g", Span("m.py", 4, 4, 4, 5))],
        imports={"g": "other.g"},
    )


def test_store_round_trips_a_file_index(tmp_path):
    db = tmp_path / ".claude-ast" / "index.db"
    store = SqliteStore(db)
    store.upsert("m.py", (123, 456), _file_index())
    store.close()

    reopened = SqliteStore(db)  # a fresh process would do exactly this
    cached = reopened.load()
    reopened.close()

    assert set(cached) == {"m.py"}
    assert cached["m.py"].stamp == (123, 456)
    fi = cached["m.py"].file
    assert fi.module == "m"
    assert fi.imports == {"g": "other.g"}
    assert [s.id for s in fi.symbols] == ["m", "m.f"]
    f = fi.symbols[1]
    assert f.kind is SymbolKind.FUNCTION
    assert f.signature == "def f()"
    assert f.parent == "m"
    assert (f.span.end_line, f.span.end_col) == (5, 4)  # spans survive
    assert fi.refs[0].kind is EdgeKind.CALL and fi.refs[0].name == "g"


def test_store_delete_prunes_files(tmp_path):
    db = tmp_path / ".claude-ast" / "index.db"
    store = SqliteStore(db)
    store.upsert("a.py", (1, 1), _file_index())
    store.upsert("b.py", (1, 1), _file_index())
    store.delete(["a.py"])
    store.close()

    reopened = SqliteStore(db)
    assert set(reopened.load()) == {"b.py"}
    reopened.close()


def test_store_writes_a_self_ignoring_gitignore(tmp_path):
    cache_dir = tmp_path / ".claude-ast"
    SqliteStore(cache_dir / "index.db").close()
    assert (cache_dir / ".gitignore").read_text() == "*\n"
