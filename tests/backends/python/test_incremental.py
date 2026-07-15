"""Incremental resolve == full resolve, under a fuzz of edits.

The session's ``IncrementalResolver`` recomputes only a dirty file set on each patch and reuses
cached per-file edges for the rest. A dirty set that is too small would silently corrupt the
graph, so this is the load-bearing guard: apply seeded random edits that exercise both coupling
channels (imports + the heuristic name-match, including its count-preserving id/order changes and
file deletion), and after each one assert the patched session's graph is byte-identical to a
graph built from scratch. Deterministic (seeded), so a divergence reproduces exactly.
"""

import random

import pytest

from claude_ast.index import Index, IndexSession
from claude_ast.model import Confidence

# Filler modules so a small regression project has enough files that a 1-2 module dirty set stays
# under the resolver's len(files)//2 full-resolve fallback — i.e. the incremental path is actually
# exercised, not silently short-circuited to a full rebuild.
_PAD = {f"pad{i}.py": f"def a{i}():\n    return {i}\n" for i in range(8)}


def _base_src(v: int) -> str:
    # Animal is imported by pkg.models (stable name); Widget{v} is renamed on each `rename` edit —
    # a count-preserving id change to the heuristically-matched methods `frob`/`speak`.
    return (
        "class Animal:\n"
        "    def speak(self):\n        ...\n"
        "    def name(self) -> str:\n        return 'a'\n\n"
        f"class Widget{v}:\n"
        "    def frob(self):\n        ...\n"
        "    def speak(self):\n        ...\n"
    )


_BASE = {
    "pkg/__init__.py": "",
    "pkg/base.py": _base_src(0),
    "pkg/models.py": (
        "from pkg.base import Animal\n\n"
        "class Dog(Animal):\n"
        "    def speak(self):\n        return 'woof'\n\n"
        "class Cat(Animal):\n"
        "    def speak(self):\n        return 'meow'\n"
    ),
    "pkg/service.py": (
        "from pkg.models import Dog, Cat\n"
        "from pkg.base import Animal\n\n"
        "def make() -> Dog:\n    return Dog()\n\n"
        "def run(a: Animal):\n"
        "    a.speak()\n"
        "    d = Dog()\n"
        "    d.speak()\n"
        "    obj.speak()\n"  # untyped receiver -> heuristic name-match on 'speak'
    ),
    # Imports NOTHING, yet calls frob()/speak()/name() on untyped receivers — so it is reachable
    # ONLY through the heuristic name-match channel, never the reverse-import closure. This is what
    # makes a count-preserving rename of a matched method's owner observable to the fuzz.
    "isolated.py": "def probe(x):\n    x.frob()\n    x.speak()\n    x.name()\n",
    "app.py": (
        "from pkg.service import make, run\n\n"
        "def main():\n"
        "    run(make())\n"
        "    thing.name()\n"  # untyped -> heuristic on 'name'
    ),
}


def _canonical(index: Index) -> str:
    """Insertion-order dump of the whole graph + coverage — the compared byte surface."""
    g = index.graph
    lines = []
    for s in g.symbols():
        sp = s.span
        lines.append(f"S\t{s.id}\t{s.kind.value}\t{sp.file}:{sp.line}:{sp.col}\t"
                     f"{s.parent}\t{s.return_type}\t{int(s.return_type_inferred)}")
    for s in g.symbols():
        for e in g.out_edges(s.id):
            at = f"{e.at.file}:{e.at.line}:{e.at.col}" if e.at else "-"
            lines.append(f"E\t{e.src}\t{e.dst}\t{e.kind.value}\t"
                         f"{e.resolution.source.value}/{e.resolution.confidence.value}\t{at}")
    lines += [f"X\t{x.id}" for x in g.externals()]
    m = index.metrics
    lines.append(f"M\t{m.total_refs}\t{m.bound_refs}")
    return "\n".join(lines)


