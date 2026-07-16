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
from claude_ast.model import Confidence

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


def test_outline_lists_submodules_as_collapsed_leaves(index: Index) -> None:
    ids = {e.id for e in index.outline("sample_pkg")}
    assert "sample_pkg.VERSION" in ids       # the package's own member, shown
    assert "sample_pkg.core" in ids          # a submodule — named as a table-of-contents leaf...
    assert "sample_pkg.core.hub" not in ids  # ...but not descended into (its contents stay hidden)


def test_same_qualname_collision_is_preserved(index: Index) -> None:
    features = {d.id for d in index.find_definition("feature")}
    assert features == {"sample_pkg.compat.feature", "sample_pkg.compat.feature#2"}


def test_repo_map_surfaces_the_most_referenced_symbol(index: Index) -> None:
    ids = {e.id for e in index.repo_map(budget=500)}
    assert "sample_pkg.core.hub" in ids


def test_external_dependencies_resolve_as_definite_external_edges(index: Index) -> None:
    path_deps = index.find_dependencies("sample_pkg.externals.build_path")
    join = [d for d in path_deps if d.id == "os.path.join"]
    # scoped to the join edge specifically: a direct external call is definite (a possible
    # STUB edge on some other dep must not be able to flip a blanket `all(definite)`).
    assert join and join[0].kind == "call" and join[0].external and join[0].tier == "definite"

    base = index.find_dependencies("sample_pkg.externals.Plugin")
    assert ("abc.ABC", "inherits", True) in {(d.id, d.kind, d.external) for d in base}


def test_module_attribute_call_resolves_to_an_external_edge(index: Index) -> None:
    # `os.getcwd()` — a call through a module attribute — binds to os.getcwd, not dropped.
    deps = index.find_dependencies("sample_pkg.externals.working_dir")
    assert ("os.getcwd", "call", True) in {(d.id, d.kind, d.external) for d in deps}


def test_builtin_call_resolves_to_a_definite_external_edge(index: Index) -> None:
    deps = index.find_dependencies("sample_pkg.externals.count")
    assert ("builtins.len", "call", True, "definite") in {
        (d.id, d.kind, d.external, d.tier) for d in deps
    }


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


def test_call_site_reports_the_passed_type_as_a_definite_observation(index: Index) -> None:
    # feed() passes `Service()` into consume() -> `consume RECEIVES_ARG Service`, a definite
    # observation (what was passed), distinct from the possible-tier dispatch resolvers.
    deps = {(d.id, d.kind, d.tier) for d in index.find_dependencies("sample_pkg.service.consume")}
    assert ("sample_pkg.service.Service", "receives-arg", "definite") in deps
    # the reverse view: "where does Service flow in as an argument type?"
    refs = {(r.id, r.kind) for r in index.find_references("sample_pkg.service.Service")}
    assert ("sample_pkg.service.consume", "receives-arg") in refs


def test_stub_resolves_a_member_on_an_external_type(index: Index) -> None:
    # normalize(name: str) -> `name.upper()` binds to the stdlib stub `builtins.str.upper`,
    # a possible-tier edge to an EXTERNAL node (member existence, not dispatch).
    deps = {
        (d.id, d.kind, d.tier, d.external)
        for d in index.find_dependencies("sample_pkg.externals.normalize")
    }
    assert ("builtins.str.upper", "call", "possible", True) in deps


def test_resolution_metrics_summarize_the_index(index: Index) -> None:
    m = index.metrics
    assert m.total_refs > 0 and 0 < m.bound_refs <= m.total_refs
    assert 0.0 < m.coverage <= 1.0
    # the fixture exercises syntactic binding + every value-typed source
    assert m.by_source.get("syntactic", 0) > 0
    assert m.by_source.get("annotation", 0) >= 1  # handle -> Service.run
    assert m.by_source.get("inference", 0) >= 1  # self-calls + bootstrap construction
    assert m.by_source.get("heuristic", 0) >= 1  # dispatch -> Base.persist name-match
    # all three tiers are present: definite (high), and possible (medium + low)
    assert m.by_confidence.get("high", 0) > 0
    assert m.by_confidence.get("medium", 0) > 0
    assert m.by_confidence.get("low", 0) > 0


def test_untyped_receiver_name_matches_via_heuristic(index: Index) -> None:
    # heuristic (LOW) edges are below the default floor -> fetched with min_confidence=LOW
    assert index.find_dependencies("sample_pkg.service.dispatch") == []
    deps = index.find_dependencies("sample_pkg.service.dispatch", Confidence.LOW)
    assert ("sample_pkg.core.Base.persist", "call", "possible") in {
        (d.id, d.kind, d.tier) for d in deps
    }
    edge = next(
        e
        for e in index.graph.out_edges("sample_pkg.service.dispatch")
        if e.dst == "sample_pkg.core.Base.persist"
    )
    assert edge.resolution.source.value == "heuristic"


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

    # ...and the definite call-site observation, reproduced only if RawRef.arg_types
    # round-trips through the store (else the warm RECEIVES_ARG edge vanishes).
    def receives(index: Index, sym: str) -> set[str]:
        return {d.id for d in index.find_dependencies(sym) if d.kind == "receives-arg"}

    consume_gets = {"sample_pkg.service.Service"}
    assert receives(cold_index, "sample_pkg.service.consume") == consume_gets
    assert receives(warm_index, "sample_pkg.service.consume") == consume_gets

    # ...and the stub edge on an external type (`name.upper()` -> builtins.str.upper),
    # reproduced only if RawRef.receiver_type round-trips through the store.
    def str_stub(index: Index) -> set[str]:
        return {
            d.id for d in index.find_dependencies("sample_pkg.externals.normalize") if d.external
        }

    assert str_stub(cold_index) == {"builtins.str.upper"}
    assert str_stub(warm_index) == {"builtins.str.upper"}
