"""Python backend — definition extraction (source -> normalized Symbols).

How Python source maps to Symbols: modules, functions, classes, methods,
variables, signatures, docstring-lines, and same-qualname disambiguation. No
binding or resolution here — that is test_edges.py / test_resolvers.py.
"""

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


def test_defs_inside_a_match_case_become_symbols():
    syms = _by_id(
        "def pick(x):\n"
        "    match x:\n"
        "        case 1:\n"
        "            def inner():\n                ...\n",
        module="m",
    )
    assert "m.pick.inner" in syms


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
