"""Python backend — the value-typed resolver stack + confidence tiers.

The `possible`-tier edges: self-calls (INFERENCE), annotated receivers
(ANNOTATION), and local `x = Foo()` construction inference (INFERENCE), plus the
shared member lookup (own / inherited / declined) and the honest-tier / no-false-
edge guarantees. Syntactic binding is test_edges.py.
"""

from claude_ast.index import Index
from claude_ast.model import Confidence


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


def test_subscripted_annotation_is_not_read_as_the_element_type(tmp_path):
    # `us: list[User]` must not be read as `us: User`: no MEDIUM annotation edge (a LOW
    # heuristic name-match may still fire — the receiver is untyped as far as we know).
    (tmp_path / "m.py").write_text(
        "class User:\n    def save(self):\n        ...\n\n\n"
        "def store(us: list[User]):\n    return us.save()\n"
    )
    index = Index.build(tmp_path)
    sources = {e.resolution.source.value for e in index.graph.out_edges("m.store")}
    assert "annotation" not in sources


# --- richer annotation forms: Optional collapse + union fan-out ---

_TWO_CLASSES = (
    "class User:\n    def save(self):\n        ...\n\n\n"
    "class Admin:\n    def save(self):\n        ...\n\n\n"
)


def _annotation_targets(index, src):
    return {e.dst for e in index.graph.out_edges(src) if e.resolution.source.value == "annotation"}


def test_optional_param_receiver_collapses_to_the_type(tmp_path):
    # `u: User | None` IS a `User` at the call site (the None arm is not a receiver type), so it
    # resolves exactly like a bare `u: User` — a single ANNOTATION edge, no fan-out.
    (tmp_path / "m.py").write_text(
        "class User:\n    def save(self):\n        ...\n\n\n"
        "def store(u: User | None):\n    return u.save()\n"
    )
    index = Index.build(tmp_path)
    assert _annotation_targets(index, "m.store") == {"m.User.save"}


def test_optional_subscript_param_collapses_to_the_type(tmp_path):
    # `Optional[User]` is the typing spelling of `User | None` — same single-type collapse.
    (tmp_path / "m.py").write_text(
        "from typing import Optional\n\n\n"
        "class User:\n    def save(self):\n        ...\n\n\n"
        "def store(u: Optional[User]):\n    return u.save()\n"
    )
    index = Index.build(tmp_path)
    assert _annotation_targets(index, "m.store") == {"m.User.save"}


def test_union_param_fans_out_to_each_arm(tmp_path):
    # `u: User | Admin` — a member call could dispatch to either, so it fans out to one possible
    # ANNOTATION edge per in-tree arm, both anchored at the single `u.save()` site.
    (tmp_path / "m.py").write_text(
        _TWO_CLASSES + "def store(u: User | Admin):\n    return u.save()\n"
    )
    index = Index.build(tmp_path)
    deps = {(d.id, d.tier) for d in index.find_dependencies("m.store")}
    assert ("m.User.save", "possible") in deps
    assert ("m.Admin.save", "possible") in deps
    # both edges are ANNOTATION and share the one call site's span
    edges = [e for e in index.graph.out_edges("m.store") if e.dst.endswith(".save")]
    assert {e.resolution.source.value for e in edges} == {"annotation"}
    assert all(e.at is not None for e in edges)
    assert len({(e.at.line, e.at.col) for e in edges if e.at is not None}) == 1
    # the reverse: the same site is a caller of each arm
    assert "m.store" in {r.id for r in index.find_callers("m.User.save")}
    assert "m.store" in {r.id for r in index.find_callers("m.Admin.save")}


def test_union_subscript_param_fans_out_to_each_arm(tmp_path):
    # `Union[User, Admin]` fans out identically to `User | Admin`.
    (tmp_path / "m.py").write_text(
        "from typing import Union\n\n\n" + _TWO_CLASSES
        + "def store(u: Union[User, Admin]):\n    return u.save()\n"
    )
    index = Index.build(tmp_path)
    assert _annotation_targets(index, "m.store") == {"m.User.save", "m.Admin.save"}


def test_union_arm_without_the_member_is_dropped(tmp_path):
    # `u: User | Admin` where only `User` defines `save`: the Admin arm resolves no member and is
    # silently dropped — one honest edge, never a wrong one.
    (tmp_path / "m.py").write_text(
        "class User:\n    def save(self):\n        ...\n\n\n"
        "class Admin:\n    def other(self):\n        ...\n\n\n"
        "def store(u: User | Admin):\n    return u.save()\n"
    )
    index = Index.build(tmp_path)
    assert _annotation_targets(index, "m.store") == {"m.User.save"}