def _edit(rng: random.Random, files: dict, n: int):
    """One mutation. Returns (rel, src) to write, or (rel, None) to delete. Each kind targets a
    specific coupling channel — including the id/order and deletion cases a purely additive menu
    (the earlier version) structurally could not generate."""
    kinds = ["body", "add_method", "add_func", "add_call", "add_import", "add_module", "rename"]
    mods = [p for p in files if p.startswith("pkg/mod")]
    if mods:
        kinds.append("delete")
    kind = rng.choice(kinds)
    if kind == "body":  # no-op body change -> pure reuse (surface unchanged)
        rel = rng.choice([p for p in files if p.endswith(".py")])
        return rel, files[rel] + f"\n# c{n}\n"
    if kind == "rename":  # count-preserving id change to heuristically-matched frob/speak
        return "pkg/base.py", _base_src(n + 1)
    if kind == "add_method":  # grows the 'speak' name population -> heuristic coupling + cap
        add = f"\nclass E{n}:\n    def speak(self):\n        ...\n"
        return "pkg/base.py", files["pkg/base.py"] + add
    if kind == "add_func":  # changes all_ids
        rel = rng.choice(["pkg/service.py", "app.py"])
        return rel, files[rel] + f"\ndef helper{n}():\n    return 1\n"
    if kind == "add_call":  # changes only this file's refs
        return "app.py", files["app.py"] + f"\ndef caller{n}():\n    make()\n"
    if kind == "add_import":  # reverse-import coupling
        return "app.py", "from pkg.base import Animal\n" + files["app.py"]
    if kind == "delete":  # removes a file others may import -> deleted-set + reverse-import
        return rng.choice(mods), None
    return f"pkg/mod{n}.py", f"from pkg.models import Dog\n\ndef use{n}():\n    Dog().speak()\n"


def _apply(root, files: dict, rel: str, src) -> None:
    p = root / rel
    if src is None:
        p.unlink()
        files.pop(rel, None)
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(src)
    files[rel] = src


@pytest.mark.parametrize("seed", range(8))
def test_incremental_patch_matches_full_rebuild(tmp_path, seed):
    rng = random.Random(seed)
    files = dict(_BASE)
    for rel, src in files.items():
        _apply(tmp_path, files, rel, src)

    session = IndexSession(tmp_path)  # cold seed -> full resolve, populates the reuse cache
    for step in range(12):
        rel, src = _edit(rng, dict(files), step)
        _apply(tmp_path, files, rel, src)

        session.patch()  # incremental: recompute only the dirty set
        fresh = Index.build(tmp_path, use_store=False)  # from scratch: full resolve
        assert _canonical(session.current) == _canonical(fresh), (
            f"seed={seed} step={step} rel={rel}: incremental graph diverged from full rebuild"
        )


def test_rename_of_heuristic_target_reresolves_untyped_non_importer(tmp_path):
    # Regression: an untyped caller that does NOT import the renamed module is reachable only via
    # the heuristic name-match; a count-preserving class rename changes the matched method's id, so
    # the dirty set must track method IDs, not just per-name counts, or the cached edge dangles.
    for rel, src in _PAD.items():
        (tmp_path / rel).write_text(src)
    (tmp_path / "a.py").write_text("class Widget:\n    def frobnicate(self):\n        return 1\n")
    (tmp_path / "b.py").write_text("def use(x):\n    x.frobnicate()\n")  # imports nothing
    session = IndexSession(tmp_path)
    # heuristic edges are LOW-confidence, so query with min_confidence=LOW to see them
    refs = session.current.find_references("a.Widget.frobnicate", Confidence.LOW)
    assert any(r.id == "b.use" for r in refs)  # heuristic edge from the non-importer

    (tmp_path / "a.py").write_text("class Gadget:\n    def frobnicate(self):\n        return 1\n")
    session.patch()
    fresh = Index.build(tmp_path, use_store=False)
    assert _canonical(session.current) == _canonical(fresh)
    # the stale target is gone, the new one is bound
    assert session.current.find_references("a.Widget.frobnicate", Confidence.LOW) == []
    gadget = session.current.find_references("a.Gadget.frobnicate", Confidence.LOW)
    assert any(r.id == "b.use" for r in gadget)


def test_deleting_an_imported_module_reresolves_its_importers(tmp_path):
    # Regression: deleting a module must re-resolve files that imported it (its edges to the gone
    # symbols must drop) — exercises the `deleted` set + reverse-import closure on the patch path.
    for rel, src in _PAD.items():
        (tmp_path / rel).write_text(src)
    (tmp_path / "lib.py").write_text("def helper():\n    return 1\n")
    (tmp_path / "app.py").write_text("from lib import helper\n\ndef use():\n    helper()\n")
    session = IndexSession(tmp_path)
    assert any(r.id == "app.use" for r in session.current.find_references("lib.helper"))

    (tmp_path / "lib.py").unlink()
    session.patch()
    fresh = Index.build(tmp_path, use_store=False)
    assert _canonical(session.current) == _canonical(fresh)
    assert session.current.find_definition("lib.helper") == []
