"""Python backend — call-site type observations (the ``RECEIVES_ARG`` reporter).

Where test_resolvers.py covers what a receiver call *dispatches to* (MEDIUM/possible),
this covers what a call site *passes*: ``g(User())`` -> a **definite** ``g RECEIVES_ARG
User`` observation. The scope lines are the point of most of these — constructions only,
functions only, one hop — so the definite label stays honest and the pass never drifts
into type inference.
"""

from claude_ast.index import Index


def _receives(index: Index, sym: str) -> set[str]:
    return {d.id for d in index.find_dependencies(sym) if d.kind == "receives-arg"}


def test_function_callee_reports_the_passed_construction_as_definite(tmp_path):
    (tmp_path / "m.py").write_text(
        "class User:\n    ...\n\n\n"
        "def g(x):\n    ...\n\n\n"
        "def caller():\n    return g(User())\n"
    )
    index = Index.build(tmp_path)

    deps = {(d.id, d.kind, d.tier) for d in index.find_dependencies("m.g")}
    assert ("m.User", "receives-arg", "definite") in deps
    edge = next(e for e in index.graph.out_edges("m.g") if e.kind.value == "receives-arg")
    assert edge.dst == "m.User" and edge.resolution.source.value == "callsite"


def test_multiple_observed_types_each_yield_an_edge(tmp_path):
    (tmp_path / "m.py").write_text(
        "class User:\n    ...\n\n\n"
        "class Admin:\n    ...\n\n\n"
        "def g(x):\n    ...\n\n\n"
        "def a():\n    return g(User())\n\n\n"
        "def b():\n    return g(Admin())\n"
    )
    index = Index.build(tmp_path)
    assert _receives(index, "m.g") == {"m.User", "m.Admin"}


def test_reverse_reference_finds_who_receives_the_type(tmp_path):
    (tmp_path / "m.py").write_text(
        "class User:\n    ...\n\n\n"
        "def g(x):\n    ...\n\n\n"
        "def caller():\n    return g(User())\n"
    )
    index = Index.build(tmp_path)

    # find_references answers "where does User flow as an argument type?"
    assert ("m.g", "receives-arg") in {(r.id, r.kind) for r in index.find_references("m.User")}
    # ...but a receives-arg is not a call, so g is not a *caller* of User
    assert "m.g" not in {r.id for r in index.find_callers("m.User")}


def test_a_factory_call_arg_is_not_reported_as_a_type(tmp_path):
    # g(make()) — make is a function, not a class — so no type is observed (only real
    # constructions resolve to a class id; a factory's return type is not inferred).
    (tmp_path / "m.py").write_text(
        "class User:\n    ...\n\n\n"
        "def make():\n    return User()\n\n\n"
        "def g(x):\n    ...\n\n\n"
        "def caller():\n    return g(make())\n"
    )
    index = Index.build(tmp_path)
    assert _receives(index, "m.g") == set()


def test_a_shadowed_constructor_name_is_not_reported(tmp_path):
    # `User = 5` makes User a local; `g(User())` must not bind to the module class User.
    (tmp_path / "m.py").write_text(
        "class User:\n    ...\n\n\n"
        "def g(x):\n    ...\n\n\n"
        "def caller():\n    User = 5\n    return g(User())\n"
    )
    index = Index.build(tmp_path)
    assert _receives(index, "m.g") == set()


def test_a_constructor_callee_is_deferred(tmp_path):
    # Widget(User()) — the callee is a class (a constructor), whose param has a self-style
    # offset this pass does not model yet -> deferred, no observation.
    (tmp_path / "m.py").write_text(
        "class User:\n    ...\n\n\n"
        "class Widget:\n    ...\n\n\n"
        "def caller():\n    return Widget(User())\n"
    )
    index = Index.build(tmp_path)
    assert _receives(index, "m.Widget") == set()


def test_a_method_callee_is_deferred(tmp_path):
    # self.m(User()) — a value-receiver callee — is not captured, so its args aren't observed.
    (tmp_path / "m.py").write_text(
        "class User:\n    ...\n\n\n"
        "class C:\n"
        "    def m(self, x):\n        ...\n"
        "    def run(self):\n        return self.m(User())\n"
    )
    index = Index.build(tmp_path)
    assert _receives(index, "m.C.m") == set()


