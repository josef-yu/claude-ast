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


def test_end_to_end_python_source_is_queryable(tmp_path):
    # Backend integration: real Python source flows through parse -> assemble ->
    # query. The query logic itself is proved neutrally in tests/test_query.py.
    (tmp_path / "auth.py").write_text(SRC)
    index = Index.build(tmp_path)

    assert [d.id for d in index.find_definition("auth.User")] == ["auth.User"]
    outline_ids = {e.id for e in index.outline("auth")}
    assert {"auth", "auth.authenticate", "auth.User", "auth.User.save"} <= outline_ids