def test_optional_external_param_stubs_the_member(tmp_path):
    # `p: Path | None` collapses to the external `Path`, whose member resolves via the stub tables.
    (tmp_path / "m.py").write_text(
        "from pathlib import Path\n\n\ndef store(p: Path | None):\n    return p.exists()\n"
    )
    index = Index.build(tmp_path)
    edges = {(e.dst, e.resolution.source.value) for e in index.graph.out_edges("m.store")}
    assert ("pathlib.Path.exists", "stub") in edges


def test_mixed_intree_external_union_resolves_each_arm(tmp_path):
    # `x: User | Path` — the in-tree arm resolves `save` as an ANNOTATION edge, the external arm
    # resolves `exists` via the stub tables; each member binds on the arm that carries it.
    (tmp_path / "m.py").write_text(
        "from pathlib import Path\n\n\nclass User:\n    def save(self):\n        ...\n\n\n"
        "def store(x: User | Path):\n    x.save()\n    x.exists()\n"
    )
    index = Index.build(tmp_path)
    edges = {(e.dst, e.resolution.source.value) for e in index.graph.out_edges("m.store")}
    assert ("m.User.save", "annotation") in edges
    assert ("pathlib.Path.exists", "stub") in edges


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


def test_straight_line_reassignment_resolves_to_the_live_type(tmp_path):
    # `x = User(); x = Post(); x.save()` — flow tracks the reassignment, so at the use x is Post,
    # not an ambiguous drop: one INFERENCE edge to Post.save (the type live at that point).
    (tmp_path / "m.py").write_text(
        "class User:\n    def save(self):\n        ...\n\n\n"
        "class Post:\n    def save(self):\n        ...\n\n\n"
        "def run():\n    x = User()\n    x = Post()\n    return x.save()\n"
    )
    index = Index.build(tmp_path)

    save = {
        (e.dst, e.resolution.source.value)
        for e in index.graph.out_edges("m.run")
        if e.dst.endswith(".save")
    }
    assert save == {("m.Post.save", "inference")}


def test_reassignment_reports_each_live_type_at_its_own_site(tmp_path):
    # The positional promise: `x.save()` before the reassignment sees User, after it sees Post —
    # each use resolves to the type live at that point, at that site's span.
    (tmp_path / "m.py").write_text(
        "class User:\n    def save(self):\n        ...\n\n\n"
        "class Post:\n    def save(self):\n        ...\n\n\n"
        "def run():\n    x = User()\n    x.save()\n    x = Post()\n    return x.save()\n"
    )
    index = Index.build(tmp_path)
    save = {
        (e.dst, e.at.line)
        for e in index.graph.out_edges("m.run")
        if e.at is not None and e.dst.endswith(".save") and e.resolution.source.value == "inference"
    }
    # User.save at the first use (line 13), Post.save at the second (line 15) — no cross-attribution
    assert ("m.User.save", 13) in save
    assert ("m.Post.save", 15) in save
    assert ("m.Post.save", 13) not in save
    assert ("m.User.save", 15) not in save


def test_branch_reassignment_reports_the_may_set(tmp_path):
    # `x = User(); if c: x = Post(); x.save()` — x could be either at the use (the branch may or
    # may not run), so it fans out to both arms (the honest may-set), not a wrong-exclusive edge.
    (tmp_path / "m.py").write_text(
        "class User:\n    def save(self):\n        ...\n\n\n"
        "class Post:\n    def save(self):\n        ...\n\n\n"
        "def run(c):\n    x = User()\n    if c:\n        x = Post()\n    return x.save()\n"
    )
    index = Index.build(tmp_path)
    save = {
        (e.dst, e.resolution.source.value)
        for e in index.graph.out_edges("m.run")
        if e.dst.endswith(".save")
    }
    assert save == {("m.User.save", "inference"), ("m.Post.save", "inference")}


def test_a_shadowed_constructor_name_does_not_type_the_receiver(tmp_path):
    # `def f(User): x = User()` constructs the PARAMETER, not the module class — the
    # inference must not bind through the shadowed name (a LOW name-match is the honest cap).
    (tmp_path / "m.py").write_text(
        "class User:\n    def save(self):\n        ...\n\n\n"
        "def f(User):\n    x = User()\n    return x.save()\n"
    )
    index = Index.build(tmp_path)

    assert "m.User.save" not in {d.id for d in index.find_dependencies("m.f")}  # no MEDIUM edge
    edge = next(e for e in index.graph.out_edges("m.f") if e.dst == "m.User.save")
    assert edge.resolution.source.value == "heuristic"  # untyped receiver -> LOW, not inference