def test_a_dotted_construction_is_deferred(tmp_path):
    (tmp_path / "models.py").write_text("class User:\n    ...\n")
    (tmp_path / "m.py").write_text(
        "import models\n\n\n"
        "def g(x):\n    ...\n\n\n"
        "def caller():\n    return g(models.User())\n"
    )
    index = Index.build(tmp_path)
    assert _receives(index, "m.g") == set()


def test_positional_args_past_a_splat_are_dropped(tmp_path):
    # g(User(), *rest, Admin()) — past *rest, position no longer aligns to a param -> stop.
    (tmp_path / "m.py").write_text(
        "class User:\n    ...\n\n\n"
        "class Admin:\n    ...\n\n\n"
        "def g(*args):\n    ...\n\n\n"
        "def caller(rest):\n    return g(User(), *rest, Admin())\n"
    )
    index = Index.build(tmp_path)
    assert _receives(index, "m.g") == {"m.User"}


def test_a_lambda_param_construction_is_not_reported(tmp_path):
    # `lambda User: g(User())` — User is the lambda's OWN parameter, not the module class,
    # so `User()` constructs whatever is passed to the lambda. The shadowed name must not
    # forge a confidently-wrong *definite* observation (a "report, don't rule" violation).
    (tmp_path / "m.py").write_text(
        "class User:\n    ...\n\n\n"
        "def g(x):\n    ...\n\n\n"
        "def outer():\n    return lambda User: g(User())\n"
    )
    index = Index.build(tmp_path)
    assert _receives(index, "m.g") == set()


def test_a_nested_lambda_param_shadow_is_not_reported(tmp_path):
    # Nested lambdas: the outer lambda's `User` param shadows in the inner lambda's body too.
    (tmp_path / "m.py").write_text(
        "class User:\n    ...\n\n\n"
        "def g(x):\n    ...\n\n\n"
        "def outer():\n    return lambda User: (lambda y: g(User()))\n"
    )
    index = Index.build(tmp_path)
    assert _receives(index, "m.g") == set()


def test_a_closure_shadows_but_still_reports_an_unshadowed_type(tmp_path):
    # A nested function closes over its enclosing scope: an enclosing binding named `User`
    # shadows (no observation), but an unshadowed module class seen from the same closure
    # still fires — the scope stack accumulates, it doesn't blanket-suppress.
    (tmp_path / "shadowed.py").write_text(
        "class User:\n    ...\n\n\n"
        "def g(x):\n    ...\n\n\n"
        "def outer(User):\n    def inner():\n        return g(User())\n    return inner\n"
    )
    (tmp_path / "clear.py").write_text(
        "class User:\n    ...\n\n\n"
        "def g(x):\n    ...\n\n\n"
        "def outer():\n    def inner():\n        return g(User())\n    return inner\n"
    )
    index = Index.build(tmp_path)
    assert _receives(index, "shadowed.g") == set()         # User is outer's param -> shadowed
    assert _receives(index, "clear.g") == {"clear.User"}   # User is the module class -> observed


def test_a_reassigned_global_class_name_is_not_reported(tmp_path):
    # `global User; User = 5` rebinds the module class to a non-class, so `g(User())` passes
    # `5()`, not a User. The reassignment must shadow the class name -> no false observation.
    (tmp_path / "m.py").write_text(
        "class User:\n    ...\n\n\n"
        "def g(x):\n    ...\n\n\n"
        "def f():\n    global User\n    User = 5\n    return g(User())\n"
    )
    index = Index.build(tmp_path)
    assert _receives(index, "m.g") == set()


def test_a_bare_global_class_name_is_still_reported(tmp_path):
    # `global User` with no reassignment still refers to the module class -> observation fires.
    (tmp_path / "m.py").write_text(
        "class User:\n    ...\n\n\n"
        "def g(x):\n    ...\n\n\n"
        "def f():\n    global User\n    return g(User())\n"
    )
    index = Index.build(tmp_path)
    assert _receives(index, "m.g") == {"m.User"}


def test_an_external_callee_is_not_profiled(tmp_path):
    # getpid binds to an external node -> we don't observe types into a library param.
    (tmp_path / "m.py").write_text(
        "from os import getpid\n\n\n"
        "class User:\n    ...\n\n\n"
        "def caller():\n    return getpid(User())\n"
    )
    index = Index.build(tmp_path)
    refs = {r.kind for r in index.find_references("m.User")}
    assert "receives-arg" not in refs  # only the construction call from `caller`, no observation
