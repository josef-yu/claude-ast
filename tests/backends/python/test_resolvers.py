"""Python backend — the value-typed resolver stack + confidence tiers.

The `possible`-tier edges: self-calls (INFERENCE), annotated receivers
(ANNOTATION), and local `x = Foo()` construction inference (INFERENCE), plus the
shared member lookup (own / inherited / declined) and the honest-tier / no-false-
edge guarantees. Syntactic binding is test_edges.py.
"""

from claude_ast.index import Index


def test_self_call_resolves_to_the_enclosing_class_member_as_possible(tmp_path):
    (tmp_path / "m.py").write_text(
        "class C:\n"
        "    def run(self):\n        return self.save()\n"
        "    def save(self):\n        ...\n"
    )
    index = Index.build(tmp_path)

    save = [d for d in index.find_dependencies("m.C.run") if d.id == "m.C.save"]
    assert save and save[0].kind == "call" and save[0].tier == "possible"
    assert "m.C.run" in {r.id for r in index.find_callers("m.C.save")}


def test_self_call_resolves_through_an_in_tree_base(tmp_path):
    (tmp_path / "base.py").write_text("class Base:\n    def save(self):\n        ...\n")
    (tmp_path / "m.py").write_text(
        "from base import Base\n\n\n"
        "class Sub(Base):\n    def run(self):\n        return self.save()\n"
    )
    index = Index.build(tmp_path)

    assert ("base.Base.save", "call", "possible") in {
        (d.id, d.kind, d.tier) for d in index.find_dependencies("m.Sub.run")
    }


def test_self_call_to_unknown_member_yields_no_edge(tmp_path):
    (tmp_path / "m.py").write_text("class C:\n    def run(self):\n        return self.missing()\n")
    index = Index.build(tmp_path)
    assert index.find_dependencies("m.C.run") == []


def test_self_in_a_nested_function_is_not_a_method_receiver(tmp_path):
    # self.save() lives in the nested `inner` (a FUNCTION, not a METHOD) -> not resolved.
    (tmp_path / "m.py").write_text(
        "class C:\n"
        "    def save(self):\n        ...\n"
        "    def run(self):\n"
        "        def inner():\n            return self.save()\n"
        "        return inner()\n"
    )
    index = Index.build(tmp_path)
    assert "m.C.run.inner" not in {r.id for r in index.find_callers("m.C.save")}


def test_self_call_to_a_class_variable_is_not_a_call_edge(tmp_path):
    # `self.count()` where `count` is a class variable must not forge a call->variable edge.
    (tmp_path / "m.py").write_text(
        "class C:\n    count = 0\n    def run(self):\n        return self.count()\n"
    )
    index = Index.build(tmp_path)
    assert index.find_dependencies("m.C.run") == []


def test_self_call_resolves_to_the_nearest_override(tmp_path):
    # A defines m, B(A) overrides m; self.m() in Sub(B) resolves to the NEAREST (B.m).
    (tmp_path / "m.py").write_text(
        "class A:\n    def m(self):\n        ...\n"
        "class B(A):\n    def m(self):\n        ...\n"
        "class Sub(B):\n    def run(self):\n        return self.m()\n"
    )
    index = Index.build(tmp_path)
    assert ("m.B.m", "possible") in {(d.id, d.tier) for d in index.find_dependencies("m.Sub.run")}


def test_self_call_across_multiple_inheritance_branches_is_declined(tmp_path):
    # Two in-tree bases on different branches define `m`. C3 would pick one deterministically,
    # but we DECLINE to compute the MRO -> no edge (an honest miss, never a wrong edge).
    (tmp_path / "m.py").write_text(
        "class X:\n    def m(self):\n        ...\n"
        "class A(X):\n    pass\n"
        "class B:\n    def m(self):\n        ...\n"
        "class C(A, B):\n    def run(self):\n        return self.m()\n"
    )
    index = Index.build(tmp_path)
    assert index.find_dependencies("m.C.run") == []


def test_annotated_param_receiver_resolves_to_the_typed_class(tmp_path):
    (tmp_path / "m.py").write_text(
        "class User:\n    def save(self):\n        ...\n\n\n"
        "def store(u: User):\n    return u.save()\n"
    )
    index = Index.build(tmp_path)

    deps = {(d.id, d.kind, d.tier) for d in index.find_dependencies("m.store")}
    assert ("m.User.save", "call", "possible") in deps
    # sourced ANNOTATION (vs INFERENCE for a self-call)
    edge = next(e for e in index.graph.out_edges("m.store") if e.dst == "m.User.save")
    assert edge.resolution.source.value == "annotation"


def test_annotated_param_resolves_cross_file(tmp_path):
    (tmp_path / "models.py").write_text("class User:\n    def save(self):\n        ...\n")
    (tmp_path / "m.py").write_text(
        "from models import User\n\n\ndef store(u: User):\n    return u.save()\n"
    )
    index = Index.build(tmp_path)

    assert ("models.User.save", "possible") in {
        (d.id, d.tier) for d in index.find_dependencies("m.store")
    }


def test_external_or_unknown_annotation_yields_no_edge(tmp_path):
    (tmp_path / "m.py").write_text(
        "from os import PathLike\n\n\n"
        "def a(p: PathLike):\n    return p.foo()\n\n\n"  # external type -> no edge
        "def b(x: Missing):\n    return x.bar()\n"        # undefined type -> no edge
    )
    index = Index.build(tmp_path)
    assert index.find_dependencies("m.a") == []
    assert index.find_dependencies("m.b") == []


def test_subscripted_annotation_is_deferred(tmp_path):
    # `list[User]` is a subscript -> no receiver_type -> deferred, never a false edge.
    (tmp_path / "m.py").write_text(
        "class User:\n    def save(self):\n        ...\n\n\n"
        "def store(us: list[User]):\n    return us.save()\n"
    )
    index = Index.build(tmp_path)
    assert index.find_dependencies("m.store") == []


