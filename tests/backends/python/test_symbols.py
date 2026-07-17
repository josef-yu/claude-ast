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


def test_property_method_is_a_property_kind():
    syms = _by_id(
        "class C:\n    @property\n    def name(self) -> str:\n        return self._n\n", module="m"
    )
    assert syms["m.C.name"].kind is SymbolKind.PROPERTY
    assert syms["m.C.name"].return_type == "str"  # threads a chain like a data attribute


def test_cached_property_is_a_property_kind():
    syms = _by_id(
        "class C:\n    @functools.cached_property\n    def big(self):\n        return 1\n", "m"
    )
    assert syms["m.C.big"].kind is SymbolKind.PROPERTY


def test_staticmethod_is_a_method_flagged_static():
    syms = _by_id("class C:\n    @staticmethod\n    def f(a):\n        return a\n", module="m")
    assert syms["m.C.f"].kind is SymbolKind.METHOD and syms["m.C.f"].is_static is True


def test_plain_method_is_not_static_and_not_a_property():
    m = _by_id("class C:\n    def m(self):\n        ...\n", module="m")["m.C.m"]
    assert m.kind is SymbolKind.METHOD and m.is_static is False


def test_staticmethod_self_assignment_is_not_an_instance_attribute():
    # `@staticmethod def f(self): self.x = 1` — `self` is a plain parameter, so `self.x` is not a
    # class attribute (no decorator tracking would mint a false `C.x`).
    syms = _by_id(
        "class C:\n    @staticmethod\n    def f(self):\n        self.x = 1\n", module="m"
    )
    assert "m.C.x" not in syms


def _iattr(body: str):
    """The instance-attribute symbol `m.C.x` extracted from a class body."""
    return _by_id(f"class C:\n{body}\n", module="m").get("m.C.x")


def test_instance_attribute_is_a_class_member_variable():
    x = _iattr("    def __init__(self):\n        self.x = 1")
    assert x is not None and x.kind is SymbolKind.VARIABLE and x.parent == "m.C"


def test_instance_attribute_typed_by_construction_is_inferred():
    x = _iattr("    def __init__(self):\n        self.x = Widget()")
    assert x is not None and x.return_type == "Widget" and x.return_type_inferred is True


def test_instance_attribute_typed_by_annotation_is_declared():
    x = _iattr("    def __init__(self, w):\n        self.x: Widget = w")
    assert x is not None and x.return_type == "Widget" and x.return_type_inferred is False


def test_instance_attribute_from_a_plain_value_is_untyped_but_captured():
    x = _iattr("    def __init__(self, w):\n        self.x = w")  # a param, not a construction
    assert x is not None and x.return_type is None


def test_instance_attribute_constructor_shadowed_by_a_param_is_untyped():
    # `self.x = Widget()` where Widget is a parameter constructs the param, not the class.
    x = _iattr("    def __init__(self, Widget):\n        self.x = Widget()")
    assert x is not None and x.return_type is None


def test_instance_attribute_constructor_shadowed_by_a_vararg_or_kwarg_is_untyped():
    # a `*args` / `**kwargs` name is a parameter too — a ctor named after one shadows the class.
    star = _iattr("    def __init__(self, *Widget):\n        self.x = Widget()")
    assert star is not None and star.return_type is None
    dstar = _iattr("    def __init__(self, **Widget):\n        self.x = Widget()")
    assert dstar is not None and dstar.return_type is None


def test_instance_attribute_with_conflicting_constructors_is_untyped():
    x = _iattr(
        "    def __init__(self, f):\n"
        "        self.x = Widget()\n"
        "        if f:\n            self.x = Gadget()"
    )
    assert x is not None and x.return_type is None


def test_none_then_construction_keeps_the_constructed_type():
    # `self.x = None` is an Optional path and must not poison a later construction.
    x = _iattr("    def __init__(self):\n        self.x = None\n        self.x = Widget()")
    assert x is not None and x.return_type == "Widget" and x.return_type_inferred is True


def test_nested_attribute_assignment_is_not_a_class_attribute():
    # `self.x.y = 1` sets on self.x; it must not mint an `x` (or `y`) class member.
    syms = _by_id("class C:\n    def m(self):\n        self.x.y = 1\n", module="m")
    assert "m.C.x" not in syms and "m.C.y" not in syms


def test_a_class_level_variable_wins_over_a_same_named_instance_attribute():
    # A class-level declaration is authoritative; the instance assignment doesn't duplicate it.
    syms = _by_id(
        "class C:\n    x: int = 0\n    def __init__(self):\n        self.x = object()\n", module="m"
    )
    assert syms["m.C.x"].return_type == "int"  # the class-level annotation, not the instance value
    assert "m.C.x#2" not in syms
