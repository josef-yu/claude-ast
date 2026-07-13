"""Python backend — reference extraction + syntactic binding (the `definite` edges).

Same-module calls/inheritance, cross-file + relative imports, package re-exports,
module-rooted attribute chains, external targets, and the scope/shadow rules that
keep binding from forging a confidently-wrong edge. Value-typed resolution (self /
annotation / inference) lives in test_resolvers.py.
"""

from claude_ast.index import Index


def test_a_lambda_param_shadows_an_import(tmp_path):
    # `lambda os: os.getcwd()` — `os` is the lambda's parameter, so the attribute call must
    # NOT bind to the imported `os` module. A lambda is a scope whose params shadow, like a
    # def's; without that the shadowed name forges a confidently-wrong external edge.
    (tmp_path / "m.py").write_text("import os\n\n\ndef f():\n    return lambda os: os.getcwd()\n")
    index = Index.build(tmp_path)
    assert index.find_dependencies("m.f") == []


def test_a_reassigned_global_shadows_an_import(tmp_path):
    # `global os; os = get()` rebinds `os` to an unknown value, so `os.getcwd()` must NOT
    # bind to the stdlib module (only the legit `get()` call survives).
    (tmp_path / "m.py").write_text(
        "import os\n\n\n"
        "def get():\n    return 1\n\n\n"
        "def f():\n    global os\n    os = get()\n    return os.getcwd()\n"
    )
    index = Index.build(tmp_path)
    deps = {d.id for d in index.find_dependencies("m.f")}
    assert "os.getcwd" not in deps
    assert "m.get" in deps  # the reassignment's RHS still binds


def test_same_qualname_sibling_owns_its_own_edges(tmp_path):
    # The `#2` (else-branch) def must carry its OWN edges, not have them
    # misattributed to the first def: reference extraction consumes the same
    # node->id map symbol extraction mints, so an edge's src == its enclosing id.
    (tmp_path / "m.py").write_text(
        "def target():\n    ...\n\n\n"
        "if X:\n"
        "    def feature():\n        ...\n"        # first def — no call
        "else:\n"
        "    def feature():\n        target()\n"   # `#2` def — the sole caller
    )
    index = Index.build(tmp_path)
    assert {r.id for r in index.find_callers("m.target")} == {"m.feature#2"}


def test_binding_same_module_call_and_inheritance(tmp_path):
    (tmp_path / "m.py").write_text(
        "def helper():\n    ...\n\n\n"
        "class Base:\n    ...\n\n\n"
        "class User(Base):\n    def act(self):\n        helper()\n"
    )
    index = Index.build(tmp_path)

    assert "m.User.act" in {r.id for r in index.find_callers("m.helper")}
    assert ("m.Base", "inherits") in {(r.id, r.kind) for r in index.find_dependencies("m.User")}
    assert all(r.tier == "definite" for r in index.find_callers("m.helper"))  # syntactic = high


def test_binding_resolves_a_cross_file_import(tmp_path):
    (tmp_path / "models.py").write_text("class User:\n    ...\n")
    (tmp_path / "svc.py").write_text(
        "from models import User\n\n\ndef make():\n    return User()\n"
    )
    index = Index.build(tmp_path)

    # `User()` in svc.make binds cross-file to models.User via the import
    assert "svc.make" in {r.id for r in index.find_callers("models.User")}


def test_external_import_call_becomes_a_definite_external_edge(tmp_path):
    # A call to a from-imported stdlib name is not dropped: it binds to an
    # EXTERNAL node as a `definite` dependency (the reference genuinely exists).
    (tmp_path / "m.py").write_text(
        "from os.path import join\n\n\ndef build(name):\n    return join('/tmp', name)\n"
    )
    index = Index.build(tmp_path)

    ext = [d for d in index.find_dependencies("m.build") if d.id == "os.path.join"]
    assert ext and ext[0].kind == "call" and ext[0].tier == "definite" and ext[0].external
    assert index.graph.is_external("os.path.join")
    # external targets are edge sinks, never definitions or ranked skeleton entries
    assert index.find_definition("os.path.join") == []
    assert "os.path.join" not in {e.id for e in index.repo_map(budget=500)}


def test_external_base_class_becomes_an_external_inherits_edge(tmp_path):
    (tmp_path / "m.py").write_text("from abc import ABC\n\n\nclass Plugin(ABC):\n    ...\n")
    index = Index.build(tmp_path)

    deps = {(d.id, d.kind, d.external) for d in index.find_dependencies("m.Plugin")}
    assert ("abc.ABC", "inherits", True) in deps


