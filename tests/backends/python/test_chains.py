"""Python backend — external call-chain resolution through the typeshed tables (finding #2).

A module-rooted call chain that stays in module namespace (``os.path.join``) is a definite
external fact; one that crosses into a value whose members are type-dependent
(``sys.stdout.getvalue``) must NOT be a definite edge. The evaluator keeps the module-fact
prefix definite, downgrades a real value-type member to a possible STUB edge, and declines a
member absent on the declared type. Unit tests pin the evaluator; integration tests drive it
through the whole engine.
"""

from claude_ast.index import Index
from claude_ast.ingest.python.chains import KEEP, chain_return_type, resolve_external_chain
from claude_ast.ingest.python.stubs import STDLIB_STUBS
from claude_ast.model import Confidence

S = STDLIB_STUBS


def test_module_fact_chains_keep_definite() -> None:
    # every hop is module namespace -> the definite external edge stands
    assert resolve_external_chain("os.path.join", S) == KEEP
    assert resolve_external_chain("os.getcwd", S) == KEEP
    assert resolve_external_chain("builtins.len", S) == KEEP


def test_value_member_downgrades_to_a_stub_target() -> None:
    # sys.stdout is a value typed TextIO; `write` IS a TextIO member -> a possible STUB edge
    # to the type member, not a definite edge to the syntactic chain.
    assert resolve_external_chain("sys.stdout.write", S) == ("stub", "typing.TextIO.write")


def test_absent_value_member_declines() -> None:
    # the crux of #2: TextIO has no getvalue (that is io.StringIO's), so the chain declines.
    assert resolve_external_chain("sys.stdout.getvalue", S) is None


def test_unknown_library_root_keeps_definite() -> None:
    # no shape data for a third-party root -> never downgrade (recall guard)
    assert resolve_external_chain("numpy.array", S) == KEEP
    assert resolve_external_chain("requests.get", S) == KEEP


def test_unknown_module_member_keeps_definite() -> None:
    # a module attribute we did not extract must not be dropped -> stays a definite module fact
    assert resolve_external_chain("os.a_member_we_did_not_extract", S) == KEEP


def test_finding_2_declines_end_to_end(tmp_path) -> None:
    (tmp_path / "m.py").write_text("import sys\n\n\ndef f():\n    return sys.stdout.getvalue()\n")
    index = Index.build(tmp_path)
    # no edge at any confidence — not a false definite external anymore
    assert index.find_dependencies("m.f", Confidence.LOW) == []
    assert not index.graph.is_external("sys.stdout.getvalue")


def test_value_member_is_a_possible_external_edge_end_to_end(tmp_path) -> None:
    (tmp_path / "m.py").write_text("import sys\n\n\ndef f():\n    sys.stdout.write('x')\n")
    index = Index.build(tmp_path)
    deps = {(d.id, d.tier, d.external) for d in index.find_dependencies("m.f")}
    assert ("typing.TextIO.write", "possible", True) in deps


def test_module_fact_stays_definite_end_to_end(tmp_path) -> None:
    (tmp_path / "m.py").write_text("import os\n\n\ndef f():\n    os.path.join('a', 'b')\n")
    index = Index.build(tmp_path)
    deps = {(d.id, d.tier, d.external) for d in index.find_dependencies("m.f")}
    assert ("os.path.join", "definite", True) in deps


# --- call-return chaining: `Path.cwd().exists()` ---


def test_chain_return_type_threads_a_call() -> None:
    assert chain_return_type("pathlib.Path.cwd", S) == "pathlib.Path"  # classmethod -> Path
    assert chain_return_type("re.compile", S) == "re.Pattern"          # func return type
    assert chain_return_type("os.getcwd", S) == "builtins.str"
    assert chain_return_type("sys.stdout", S) is None                  # a value isn't callable


def test_call_return_chain_resolves_the_trailing_member(tmp_path) -> None:
    (tmp_path / "m.py").write_text(
        "from pathlib import Path\n\n\ndef f():\n    return Path.cwd().exists()\n"
    )
    deps = {(d.id, d.tier) for d in Index.build(tmp_path).find_dependencies("m.f")}
    assert ("pathlib.Path.cwd", "possible") in deps       # the inner call
    assert ("pathlib.Path.exists", "possible") in deps    # the chained `.exists()` on its return


def test_call_return_chain_declines_an_absent_trailing_member(tmp_path) -> None:
    (tmp_path / "m.py").write_text(
        "from pathlib import Path\n\n\ndef f():\n    return Path.cwd().no_such()\n"
    )
    deps = {d.id for d in Index.build(tmp_path).find_dependencies("m.f")}
    assert "pathlib.Path.cwd" in deps               # inner call still resolves
    assert not any("no_such" in d for d in deps)    # the absent trailing member declines


def test_multi_hop_chain_emits_an_edge_per_call(tmp_path) -> None:
    # re.compile(p).match(s).group() -> compile (definite) + Pattern.match + Match.group (possible)
    (tmp_path / "m.py").write_text(
        "import re\n\n\ndef f():\n    return re.compile('x').match('y').group()\n"
    )
    deps = {(d.id, d.tier) for d in Index.build(tmp_path).find_dependencies("m.f")}
    assert ("re.compile", "definite") in deps
    assert ("re.Pattern.match", "possible") in deps
    assert ("re.Match.group", "possible") in deps


def test_self_return_is_covariant(tmp_path) -> None:
    # Path.cwd().parent.exists(): parent returns Self -> resolves to Path (not the defining
    # PurePath), so `.exists()` (which is on Path, not PurePath) resolves.
    (tmp_path / "m.py").write_text(
        "from pathlib import Path\n\n\ndef f():\n    return Path.cwd().parent.exists()\n"
    )
    deps = {d.id for d in Index.build(tmp_path).find_dependencies("m.f")}
    assert "pathlib.Path.exists" in deps


