"""Python backend — multi-member value chains (`self.a.b`, `u.a.b()`).

A receiver chain threads one hop per member: each member but the last is *read* as a value and
advances to its declared type (an annotated data attribute), the last member is the target. These
pin the ladder roots (self / annotation / inference), reads vs calls, depth, cross-file threading,
and the honesty boundaries — a method intermediate is a bound method (not its return type), an
untyped receiver can't thread, and an unannotated attribute declines. Single-hop value resolution
is test_resolvers.py; external call-return chains are test_chains.py.
"""

from claude_ast.index import Index
from claude_ast.model import Confidence


def test_self_attribute_chain_read_threads_through_a_typed_attribute(tmp_path):
    (tmp_path / "m.py").write_text(
        "class Engine:\n    rpm = 0\n\n\n"
        "class Car:\n"
        "    engine: Engine\n"
        "    def read(self):\n        return self.engine.rpm\n"
    )
    index = Index.build(tmp_path)
    deps = {(d.id, d.kind, d.tier) for d in index.find_dependencies("m.Car.read")}
    assert ("m.Engine.rpm", "reference", "possible") in deps
    edge = next(e for e in index.graph.out_edges("m.Car.read") if e.dst == "m.Engine.rpm")
    assert edge.resolution.source.value == "inference"  # self-rooted


def test_self_attribute_chain_call_threads_through_a_typed_attribute(tmp_path):
    (tmp_path / "m.py").write_text(
        "class Engine:\n    def start(self):\n        ...\n\n\n"
        "class Car:\n"
        "    engine: Engine\n"
        "    def go(self):\n        return self.engine.start()\n"
    )
    index = Index.build(tmp_path)
    assert ("m.Engine.start", "call", "possible") in {
        (d.id, d.kind, d.tier) for d in index.find_dependencies("m.Car.go")
    }


def test_annotated_receiver_chain_is_all_annotation(tmp_path):
    (tmp_path / "m.py").write_text(
        "class Engine:\n    def start(self):\n        ...\n\n\n"
        "class Car:\n    engine: Engine\n\n\n"
        "def drive(c: Car):\n    return c.engine.start()\n"
    )
    index = Index.build(tmp_path)
    edge = next(e for e in index.graph.out_edges("m.drive") if e.dst == "m.Engine.start")
    assert edge.resolution.confidence.tier == "possible"
    assert edge.resolution.source.value == "annotation"  # every hop declared


def test_constructed_receiver_chain_is_inference(tmp_path):
    (tmp_path / "m.py").write_text(
        "class Engine:\n    def start(self):\n        ...\n\n\n"
        "class Car:\n    engine: Engine\n\n\n"
        "def run():\n    c = Car()\n    return c.engine.start()\n"
    )
    index = Index.build(tmp_path)
    edge = next(e for e in index.graph.out_edges("m.run") if e.dst == "m.Engine.start")
    assert edge.resolution.source.value == "inference"  # a constructed root


def test_three_hop_chain_threads_two_typed_attributes(tmp_path):
    (tmp_path / "m.py").write_text(
        "class Engine:\n    def start(self):\n        ...\n\n\n"
        "class Car:\n    engine: Engine\n\n\n"
        "class Garage:\n"
        "    car: Car\n"
        "    def run(self):\n        return self.car.engine.start()\n"
    )
    index = Index.build(tmp_path)
    assert ("m.Engine.start", "call", "possible") in {
        (d.id, d.kind, d.tier) for d in index.find_dependencies("m.Garage.run")
    }


def test_chain_threads_across_files(tmp_path):
    (tmp_path / "models.py").write_text(
        "class Engine:\n    def start(self):\n        ...\n\n\n"
        "class Car:\n    engine: Engine\n"
    )
    (tmp_path / "app.py").write_text(
        "from models import Car\n\n\ndef drive(c: Car):\n    return c.engine.start()\n"
    )
    index = Index.build(tmp_path)
    assert ("models.Engine.start", "call", "possible") in {
        (d.id, d.kind, d.tier) for d in index.find_dependencies("app.drive")
    }


def test_chain_resolves_the_attribute_type_through_an_in_tree_base(tmp_path):
    # the intermediate attribute's declared type resolves through the class's own defs/imports;
    # the final member resolves through the shared base walk.
    (tmp_path / "m.py").write_text(
        "class Base:\n    def hook(self):\n        ...\n\n\n"
        "class Engine(Base):\n    ...\n\n\n"
        "class Car:\n"
        "    engine: Engine\n"
        "    def go(self):\n        return self.engine.hook()\n"
    )
    index = Index.build(tmp_path)
    assert ("m.Base.hook", "call", "possible") in {
        (d.id, d.kind, d.tier) for d in index.find_dependencies("m.Car.go")
    }