def test_a_rebound_local_drops_its_inferred_type(tmp_path):
    # `x = User(); x = 5` — flow-insensitively, User no longer holds at every use of x,
    # so the construction must not type the later receiver call.
    (tmp_path / "m.py").write_text(
        "class User:\n    def save(self):\n        ...\n\n\n"
        "def f():\n    x = User()\n    x = 5\n    return x.save()\n"
    )
    index = Index.build(tmp_path)
    assert "m.User.save" not in {d.id for d in index.find_dependencies("m.f")}


def test_a_loop_rebound_local_drops_its_inferred_type(tmp_path):
    # any non-construction rebinding poisons the type — `for x in ...` included.
    (tmp_path / "m.py").write_text(
        "class User:\n    def save(self):\n        ...\n\n\n"
        "def f(items):\n    x = User()\n    for x in items:\n        pass\n    return x.save()\n"
    )
    index = Index.build(tmp_path)
    assert "m.User.save" not in {d.id for d in index.find_dependencies("m.f")}


def test_inference_binds_a_function_return_value(tmp_path):
    # `x = make(); x.save()` where `make` returns User (here inferred from `return User()`, with
    # no annotation): x's type is make's return type, so the receiver call resolves to User.save.
    (tmp_path / "m.py").write_text(
        "class User:\n    def save(self):\n        ...\n\n\n"
        "def make():\n    return User()\n\n\n"
        "def run():\n    x = make()\n    return x.save()\n"
    )
    index = Index.build(tmp_path)
    assert "m.User.save" in {d.id for d in index.find_dependencies("m.run")}


def test_nested_shadow_does_not_inherit_the_outer_annotation(tmp_path):
    # inner's `u` is a distinct, untyped param: no ANNOTATION edge inherited from outer's
    # `u: User` (a LOW heuristic name-match is fine — the point is honest provenance).
    (tmp_path / "m.py").write_text(
        "class User:\n    def m(self):\n        ...\n\n\n"
        "def outer(u: User):\n"
        "    def inner(u):\n        return u.m()\n"
        "    return inner\n"
    )
    index = Index.build(tmp_path)
    sources = {e.resolution.source.value for e in index.graph.out_edges("m.outer.inner")}
    assert "annotation" not in sources


def test_typed_receiver_resolves_through_an_in_tree_base(tmp_path):
    # An annotated receiver resolves through the shared member-lookup base walk (not just self).
    (tmp_path / "m.py").write_text(
        "class Base:\n    def hook(self):\n        ...\n\n\n"
        "class Sub(Base):\n    ...\n\n\n"
        "def use(s: Sub):\n    return s.hook()\n"
    )
    index = Index.build(tmp_path)
    assert ("m.Base.hook", "possible") in {(d.id, d.tier) for d in index.find_dependencies("m.use")}


def test_local_annotated_assignment_resolves_via_the_declared_type(tmp_path):
    # `x: User = make()` — the local annotation declares x's type (like a parameter), so `x.save()`
    # resolves to `User.save` (ANNOTATION), trusting the declaration over the `make()` return.
    (tmp_path / "m.py").write_text(
        "class User:\n    def save(self):\n        ...\n\n\n"
        "def make():\n    return object()\n\n\n"
        "def run():\n    x: User = make()\n    return x.save()\n"
    )
    index = Index.build(tmp_path)
    assert ("m.User.save", "possible") in {(d.id, d.tier) for d in index.find_dependencies("m.run")}
    edge = next(e for e in index.graph.out_edges("m.run") if e.dst == "m.User.save")
    assert edge.resolution.source.value == "annotation"


def test_bare_annotated_local_without_a_value_is_captured(tmp_path):
    # `x: User` with no assignment still declares the type, so `x.save()` resolves.
    (tmp_path / "m.py").write_text(
        "class User:\n    def save(self):\n        ...\n\n\n"
        "def run():\n    x: User\n    return x.save()\n"
    )
    index = Index.build(tmp_path)
    assert _annotation_targets(index, "m.run") == {"m.User.save"}


def test_union_annotated_local_fans_out(tmp_path):
    # A union local annotation fans out to each arm, exactly as a union parameter does.
    (tmp_path / "m.py").write_text(
        _TWO_CLASSES + "def run():\n    x: User | Admin = get()\n    return x.save()\n"
    )
    index = Index.build(tmp_path)
    assert _annotation_targets(index, "m.run") == {"m.User.save", "m.Admin.save"}