def test_unannotated_return_is_inferred_intree(tmp_path) -> None:
    # `def make(): return Service()` (no annotation) -> Service; make().inner() resolves.
    src = (
        "class Inner:\n    def run(self): return 1\n\n\n"
        "class Service:\n    def inner(self) -> Inner: return Inner()\n\n\n"
        "def make():\n    return Service()\n\n\n"
        "def f():\n    return make().inner()\n"
    )
    (tmp_path / "m.py").write_text(src)
    deps = {d.id for d in Index.build(tmp_path).find_dependencies("m.f")}
    assert "m.Service.inner" in deps


def test_property_hop_threads_through_an_accessed_member(tmp_path) -> None:
    # Path.cwd().name.upper() : cwd() -> Path, .name (property) -> str, .upper() -> str.upper
    (tmp_path / "m.py").write_text(
        "from pathlib import Path\n\n\ndef f():\n    return Path.cwd().name.upper()\n"
    )
    deps = {d.id for d in Index.build(tmp_path).find_dependencies("m.f")}
    assert "builtins.str.upper" in deps


# --- in-tree call-return chaining, via function return annotations ---

_INTREE = (
    "class Inner:\n    def run(self): return 1\n\n\n"
    "class Service:\n    def inner(self) -> Inner: return Inner()\n\n\n"
    "def make() -> Service:\n    return Service()\n\n\n"
)


def test_intree_call_return_chain(tmp_path) -> None:
    (tmp_path / "m.py").write_text(_INTREE + "def f():\n    return make().inner()\n")
    deps = {(d.id, d.tier) for d in Index.build(tmp_path).find_dependencies("m.f")}
    assert ("m.make", "definite") in deps            # the receiver call
    assert ("m.Service.inner", "possible") in deps   # make() -> Service; .inner() resolves in-tree


def test_intree_multi_hop_chain(tmp_path) -> None:
    (tmp_path / "m.py").write_text(_INTREE + "def f():\n    return make().inner().run()\n")
    deps = {d.id for d in Index.build(tmp_path).find_dependencies("m.f", Confidence.LOW)}
    assert {"m.make", "m.Service.inner", "m.Inner.run"} <= deps  # threads make->Service->Inner


def test_intree_assignment_from_a_call_return(tmp_path) -> None:
    # s = make(); s.inner()  where make() -> Service : s is typed by make's return annotation,
    # so the value-receiver call resolves to the in-tree member (not just a construction).
    (tmp_path / "m.py").write_text(_INTREE + "def f():\n    s = make()\n    return s.inner()\n")
    deps = {(d.id, d.tier) for d in Index.build(tmp_path).find_dependencies("m.f")}
    assert ("m.Service.inner", "possible") in deps


def test_intree_chain_declines_on_an_uninferable_return(tmp_path) -> None:
    # make(x) returns a parameter — no annotation, no inferable constructor — so its return type
    # is unknown and the trailing member can't resolve (only the receiver call does).
    src = _INTREE.replace(
        "def make() -> Service:\n    return Service()", "def make(x):\n    return x"
    )
    (tmp_path / "m.py").write_text(src + "def f():\n    return make(1).inner()\n")
    deps = {d.id for d in Index.build(tmp_path).find_dependencies("m.f", Confidence.LOW)}
    assert "m.make" in deps
    assert "m.Service.inner" not in deps


def test_value_rooted_chain_self_receiver(tmp_path) -> None:
    # self.svc().make().run(): the receiver is a value call (self.svc), resolved via `self`'s
    # class, then the chain threads through the in-tree return types.
    (tmp_path / "m.py").write_text(
        "class Inner:\n    def run(self): return 1\n\n\n"
        "class Service:\n    def make(self) -> Inner: return Inner()\n\n\n"
        "class App:\n    def svc(self) -> Service: return Service()\n"
        "    def use(self):\n        return self.svc().make().run()\n"
    )
    deps = {d.id for d in Index.build(tmp_path).find_dependencies("m.App.use", Confidence.LOW)}
    assert {"m.App.svc", "m.Service.make", "m.Inner.run"} <= deps


def test_intree_chain_survives_a_warm_rebuild(tmp_path, monkeypatch) -> None:
    # Symbol.return_type must round-trip, else warm rebuild loses the in-tree chain edge.
    (tmp_path / "m.py").write_text(_INTREE + "def f():\n    return make().inner()\n")
    monkeypatch.setenv("CLAUDE_AST_CACHE_DIR", str(tmp_path / "cache"))
    cold = {d.id for d in Index.build(tmp_path).find_dependencies("m.f")}
    warm = {d.id for d in Index.build(tmp_path).find_dependencies("m.f")}
    assert "m.Service.inner" in cold and warm == cold


def test_call_return_chain_survives_a_warm_rebuild(tmp_path, monkeypatch) -> None:
    # `then` must round-trip through the store, else the warm rebuild loses the chained edge.
    (tmp_path / "m.py").write_text(
        "from pathlib import Path\n\n\ndef f():\n    return Path.cwd().exists()\n"
    )
    monkeypatch.setenv("CLAUDE_AST_CACHE_DIR", str(tmp_path / "cache"))
    cold = {d.id for d in Index.build(tmp_path).find_dependencies("m.f")}
    warm = {d.id for d in Index.build(tmp_path).find_dependencies("m.f")}
    assert "pathlib.Path.exists" in cold
    assert warm == cold