def test_module_attribute_call_binds_to_an_external_node(tmp_path):
    # `import os` then `os.getcwd()` — attribute chain rooted at a module import.
    (tmp_path / "m.py").write_text("import os\n\n\ndef here():\n    return os.getcwd()\n")
    index = Index.build(tmp_path)

    deps = {(d.id, d.external) for d in index.find_dependencies("m.here")}
    assert ("os.getcwd", True) in deps


def test_dotted_external_base_class_is_an_external_inherits_edge(tmp_path):
    (tmp_path / "m.py").write_text("import abc\n\n\nclass C(abc.ABC):\n    ...\n")
    index = Index.build(tmp_path)

    deps = {(d.id, d.kind, d.external) for d in index.find_dependencies("m.C")}
    assert ("abc.ABC", "inherits", True) in deps


def test_internal_module_attribute_call_binds_in_tree(tmp_path):
    # `import pkg.mod` then `pkg.mod.f()` resolves to the in-tree symbol, not external.
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "mod.py").write_text("def f():\n    ...\n")
    (tmp_path / "main.py").write_text("import pkg.mod\n\n\ndef g():\n    return pkg.mod.f()\n")
    index = Index.build(tmp_path)

    assert "main.g" in {r.id for r in index.find_callers("pkg.mod.f")}
    assert not index.graph.is_external("pkg.mod.f")


def test_value_receiver_attribute_call_yields_no_edge(tmp_path):
    # `x.run()` on a parameter is value-typed — deferred to the resolvers, never a false edge.
    (tmp_path / "m.py").write_text("def g(x):\n    return x.run()\n")
    index = Index.build(tmp_path)

    assert index.find_dependencies("m.g") == []
    assert not index.graph.is_external("x.run")


def test_unknown_attr_on_an_internal_module_is_deferred_not_externalized(tmp_path):
    # `helpers.missing()` — internal root, unknown attribute — must NOT mint a bogus
    # external node; it is left for the resolver stack.
    (tmp_path / "helpers.py").write_text("def real():\n    ...\n")
    (tmp_path / "main.py").write_text(
        "import helpers\n\n\ndef g():\n    return helpers.missing()\n"
    )
    index = Index.build(tmp_path)

    assert index.find_dependencies("main.g") == []
    assert not index.graph.is_external("helpers.missing")


def test_shadowing_local_receiver_does_not_forge_a_definite_edge(tmp_path):
    # `getcwd` is a param shadowing the import; `getcwd.write()` is a value receiver and
    # must NOT bind through the import to a false definite external edge.
    (tmp_path / "m.py").write_text(
        "from os import getcwd\n\n\ndef f(getcwd):\n    return getcwd.write()\n"
    )
    index = Index.build(tmp_path)
    assert index.find_dependencies("m.f") == []
    assert not index.graph.is_external("os.getcwd.write")


def test_match_capture_name_is_a_local_receiver_not_an_import(tmp_path):
    # `case getcwd:` binds a local; `getcwd.write()` must NOT bind through the import.
    (tmp_path / "m.py").write_text(
        "from os import getcwd\n\n\n"
        "def f(cmd):\n    match cmd:\n        case getcwd:\n            return getcwd.write()\n"
    )
    index = Index.build(tmp_path)
    assert index.find_dependencies("m.f") == []
    assert not index.graph.is_external("os.getcwd.write")


def test_module_scope_loop_target_does_not_shadow_bind_an_import(tmp_path):
    # `for json in ...` at module scope must not bind `json.dumps()` through `import json`.
    (tmp_path / "m.py").write_text(
        "import json\n\n\nconfigs = []\nfor json in configs:\n    json.dumps(configs)\n"
    )
    index = Index.build(tmp_path)
    assert not index.graph.is_external("json.dumps")


def test_class_body_var_does_not_shadow_bind_an_import(tmp_path):
    # A class-body var named like an import must not forge a definite edge through the import.
    (tmp_path / "m.py").write_text("import x\n\n\nclass C:\n    x = object()\n    x.save()\n")
    index = Index.build(tmp_path)
    assert not index.graph.is_external("x.save")


def test_relative_import_binds_cross_module(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "core.py").write_text("def hub():\n    ...\n")
    (pkg / "service.py").write_text("from .core import hub\n\n\ndef run():\n    return hub()\n")
    index = Index.build(tmp_path)

    assert "pkg.service.run" in {r.id for r in index.find_callers("pkg.core.hub")}


