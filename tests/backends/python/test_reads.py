"""Python backend — bare attribute-read edges (the REFERENCE edge kind).

A read (`obj.attr` with no call) flows through the same receiver ladder as a call, but emits a
REFERENCE edge and can land on a *data* attribute, not just a callable. These tests pin the ladder
(self / annotation / inference / stub / heuristic + name-rooted syntactic), the read-vs-call
asymmetry, and the capture boundaries (a call callee is never a read; Store targets are not reads).
The value-typed *call* ladder is test_resolvers.py; syntactic *call* binding is test_edges.py.
"""

from claude_ast.index import Index
from claude_ast.model import Confidence


def test_self_attribute_read_resolves_to_a_data_attribute(tmp_path):
    # `self.kind` (a read, not a call) resolves to the class's data attribute (a VARIABLE) at the
    # possible tier via INFERENCE — a read lands on a member a *call* map deliberately excludes.
    (tmp_path / "m.py").write_text(
        "class C:\n"
        "    kind = 'c'\n"
        "    def who(self):\n        return self.kind\n"
    )
    index = Index.build(tmp_path)
    deps = {(d.id, d.kind, d.tier) for d in index.find_dependencies("m.C.who")}
    assert ("m.C.kind", "reference", "possible") in deps
    edge = next(e for e in index.graph.out_edges("m.C.who") if e.dst == "m.C.kind")
    assert edge.resolution.source.value == "inference"


def test_reading_a_method_as_a_value_is_a_reference_not_a_call(tmp_path):
    # `u.save` (no parens) reads the method as a value -> a REFERENCE edge, distinct from the CALL
    # edge `u.save()` would make. It surfaces as a reference, but never as a caller.
    (tmp_path / "m.py").write_text(
        "class User:\n    def save(self):\n        ...\n\n\n"
        "def handler(u: User):\n    return u.save\n"
    )
    index = Index.build(tmp_path)
    deps = {(d.id, d.kind, d.tier) for d in index.find_dependencies("m.handler")}
    assert ("m.User.save", "reference", "possible") in deps
    assert "m.handler" in {r.id for r in index.find_references("m.User.save")}
    # a read, never a caller:
    assert "m.handler" not in {r.id for r in index.find_callers("m.User.save")}


def test_annotated_receiver_attribute_read_resolves_to_the_typed_member(tmp_path):
    (tmp_path / "m.py").write_text(
        "class User:\n    role = 'admin'\n\n\n"
        "def show(u: User):\n    return u.role\n"
    )
    index = Index.build(tmp_path)
    deps = {(d.id, d.kind, d.tier) for d in index.find_dependencies("m.show")}
    assert ("m.User.role", "reference", "possible") in deps
    edge = next(e for e in index.graph.out_edges("m.show") if e.dst == "m.User.role")
    assert edge.resolution.source.value == "annotation"


def test_constructed_receiver_attribute_read_resolves_via_inference(tmp_path):
    (tmp_path / "m.py").write_text(
        "class User:\n    role = 'admin'\n\n\n"
        "def run():\n    u = User()\n    return u.role\n"
    )
    index = Index.build(tmp_path)
    deps = {(d.id, d.kind, d.tier) for d in index.find_dependencies("m.run")}
    assert ("m.User.role", "reference", "possible") in deps
    edge = next(e for e in index.graph.out_edges("m.run") if e.dst == "m.User.role")
    assert edge.resolution.source.value == "inference"


def test_attribute_read_resolves_through_an_in_tree_base(tmp_path):
    # A read resolves through the same base walk as a call: an inherited data attribute is found.
    (tmp_path / "m.py").write_text(
        "class Base:\n    limit = 10\n\n\n"
        "class Sub(Base):\n    ...\n\n\n"
        "def use(s: Sub):\n    return s.limit\n"
    )
    index = Index.build(tmp_path)
    assert ("m.Base.limit", "reference", "possible") in {
        (d.id, d.kind, d.tier) for d in index.find_dependencies("m.use")
    }


def test_untyped_receiver_read_name_matches_via_heuristic(tmp_path):
    # `obj.role` on an untyped receiver -> a LOW REFERENCE to every `*.role`, below the default.
    (tmp_path / "m.py").write_text(
        "class A:\n    role = 1\n\n\n"
        "class B:\n    role = 2\n\n\n"
        "def sniff(obj):\n    return obj.role\n"
    )
    index = Index.build(tmp_path)
    assert index.find_dependencies("m.sniff") == []  # LOW is below the MEDIUM default
    deps = {(d.id, d.kind, d.tier) for d in index.find_dependencies("m.sniff", Confidence.LOW)}
    assert ("m.A.role", "reference", "possible") in deps
    assert ("m.B.role", "reference", "possible") in deps
    edge = next(e for e in index.graph.out_edges("m.sniff") if e.dst == "m.A.role")
    assert edge.resolution.source.value == "heuristic"


def test_name_rooted_read_binds_an_in_tree_module_variable_as_definite(tmp_path):
    (tmp_path / "conf.py").write_text("DEBUG = True\n")
    (tmp_path / "app.py").write_text("import conf\n\n\ndef flag():\n    return conf.DEBUG\n")
    index = Index.build(tmp_path)
    deps = {(d.id, d.kind, d.tier, d.external) for d in index.find_dependencies("app.flag")}
    assert ("conf.DEBUG", "reference", "definite", False) in deps
    assert "app.flag" in {r.id for r in index.find_references("conf.DEBUG")}


