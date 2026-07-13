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


def test_self_call_resolves_to_the_class_member_as_possible(index: Index) -> None:
    # `self.save()` in Base.persist -> Base.save, at the possible tier (value-typed).
    deps = {(d.id, d.kind, d.tier) for d in index.find_dependencies("sample_pkg.core.Base.persist")}
    assert ("sample_pkg.core.Base.save", "call", "possible") in deps


def test_self_call_resolves_through_a_cross_file_base(index: Index) -> None:
    # `self.save()` in Service.store resolves to the INHERITED Base.save (a different module).
    deps = {(d.id, d.tier) for d in index.find_dependencies("sample_pkg.service.Service.store")}
    assert ("sample_pkg.core.Base.save", "possible") in deps
    callers = index.find_callers("sample_pkg.core.Base.save")
    assert {r.id for r in callers} == {
        "sample_pkg.core.Base.persist",
        "sample_pkg.service.Service.store",
    }
    assert all(r.tier == "possible" for r in callers)


def test_annotated_receiver_resolves_to_the_typed_method(index: Index) -> None:
    # `service: Service` -> `service.run()` binds to Service.run at the possible tier.
    deps = {(d.id, d.kind, d.tier) for d in index.find_dependencies("sample_pkg.service.handle")}
    assert ("sample_pkg.service.Service.run", "call", "possible") in deps


def test_constructed_receiver_resolves_via_inference(index: Index) -> None:
    # `s = Service()` -> `s.run()` binds to Service.run at the possible tier.
    deps = {(d.id, d.kind, d.tier) for d in index.find_dependencies("sample_pkg.service.bootstrap")}
    assert ("sample_pkg.service.Service.run", "call", "possible") in deps


def test_resolution_metrics_summarize_the_index(index: Index) -> None:
    m = index.metrics
    assert m.total_refs > 0 and 0 < m.bound_refs <= m.total_refs
    assert 0.0 < m.coverage <= 1.0
    # the fixture exercises syntactic binding + every value-typed source
    assert m.by_source.get("syntactic", 0) > 0
    assert m.by_source.get("annotation", 0) >= 1  # handle -> Service.run
    assert m.by_source.get("inference", 0) >= 1  # self-calls + bootstrap construction
    # both tiers are present: definite (high) and possible (medium)
    assert m.by_confidence.get("high", 0) > 0 and m.by_confidence.get("medium", 0) > 0


def test_external_targets_stay_out_of_ranking(index: Index) -> None:
    # Library nodes are edge sinks, never part of the ranked skeleton.
    ids = {e.id for e in index.repo_map(budget=1000)}
    assert "os.path.join" not in ids and "abc.ABC" not in ids


def test_warm_rebuild_reproduces_results(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Persisted round-trip via the snapshot must reproduce the cold-build answers.
    monkeypatch.setenv("CLAUDE_AST_CACHE_DIR", str(tmp_path))
    cold_index = Index.build(FIXTURES)  # cold: parses and writes the snapshot
    warm_index = Index.build(FIXTURES)  # warm: reuses persisted refs, rebuilds edges

    def callers(index: Index, symbol: str) -> set[str]:
        return {r.id for r in index.find_callers(symbol)}

    # syntactic (definite) edges reproduce...
    hub_callers = {"sample_pkg.service.Service.run", "sample_pkg.service.start"}
    assert callers(cold_index, "sample_pkg.core.hub") == hub_callers
    assert callers(warm_index, "sample_pkg.core.hub") == hub_callers

    # ...and so do the value-typed (possible) self edges, which are only reproduced if
    # RawRef.local_root survives the store round-trip (else the warm self-edges vanish).
    save_callers = {"sample_pkg.core.Base.persist", "sample_pkg.service.Service.store"}
    assert callers(cold_index, "sample_pkg.core.Base.save") == save_callers
    assert callers(warm_index, "sample_pkg.core.Base.save") == save_callers

    # ...and the annotation (handle) + construction-inference (bootstrap) edges, which
    # survive only if receiver_type AND receiver_inferred round-trip through the store.
    run_callers = {"sample_pkg.service.handle", "sample_pkg.service.bootstrap"}
    assert callers(cold_index, "sample_pkg.service.Service.run") == run_callers
    assert callers(warm_index, "sample_pkg.service.Service.run") == run_callers