def test_relative_import_beyond_top_level_is_skipped(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    # `...` from pkg.m walks above the top-level package -> unresolvable, no crash, no edge.
    (pkg / "m.py").write_text("from ... import x\n\n\ndef f():\n    return x()\n")
    index = Index.build(tmp_path)

    assert index.find_dependencies("pkg.m.f") == []


def test_package_reexport_binds_to_the_real_definition(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("from .core import hub\n")  # re-export
    (pkg / "core.py").write_text("def hub():\n    ...\n")
    (pkg / "app.py").write_text("from pkg import hub\n\n\ndef run():\n    return hub()\n")
    index = Index.build(tmp_path)

    # `from pkg import hub` follows pkg/__init__'s re-export to the real pkg.core.hub
    assert "pkg.app.run" in {r.id for r in index.find_callers("pkg.core.hub")}


def test_reexport_chain_is_followed(tmp_path):
    pkg = tmp_path / "pkg"
    (pkg / "sub").mkdir(parents=True)
    (pkg / "__init__.py").write_text("from .sub import work\n")           # pkg.work -> pkg.sub.work
    (pkg / "sub" / "__init__.py").write_text("from .impl import work\n")  # -> pkg.sub.impl.work
    (pkg / "sub" / "impl.py").write_text("def work():\n    ...\n")
    (pkg / "app.py").write_text("from pkg import work\n\n\ndef run():\n    return work()\n")
    index = Index.build(tmp_path)

    assert "pkg.app.run" in {r.id for r in index.find_callers("pkg.sub.impl.work")}


def test_local_parameter_does_not_bind_to_a_module_function(tmp_path):
    (tmp_path / "m.py").write_text(
        "def run():\n    ...\n\n\n"
        "def go(run):\n    run()\n"  # `run` is the parameter, not m.run
    )
    index = Index.build(tmp_path)

    assert index.find_callers("m.run") == []


def test_non_parameter_locals_do_not_bind_to_module_functions(tmp_path):
    # assignment, for-target, and comprehension shadows must not emit false edges
    (tmp_path / "m.py").write_text(
        "def save(): ...\n"
        "def helper(): ...\n\n\n"
        "def run(items):\n"
        "    save = items[0]\n"  # local assignment shadows module save
        "    save()\n"
        "    for helper in items:\n"  # for-target shadows module helper
        "        helper()\n"
    )
    index = Index.build(tmp_path)
    assert index.find_callers("m.save") == []
    assert index.find_callers("m.helper") == []


def test_function_local_import_does_not_leak_module_wide(tmp_path):
    # a `from x import y` inside one function must not let a sibling's bare y() bind
    (tmp_path / "m.py").write_text(
        "def a():\n"
        "    from other import thing\n"
        "    thing()\n\n\n"
        "def b():\n"
        "    thing()\n"  # NameError at runtime — must not bind anywhere
    )
    (tmp_path / "other.py").write_text("def thing(): ...\n")
    index = Index.build(tmp_path)
    # b.thing() must not resolve to other.thing (a's local import doesn't leak)
    assert "m.b" not in {r.id for r in index.find_callers("other.thing")}


def test_call_in_a_default_argument_is_attributed_to_the_enclosing_scope(tmp_path):
    (tmp_path / "m.py").write_text("def make(): ...\n\n\ndef f(x=make()):\n    ...\n")
    index = Index.build(tmp_path)
    # the default `make()` runs at module scope and must not be dropped
    assert "m.make" in {r.id for fam in ("m",) for r in index.find_dependencies(fam)}


def test_builtin_call_binds_to_a_definite_external_node(tmp_path):
    (tmp_path / "m.py").write_text("def size(items):\n    return len(items)\n")
    index = Index.build(tmp_path)

    ext = [d for d in index.find_dependencies("m.size") if d.id == "builtins.len"]
    assert ext and ext[0].tier == "definite" and ext[0].external
    assert index.graph.is_external("builtins.len")


def test_a_local_def_shadows_the_builtin(tmp_path):
    # A module-level `def len` shadows the builtin -> bind in-tree, not to builtins.len.
    (tmp_path / "m.py").write_text("def len(x):\n    ...\n\n\ndef use():\n    return len(0)\n")
    index = Index.build(tmp_path)

    assert "m.use" in {r.id for r in index.find_callers("m.len")}
    assert not index.graph.is_external("builtins.len")


def test_builtin_base_class_is_an_external_inherits_edge(tmp_path):
    (tmp_path / "m.py").write_text("class MyError(Exception):\n    ...\n")
    index = Index.build(tmp_path)

    deps = {(d.id, d.kind, d.external) for d in index.find_dependencies("m.MyError")}
    assert ("builtins.Exception", "inherits", True) in deps


def test_builtin_type_attribute_call_binds_external(tmp_path):
    (tmp_path / "m.py").write_text("def combine(parts):\n    return str.join(',', parts)\n")
    index = Index.build(tmp_path)

    deps = {(d.id, d.external) for d in index.find_dependencies("m.combine")}
    assert ("builtins.str.join", True) in deps