def test_optional_annotated_local_collapses(tmp_path):
    # `x: User | None` collapses to `User` — a single edge, like the parameter case.
    (tmp_path / "m.py").write_text(
        "class User:\n    def save(self):\n        ...\n\n\n"
        "def run():\n    x: User | None = get()\n    return x.save()\n"
    )
    index = Index.build(tmp_path)
    assert _annotation_targets(index, "m.run") == {"m.User.save"}


def test_container_annotated_local_is_not_read_as_the_element_type(tmp_path):
    # `x: list[User]` is a container, not a `User`: no ANNOTATION edge (a LOW heuristic may fire).
    (tmp_path / "m.py").write_text(
        "class User:\n    def save(self):\n        ...\n\n\n"
        "def run():\n    x: list[User] = []\n    return x.save()\n"
    )
    index = Index.build(tmp_path)
    assert "annotation" not in {e.resolution.source.value for e in index.graph.out_edges("m.run")}


def test_reannotated_local_resolves_to_the_latest_declaration(tmp_path):
    # `x: User` then `x: Admin` is a re-declaration, not an unresolvable conflict: flow tracks it,
    # so at `x.save()` (after both) x is Admin -> a single ANNOTATION edge to Admin.save.
    (tmp_path / "m.py").write_text(
        _TWO_CLASSES + "def run():\n    x: User = a()\n    x: Admin = b()\n    return x.save()\n"
    )
    index = Index.build(tmp_path)
    save = {
        (e.dst, e.resolution.source.value)
        for e in index.graph.out_edges("m.run")
        if e.dst.endswith(".save")
    }
    assert save == {("m.Admin.save", "annotation")}


def test_annotated_local_types_from_the_annotation_not_the_rhs(tmp_path):
    # `x: User = Admin()` — the declared annotation types x, not the `Admin()` on the right-hand
    # side, so `x.save()` resolves to `User.save` (ANNOTATION) and never to `Admin.save`.
    (tmp_path / "m.py").write_text(
        _TWO_CLASSES + "def run():\n    x: User = Admin()\n    return x.save()\n"
    )
    index = Index.build(tmp_path)
    edge = next(e for e in index.graph.out_edges("m.run") if e.dst == "m.User.save")
    assert edge.resolution.source.value == "annotation"
    assert "m.Admin.save" not in {e.dst for e in index.graph.out_edges("m.run")}


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


def test_untyped_receiver_name_matches_via_heuristic(tmp_path):
    # `obj.save()` on an untyped param -> a LOW edge to every `*.save` candidate.
    (tmp_path / "m.py").write_text(
        "class A:\n    def save(self):\n        ...\n\n\n"
        "class B:\n    def save(self):\n        ...\n\n\n"
        "def dispatch(obj):\n    return obj.save()\n"
    )
    index = Index.build(tmp_path)

    # heuristic edges are LOW — excluded by the default floor, fetched with min_confidence=LOW
    assert index.find_dependencies("m.dispatch") == []
    deps = {(d.id, d.tier) for d in index.find_dependencies("m.dispatch", Confidence.LOW)}
    assert ("m.A.save", "possible") in deps and ("m.B.save", "possible") in deps
    edge = next(e for e in index.graph.out_edges("m.dispatch") if e.dst == "m.A.save")
    assert edge.resolution.source.value == "heuristic"


def test_heuristic_declines_when_a_name_is_too_common(tmp_path):
    classes = "".join(f"class C{i}:\n    def ping(self):\n        ...\n\n\n" for i in range(12))
    (tmp_path / "m.py").write_text(classes + "def dispatch(obj):\n    return obj.ping()\n")
    index = Index.build(tmp_path)

    # `ping` is defined on 12 classes (over the cap) -> too ambiguous -> no heuristic edges
    assert index.find_dependencies("m.dispatch") == []


def test_typed_receiver_missing_member_does_not_fall_to_heuristic(tmp_path):
    (tmp_path / "m.py").write_text(
        "class A:\n    def save(self):\n        ...\n\n\n"  # a name-match candidate
        "class User:\n    ...\n\n\n"  # the receiver's type — has no `save`
        "def f(u: User):\n    return u.save()\n"
    )
    index = Index.build(tmp_path)

    # u is typed User (no save); the typed path finds nothing and must NOT name-match A.save
    assert index.find_dependencies("m.f") == []
