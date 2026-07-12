"""Golden end-to-end eval — known-answer regression net over a real index.

Unlike the neutral tests (model primitives) and the backend unit tests, this
builds the *whole* engine over a committed fixture package and pins the answers
queries must return: definitions, relationships, and — crucially — confidence
tiers. It is the guard rail the P2 resolver stack builds against. Today every
edge is ``definite``; when ``possible`` edges arrive they should *extend* these
assertions, not silently change them. If precision/recall drifts, this fails.

The fixture is rooted at ``tests/fixtures`` so its modules qualify as
``sample_pkg.*`` and its absolute imports bind. Built with ``use_store=False`` so
the eval stays hermetic (no ``.claude-ast/`` written into the fixture tree).
"""

from pathlib import Path

import pytest

from claude_ast.index import Index

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture(scope="module")
def index() -> Index:
    return Index.build(FIXTURES, use_store=False)


def test_definitions_by_qualified_and_bare_name(index: Index) -> None:
    hub = index.find_definition("sample_pkg.core.hub")
    assert [d.id for d in hub] == ["sample_pkg.core.hub"]
    assert hub[0].signature == "def hub() -> int"
    assert {d.id for d in index.find_definition("save")} == {"sample_pkg.core.Base.save"}


def test_callers_of_the_hub_are_definite(index: Index) -> None:
    callers = index.find_callers("sample_pkg.core.hub")
    assert {r.id for r in callers} == {
        "sample_pkg.service.Service.run",
        "sample_pkg.service.start",
    }
    assert all(r.tier == "definite" for r in callers)


def test_dependencies_follow_the_import(index: Index) -> None:
    deps = {(r.id, r.kind) for r in index.find_dependencies("sample_pkg.service.start")}
    assert ("sample_pkg.core.hub", "call") in deps


def test_inheritance_edge_crosses_the_import(index: Index) -> None:
    refs = {(r.id, r.kind) for r in index.find_references("sample_pkg.core.Base")}
    assert ("sample_pkg.service.Service", "inherits") in refs


def test_a_local_shadow_does_not_forge_a_caller(index: Index) -> None:
    # core.shadowed defines a local `hub`; its `hub()` must not bind to core.hub.
    callers = {r.id for r in index.find_callers("sample_pkg.core.hub")}
    assert "sample_pkg.core.shadowed" not in callers


def test_outline_excludes_submodules(index: Index) -> None:
    ids = {e.id for e in index.outline("sample_pkg")}
    assert "sample_pkg.VERSION" in ids       # the package's own member
    assert "sample_pkg.core" not in ids       # a submodule, not a member
    assert "sample_pkg.core.hub" not in ids   # nor a submodule's contents


def test_same_qualname_collision_is_preserved(index: Index) -> None:
    features = {d.id for d in index.find_definition("feature")}
    assert features == {"sample_pkg.compat.feature", "sample_pkg.compat.feature#2"}


def test_repo_map_surfaces_the_most_referenced_symbol(index: Index) -> None:
    ids = {e.id for e in index.repo_map(budget=500)}
    assert "sample_pkg.core.hub" in ids


def test_external_dependencies_resolve_as_definite_external_edges(index: Index) -> None:
    path_deps = index.find_dependencies("sample_pkg.externals.build_path")
    assert ("os.path.join", "call", True) in {(d.id, d.kind, d.external) for d in path_deps}
    assert all(d.tier == "definite" for d in path_deps)

    base = index.find_dependencies("sample_pkg.externals.Plugin")
    assert ("abc.ABC", "inherits", True) in {(d.id, d.kind, d.external) for d in base}


def test_module_attribute_call_resolves_to_an_external_edge(index: Index) -> None:
    # `os.getcwd()` — a call through a module attribute — binds to os.getcwd, not dropped.
    deps = index.find_dependencies("sample_pkg.externals.working_dir")
    assert ("os.getcwd", "call", True) in {(d.id, d.kind, d.external) for d in deps}


def test_external_targets_stay_out_of_ranking(index: Index) -> None:
    # Library nodes are edge sinks, never part of the ranked skeleton.
    ids = {e.id for e in index.repo_map(budget=1000)}
    assert "os.path.join" not in ids and "abc.ABC" not in ids


def test_warm_rebuild_reproduces_results(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Persisted round-trip via the snapshot must reproduce the cold-build answers.
    monkeypatch.setenv("CLAUDE_AST_CACHE_DIR", str(tmp_path))
    cold = {r.id for r in Index.build(FIXTURES).find_callers("sample_pkg.core.hub")}
    warm = {r.id for r in Index.build(FIXTURES).find_callers("sample_pkg.core.hub")}
    assert cold == warm == {"sample_pkg.service.Service.run", "sample_pkg.service.start"}
