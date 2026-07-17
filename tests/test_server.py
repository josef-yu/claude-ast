"""MCP server — the read-only tool surface over a real index (transport-free).

Exercises the JSON shapers against the golden fixture and confirms build_server registers
its tools without spinning up stdio or touching stdout (the protocol channel). The thin
FastMCP/stdio glue is covered by the smoke check.
"""

from pathlib import Path

import pytest

from claude_ast.index import Index, IndexSession
from claude_ast.server.__main__ import main
from claude_ast.server.app import _definition, _outline, _ref, _relations, build_server

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(scope="module")
def index() -> Index:
    return Index.build(FIXTURES, use_store=False)


def test_find_definition_shaper(index: Index) -> None:
    (hub,) = _definition(index, "sample_pkg.core.hub")
    assert hub["id"] == "sample_pkg.core.hub"
    assert hub["kind"] == "function"
    assert hub["signature"] == "def hub() -> int"
    assert hub["file"].endswith("core.py") and hub["line"] == 17


def test_outline_shaper_nests_by_depth(index: Index) -> None:
    entries = _outline(index, "sample_pkg.core")
    ids = {e["id"] for e in entries}
    assert {"sample_pkg.core.Base", "sample_pkg.core.hub"} <= ids
    save = next(e for e in entries if e["id"] == "sample_pkg.core.Base.save")
    assert save["depth"] == 2  # module(0) -> class(1) -> method(2)


def test_dependency_ref_shape(index: Index) -> None:
    deps = [_ref(r) for r in index.find_dependencies("sample_pkg.service.start")]
    hub = next(d for d in deps if d["id"] == "sample_pkg.core.hub")
    assert hub["kind"] == "call" and hub["tier"] == "definite" and hub["external"] is False
    assert hub["location"] and ":" in hub["location"]


def test_external_dependency_is_flagged(index: Index) -> None:
    deps = [_ref(r) for r in index.find_dependencies("sample_pkg.externals.build_path")]
    join = next(d for d in deps if d["id"] == "os.path.join")
    assert join["external"] is True and join["tier"] == "definite"


def test_relations_shape_found_with_results(index: Index) -> None:
    sym = "sample_pkg.service.start"
    r = _relations(index, sym, index.find_dependencies(sym), index.suppression(sym, "dependencies"))
    assert r["symbol"] == sym and r["found"] is True
    assert r["suggestions"] == []
    assert r["suppressed"] == {"confidence": 0, "reassignment": 0}
    assert any(x["id"] == "sample_pkg.core.hub" for x in r["results"])


def test_relations_shape_unknown_symbol_carries_a_near_miss(index: Index) -> None:
    # a real symbol with no results vs an unknown id: `found` is the disambiguator, and an unknown
    # id offers near-misses so Claude can retry instead of trusting an empty `results`.
    sym = "sample_pkg.service.startt"  # typo of ...service.start
    r = _relations(index, sym, index.find_dependencies(sym), index.suppression(sym, "dependencies"))
    assert r["found"] is False and r["results"] == []
    assert "sample_pkg.service.start" in r["suggestions"]


def test_build_server_registers_tools_without_touching_stdout(capsys) -> None:
    server = build_server(IndexSession(FIXTURES, use_store=False))
    assert server.name == "claude-ast"
    assert capsys.readouterr().out == ""  # nothing leaked onto the protocol channel


def test_entry_point_reports_a_missing_path() -> None:
    # The happy path blocks on stdio; the error path must return before .run().
    assert main(["/no/such/path/xyzzy"]) == 2