def test_double_dot_relative_import_feeds_annotation_resolution(tmp_path):
    pkg = tmp_path / "pkg"
    (pkg / "sub").mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "sub" / "__init__.py").write_text("")
    (pkg / "model.py").write_text("class Graph:\n    def add(self):\n        ...\n")
    (pkg / "sub" / "q.py").write_text(
        "from ..model import Graph\n\n\ndef run(g: Graph):\n    return g.add()\n"
    )
    index = Index.build(tmp_path)

    # `from ..model import Graph` resolves to pkg.model.Graph, so `g: Graph; g.add()` binds.
    assert ("pkg.model.Graph.add", "possible") in {
        (d.id, d.tier) for d in index.find_dependencies("pkg.sub.q.run")
    }


def test_reexported_class_feeds_annotation_resolution(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("from .model import Graph\n")
    (pkg / "model.py").write_text("class Graph:\n    def add(self):\n        ...\n")
    (pkg / "app.py").write_text(
        "from pkg import Graph\n\n\ndef run(g: Graph):\n    return g.add()\n"
    )
    index = Index.build(tmp_path)

    # the re-exported type resolves, so `g: Graph; g.add()` binds to pkg.model.Graph.add
    assert ("pkg.model.Graph.add", "possible") in {
        (d.id, d.tier) for d in index.find_dependencies("pkg.app.run")
    }


def test_constructed_local_receiver_resolves_via_inference(tmp_path):
    (tmp_path / "m.py").write_text(
        "class User:\n    def save(self):\n        ...\n\n\n"
        "def run():\n    u = User()\n    return u.save()\n"
    )
    index = Index.build(tmp_path)

    deps = {(d.id, d.kind, d.tier) for d in index.find_dependencies("m.run")}
    assert ("m.User.save", "call", "possible") in deps
    # sourced INFERENCE (a local construction), distinct from an annotation
    edge = next(e for e in index.graph.out_edges("m.run") if e.dst == "m.User.save")
    assert edge.resolution.source.value == "inference"


def test_reassigned_local_is_ambiguous_and_yields_no_inference_edge(tmp_path):
    (tmp_path / "m.py").write_text(
        "class User:\n    def save(self):\n        ...\n\n\n"
        "class Post:\n    def save(self):\n        ...\n\n\n"
        "def run():\n    x = User()\n    x = Post()\n    return x.save()\n"
    )
    index = Index.build(tmp_path)

    # x is assigned two distinct constructors -> ambiguous -> dropped, no guessed edge
    assert not any(d.id.endswith(".save") for d in index.find_dependencies("m.run"))


def test_inference_does_not_bind_a_function_return_value(tmp_path):
    (tmp_path / "m.py").write_text(
        "class User:\n    def save(self):\n        ...\n\n\n"
        "def make():\n    return User()\n\n\n"
        "def run():\n    x = make()\n    return x.save()\n"
    )
    index = Index.build(tmp_path)

    # `make` is a function, not a class; x's type is its return, which we don't infer.
    assert "m.User.save" not in {d.id for d in index.find_dependencies("m.run")}


def test_nested_shadow_does_not_inherit_the_outer_annotation(tmp_path):
    # inner's `u` is a distinct, untyped parameter; it must NOT inherit outer's `u: User`.
    (tmp_path / "m.py").write_text(
        "class User:\n    def m(self):\n        ...\n\n\n"
        "def outer(u: User):\n"
        "    def inner(u):\n        return u.m()\n"
        "    return inner\n"
    )
    index = Index.build(tmp_path)
    assert "m.User.m" not in {d.id for d in index.find_dependencies("m.outer.inner")}


def test_typed_receiver_resolves_through_an_in_tree_base(tmp_path):
    # An annotated receiver resolves through the shared member-lookup base walk (not just self).
    (tmp_path / "m.py").write_text(
        "class Base:\n    def hook(self):\n        ...\n\n\n"
        "class Sub(Base):\n    ...\n\n\n"
        "def use(s: Sub):\n    return s.hook()\n"
    )
    index = Index.build(tmp_path)
    assert ("m.Base.hook", "possible") in {(d.id, d.tier) for d in index.find_dependencies("m.use")}


def test_local_annotated_assignment_is_deferred(tmp_path):
    # `x: User = make()` — a local annotated assignment — is not captured yet -> no edge.
    (tmp_path / "m.py").write_text(
        "class User:\n    def save(self):\n        ...\n\n\n"
        "def make():\n    return object()\n\n\n"
        "def run():\n    x: User = make()\n    return x.save()\n"
    )
    index = Index.build(tmp_path)
    assert "m.User.save" not in {d.id for d in index.find_dependencies("m.run")}


def test_chained_receiver_is_deferred(tmp_path):
    # `self.a.b()` is a chained receiver -> deferred (single attribute only), no edge.
    (tmp_path / "m.py").write_text("class C:\n    def run(self):\n        return self.a.b()\n")
    index = Index.build(tmp_path)
    assert index.find_dependencies("m.C.run") == []


def test_mixed_definite_and_possible_callers(tmp_path):
    # target() is called directly (definite) AND via a typed receiver (possible): both surface.
    (tmp_path / "m.py").write_text(
        "class C:\n    def target(self):\n        ...\n\n\n"
        "def direct():\n    return C.target(None)\n\n\n"
        "def via(c: C):\n    return c.target()\n"
    )
    index = Index.build(tmp_path)
    tiers = {r.id: r.tier for r in index.find_callers("m.C.target")}
    assert tiers.get("m.direct") == "definite"
    assert tiers.get("m.via") == "possible"
