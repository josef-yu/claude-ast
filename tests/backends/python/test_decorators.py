"""Python backend — decorator-driven resolution (@property, @staticmethod).

An in-tree ``@property`` behaves like an external stdlib property: read as a value (not called), and
its return type threads a chain. A ``@staticmethod``'s first parameter is not the instance, so a
``self.x`` inside it must not resolve against the enclosing class. Symbol-kind extraction is
test_symbols.py; this pins the resolution consequences.
"""

from claude_ast.index import Index
from claude_ast.model import Confidence


def test_reading_a_property_resolves_to_it(tmp_path):
    (tmp_path / "m.py").write_text(
        "class C:\n"
        "    @property\n    def name(self) -> str:\n        return 'c'\n"
        "    def who(self):\n        return self.name\n"  # READ the property
    )
    index = Index.build(tmp_path)
    deps = {(d.id, d.kind, d.tier) for d in index.find_dependencies("m.C.who")}
    assert ("m.C.name", "reference", "possible") in deps


def test_calling_a_property_resolves_to_nothing(tmp_path):
    # A property is accessed, not called — `self.name()` must NOT resolve to the getter as a call,
    # exactly as an external `p.name()` (a stdlib property call) declines.
    (tmp_path / "m.py").write_text(
        "class C:\n"
        "    @property\n    def name(self):\n        ...\n"
        "    def call_it(self):\n        return self.name()\n"
    )
    index = Index.build(tmp_path)
    assert index.find_dependencies("m.C.call_it", Confidence.LOW) == []


def test_chain_threads_through_a_property_return_type(tmp_path):
    # `self.engine.start()` where `engine` is a @property returning Engine -> Engine.start: a
    # property threads a chain through its return type, like a typed data attribute.
    (tmp_path / "m.py").write_text(
        "class Engine:\n    def start(self):\n        ...\n\n\n"
        "class Car:\n"
        "    @property\n    def engine(self) -> Engine:\n        return Engine()\n"
        "    def go(self):\n        return self.engine.start()\n"
    )
    index = Index.build(tmp_path)
    assert ("m.Engine.start", "call", "possible") in {
        (d.id, d.kind, d.tier) for d in index.find_dependencies("m.Car.go")
    }


def test_untyped_property_intermediate_falls_back_to_low(tmp_path):
    # A property with no threadable return type is still a data value of unknown type, so a chain
    # through it falls back to a LOW name-match on the last member (like an untyped data attribute).
    (tmp_path / "m.py").write_text(
        "class Engine:\n    def start(self):\n        ...\n\n\n"
        "class Car:\n"
        "    @property\n    def engine(self):\n        return make_engine()\n"  # no return type
        "    def go(self):\n        return self.engine.start()\n"
    )
    index = Index.build(tmp_path)
    assert index.find_dependencies("m.Car.go") == []  # below the default floor
    low = {(d.id, d.tier) for d in index.find_dependencies("m.Car.go", Confidence.LOW)}
    assert ("m.Engine.start", "possible") in low


def test_property_is_not_a_call_heuristic_candidate(tmp_path):
    # An untyped `obj.name()` call name-matches methods, never a property (you don't call one).
    (tmp_path / "m.py").write_text(
        "class C:\n    @property\n    def ping(self):\n        ...\n\n\n"
        "def dispatch(obj):\n    return obj.ping()\n"
    )
    index = Index.build(tmp_path)
    assert index.find_dependencies("m.dispatch", Confidence.LOW) == []  # ping is a property


def test_property_is_a_read_heuristic_candidate(tmp_path):
    # An untyped `obj.ping` READ does name-match a property (it's a readable member).
    (tmp_path / "m.py").write_text(
        "class C:\n    @property\n    def ping(self):\n        ...\n\n\n"
        "def sniff(obj):\n    return obj.ping\n"
    )
    index = Index.build(tmp_path)
    deps = {(d.id, d.kind) for d in index.find_dependencies("m.sniff", Confidence.LOW)}
    assert ("m.C.ping", "reference") in deps


def test_self_in_a_staticmethod_does_not_resolve_to_the_class(tmp_path):
    # `@staticmethod def f(self): self.save()` — `self` is a plain parameter, so `self.save` must
    # not resolve to the enclosing class's method (no spurious self-dispatch edge).
    (tmp_path / "m.py").write_text(
        "class C:\n"
        "    def save(self):\n        ...\n"
        "    @staticmethod\n    def f(self):\n        return self.save()\n"
    )
    index = Index.build(tmp_path)
    assert "m.C.f" not in {r.id for r in index.find_callers("m.C.save", Confidence.LOW)}


def test_property_getter_self_still_resolves(tmp_path):
    # A @property getter's `self` IS the instance — `self.save()` inside it resolves to the class.
    (tmp_path / "m.py").write_text(
        "class C:\n"
        "    def save(self):\n        ...\n"
        "    @property\n    def ready(self):\n        return self.save()\n"
    )
    index = Index.build(tmp_path)
    assert "m.C.ready" in {r.id for r in index.find_callers("m.C.save")}
