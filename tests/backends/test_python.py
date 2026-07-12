"""Python backend (PythonIndexer) — symbol extraction from ``ast``.

Backend-specific: everything here is about how Python source maps to the
normalized model. Neutral orchestration (dispatch, discovery) lives in
``tests/test_ingest_project.py``. A future language backend gets its own
sibling ``test_<lang>.py`` here.
"""

from claude_ast.index import Index
from claude_ast.ingest import PythonIndexer
from claude_ast.model import SymbolKind

SRC = '''\
"""Module doc.

Second paragraph."""

import os

CONST = 3


def authenticate(email: str, pw: str) -> bool:
    """Verify credentials."""
    return True


class User(Base, metaclass=Meta):
    """A registered account."""

    name: str = ""

    def save(self) -> None:
        """Persist the user."""
        ...

    async def refresh(self):
        ...
'''


def _by_id(src: str, module: str = "auth"):
    fi = PythonIndexer().ingest_source("auth.py", src, module)
    return {sym.id: sym for sym in fi.symbols}


def test_python_backend_declares_its_seam():
    ix = PythonIndexer()
    assert ix.name == "python"
    assert ix.extensions == frozenset({".py"})


def test_module_symbol_and_docline():
    mod = _by_id(SRC)["auth"]
    assert mod.kind is SymbolKind.MODULE
    assert mod.doc == "Module doc."  # first non-empty line only


def test_function_signature_and_doc():
    fn = _by_id(SRC)["auth.authenticate"]
    assert fn.kind is SymbolKind.FUNCTION
    assert fn.signature == "def authenticate(email: str, pw: str) -> bool"
    assert fn.doc == "Verify credentials."


def test_class_signature_with_bases_and_keywords():
    cls = _by_id(SRC)["auth.User"]
    assert cls.kind is SymbolKind.CLASS
    assert cls.signature == "class User(Base, metaclass=Meta)"
    assert cls.doc == "A registered account."
    assert cls.parent == "auth"


def test_method_vs_function_and_async():
    syms = _by_id(SRC)
    assert syms["auth.User.save"].kind is SymbolKind.METHOD
    assert syms["auth.User.save"].signature == "def save(self) -> None"
    assert syms["auth.User.refresh"].signature == "async def refresh(self)"
    assert syms["auth.User.save"].parent == "auth.User"


def test_module_and_class_level_variables():
    syms = _by_id(SRC)
    assert syms["auth.CONST"].kind is SymbolKind.VARIABLE
    assert syms["auth.User.name"].kind is SymbolKind.VARIABLE  # annotated class attr


def test_def_inside_a_block_still_module_scoped():
    syms = _by_id("if True:\n    def hidden():\n        ...\n", module="m")
    assert syms["m.hidden"].kind is SymbolKind.FUNCTION


def test_same_qualname_defs_both_kept_via_disambiguation():
    # Conditional/redefined defs share a qualname; neither may silently overwrite
    # the other in the index. First keeps the base id, the rest get `#N`.
    syms = _by_id(
        "if X:\n    def feature():\n        ...\n"
        "else:\n    def feature():\n        ...\n",
        module="m",
    )
    assert "m.feature" in syms
    assert "m.feature#2" in syms
    assert syms["m.feature"].name == syms["m.feature#2"].name == "feature"


def test_reassigned_variable_is_one_symbol_not_a_collision():
    # A rebound module-level name is the same variable, not two — no `#N` spam.
    syms = _by_id("x = 1\nx = 2\n", module="m")
    assert "m.x" in syms
    assert "m.x#2" not in syms


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


def test_end_to_end_python_source_is_queryable(tmp_path):
    # Backend integration: real Python source flows through parse -> assemble ->
    # query. The query logic itself is proved neutrally in tests/test_query.py.
    (tmp_path / "auth.py").write_text(SRC)
    index = Index.build(tmp_path)

    assert [d.id for d in index.find_definition("auth.User")] == ["auth.User"]
    outline_ids = {e.id for e in index.outline("auth")}
    assert {"auth", "auth.authenticate", "auth.User", "auth.User.save"} <= outline_ids


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


def test_bom_prefixed_source_is_still_parsed(tmp_path):
    (tmp_path / "m.py").write_bytes(b"\xef\xbb\xbfdef f():\n    ...\n")  # UTF-8 BOM
    index = Index.build(tmp_path)
    assert [d.id for d in index.find_definition("m.f")] == ["m.f"]


def test_defs_inside_a_match_case_become_symbols(tmp_path):
    (tmp_path / "m.py").write_text(
        "def pick(x):\n"
        "    match x:\n"
        "        case 1:\n"
        "            def inner():\n"
        "                ...\n"
    )
    index = Index.build(tmp_path)
    assert [d.id for d in index.find_definition("m.pick.inner")] == ["m.pick.inner"]


def test_warm_start_preserves_results_and_writes_a_snapshot(tmp_path):
    (tmp_path / "m.py").write_text("def helper():\n    ...\n\n\ndef use():\n    helper()\n")

    first = Index.build(tmp_path)  # cold — parses + writes snapshot
    assert (tmp_path / ".claude-ast" / "index.db").exists()

    second = Index.build(tmp_path)  # warm — reuses the snapshot
    cold_callers = {r.id for r in first.find_callers("m.helper")}
    warm_callers = {r.id for r in second.find_callers("m.helper")}
    assert cold_callers == warm_callers == {"m.use"}  # identical across cold/warm