def test_name_rooted_read_of_a_module_attribute_is_a_definite_external(tmp_path):
    # `os.getcwd` read (no call) -> a definite external REFERENCE to the module function.
    (tmp_path / "m.py").write_text("import os\n\n\ndef fn():\n    return os.getcwd\n")
    index = Index.build(tmp_path)
    deps = {(d.id, d.kind, d.external) for d in index.find_dependencies("m.fn")}
    assert ("os.getcwd", "reference", True) in deps


def test_external_type_member_read_resolves_to_a_stub(tmp_path):
    # `p.name` (a read) on an external type -> a possible STUB edge to the member — even though
    # `name` is a property, which a *call* (`p.name()`) would decline as non-callable.
    (tmp_path / "m.py").write_text(
        "from pathlib import Path\n\n\ndef fn(p: Path):\n    return p.name\n"
    )
    index = Index.build(tmp_path)
    deps = {(d.id, d.kind, d.tier, d.external) for d in index.find_dependencies("m.fn")}
    assert ("pathlib.Path.name", "reference", "possible", True) in deps


def test_read_crossing_into_a_value_member_declines_like_a_call(tmp_path):
    # `sys.stdout.getvalue` read: TextIO has no `getvalue`, so it declines — the value-attribute
    # imprecision the call path avoids is not reintroduced for reads.
    (tmp_path / "m.py").write_text("import sys\n\n\ndef fn():\n    return sys.stdout.getvalue\n")
    index = Index.build(tmp_path)
    assert index.find_dependencies("m.fn", Confidence.LOW) == []
    assert not index.graph.is_external("sys.stdout.getvalue")


def test_a_call_callee_is_not_also_emitted_as_a_read(tmp_path):
    # `os.path.join(...)` is a CALL; its callee attribute chain must NOT also emit a REFERENCE
    # read (no `os.path` / `os.path.join` reference edge alongside the call).
    (tmp_path / "m.py").write_text(
        "import os\n\n\ndef build(name):\n    return os.path.join('/tmp', name)\n"
    )
    index = Index.build(tmp_path)
    kinds = {(d.id, d.kind) for d in index.find_dependencies("m.build")}
    assert ("os.path.join", "call") in kinds
    assert ("os.path.join", "reference") not in kinds
    assert ("os.path", "reference") not in kinds


def test_a_store_target_is_not_a_read(tmp_path):
    # `self.x = 1` writes an attribute; a write is not a read, so it emits no REFERENCE edge (and
    # `x` has no symbol, so even the later read yields nothing — an honest miss).
    (tmp_path / "m.py").write_text(
        "class C:\n"
        "    total = 0\n"
        "    def bump(self):\n        self.total = 1\n"  # Store target -> not a read
    )
    index = Index.build(tmp_path)
    assert index.find_dependencies("m.C.bump", Confidence.LOW) == []
    assert "m.C.bump" not in {r.id for r in index.find_references("m.C.total", Confidence.LOW)}


def test_chained_receiver_read_is_deferred(tmp_path):
    # `self.a.b` is a chained receiver read -> deferred (single attribute only), no edge.
    (tmp_path / "m.py").write_text("class C:\n    def run(self):\n        return self.a.b\n")
    index = Index.build(tmp_path)
    assert index.find_dependencies("m.C.run", Confidence.LOW) == []


def test_a_shadowing_local_read_does_not_forge_a_definite_edge(tmp_path):
    # `getcwd` is a parameter shadowing the import; `getcwd.write` (a read) must NOT bind through
    # the import to a false definite external, exactly as the call path guards.
    (tmp_path / "m.py").write_text(
        "from os import getcwd\n\n\ndef f(getcwd):\n    return getcwd.write\n"
    )
    index = Index.build(tmp_path)
    assert not index.graph.is_external("os.getcwd.write")
    # no definite/medium edge — an untyped value receiver at best name-matches (LOW), never definite
    assert all(
        d.tier != "definite" for d in index.find_dependencies("m.f", Confidence.LOW)
    )


def test_read_contributes_to_ranking(tmp_path):
    # REFERENCE edges flow importance in PageRank (they are in _RANK_KINDS). A function read as a
    # value by many callers (a bare `lib.compute`, no call) floats up the ranked skeleton, just as
    # a called function would — and outranks a never-referenced sibling. (Module variables are
    # omitted from the skeleton as noise, so the read target here is a function, which is shown.)
    (tmp_path / "lib.py").write_text(
        "def compute():\n    return 1\n\n\ndef unused():\n    return 2\n"
    )
    readers = "".join(
        f"import lib\n\n\ndef use{i}():\n    return lib.compute\n" for i in range(5)
    )
    (tmp_path / "app.py").write_text(readers)
    index = Index.build(tmp_path)
    assert "lib.compute" in {e.id for e in index.repo_map(budget=500)}
    ranks = {e.id: e.rank for e in index.repo_map(budget=99999)}
    assert ranks["lib.compute"] > ranks.get("lib.unused", 0.0)