def test_method_intermediate_is_not_threaded_through_its_return_type(tmp_path):
    # `self.make.start()` where `make` is a METHOD (accessed, not called) is a bound method, NOT its
    # return type — threading it through the return would forge a wrong edge, so it declines.
    (tmp_path / "m.py").write_text(
        "class Engine:\n    def start(self):\n        ...\n\n\n"
        "class Car:\n"
        "    def make(self) -> Engine:\n        return Engine()\n"
        "    def go(self):\n        return self.make.start()\n"
    )
    index = Index.build(tmp_path)
    assert index.find_dependencies("m.Car.go", Confidence.LOW) == []


def test_unannotated_attribute_intermediate_declines(tmp_path):
    # `self.engine` has no type annotation, so the chain cannot thread -> no edge (honest miss).
    (tmp_path / "m.py").write_text(
        "class Engine:\n    def start(self):\n        ...\n\n\n"
        "class Car:\n"
        "    def __init__(self):\n        self.engine = Engine()\n"  # instance attr, no symbol/type
        "    def go(self):\n        return self.engine.start()\n"
    )
    index = Index.build(tmp_path)
    assert index.find_dependencies("m.Car.go", Confidence.LOW) == []


def test_untyped_receiver_chain_declines(tmp_path):
    # `obj.a.b` on an untyped receiver has no type to thread -> no edge (heuristic is single-hop).
    (tmp_path / "m.py").write_text(
        "class T:\n    b = 1\n\n\ndef f(obj):\n    return obj.a.b\n"
    )
    index = Index.build(tmp_path)
    assert index.find_dependencies("m.f", Confidence.LOW) == []


def test_calling_a_class_typed_variable_does_not_forge_a_return_edge(tmp_path):
    # `make: Service = Service()` is a Service *instance*, not a factory. Calling it invokes
    # `Service.__call__` (unmodeled) -> NOT a Service, so `make().run()` and `s = make(); s.inner()`
    # must NOT resolve to Service.run/inner. A variable's declared type is a READ type, never a call
    # return, so the call-return resolvers must not thread it (a data-attribute chain would).
    (tmp_path / "svc.py").write_text(
        "class Service:\n    def run(self):\n        ...\n    def inner(self):\n        ...\n\n\n"
        "make: Service = Service()\n"
    )
    (tmp_path / "app.py").write_text(
        "from svc import make\n\n\n"
        "def caller():\n    return make().run()\n\n\n"
        "def caller2():\n    s = make()\n    return s.inner()\n"
    )
    index = Index.build(tmp_path, use_store=False)
    for fn in ("app.caller", "app.caller2"):
        dsts = {d.id for d in index.find_dependencies(fn, Confidence.LOW)}
        assert "svc.Service.run" not in dsts and "svc.Service.inner" not in dsts
        assert "svc.make" in dsts  # the honest edge: a definite call to the module variable

    # a genuine factory FUNCTION (not a variable) still threads correctly through its return type
    (tmp_path / "svc.py").write_text(
        "class Service:\n    def run(self):\n        ...\n\n\n"
        "def make() -> Service:\n    return Service()\n"
    )
    index = Index.build(tmp_path, use_store=False)
    caller_deps = {d.id for d in index.find_dependencies("app.caller", Confidence.LOW)}
    assert "svc.Service.run" in caller_deps


def test_subscripted_attribute_type_does_not_thread(tmp_path):
    # `engines: list[Engine]` is not read as `Engine` (subscript -> no captured type), so the chain
    # declines rather than wrongly threading the element type.
    (tmp_path / "m.py").write_text(
        "class Engine:\n    def start(self):\n        ...\n\n\n"
        "class Car:\n"
        "    engines: list[Engine]\n"
        "    def go(self):\n        return self.engines.start()\n"
    )
    index = Index.build(tmp_path)
    # `engines.start` is a single hop on an untyped-as-far-as-we-know receiver: no chain thread, and
    # `start` name-matches only at LOW (or not at all) — never a possible ANNOTATION/INFERENCE edge.
    sources = {e.resolution.source.value for e in index.graph.out_edges("m.Car.go")}
    assert "annotation" not in sources and "inference" not in sources
