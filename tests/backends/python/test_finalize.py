"""Cross-file id uniqueness — the ``finalize`` pass.

Per-file ingest can't see that a submodule ``pkg/helpers.py`` and a same-named class in
``pkg/__init__.py`` both mint ``pkg.helpers`` (and their whole subtrees collide). Left alone the
graph silently clobbers one and duplicates ids in its name/child indexes. ``finalize`` makes ids
globally unique — the module (an import target) keeps the canonical id, the colliding member is
suffixed ``#N`` — so both constructs survive and the graph stays consistent.
"""

from claude_ast.index import Index
from claude_ast.ingest import ingest_project
from claude_ast.ingest.python.finalize import ensure_unique_ids


def _build(tmp_path, files: dict) -> Index:
    for rel, src in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
    return Index.build(tmp_path, use_store=False)


def _ingest(tmp_path, files: dict):
    for rel, src in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
    return ingest_project(tmp_path).files  # raw per-file products, pre-finalize


def test_submodule_and_same_named_member_get_distinct_ids(tmp_path):
    index = _build(tmp_path, {
        "pkg/__init__.py": "class helpers:\n    def run(self):\n        ...\n",
        "pkg/helpers.py": "def run():\n    ...\n\nCONST = 1\n",
    })
    g = index.graph

    ids = [s.id for s in g.symbols()]
    assert len(ids) == len(set(ids))  # no duplicates anywhere in the graph

    # the submodule (import target) keeps the canonical id; the class is the one suffixed.
    canonical = g.symbol("pkg.helpers")
    assert canonical is not None and canonical.kind.value == "module"
    suffixed = g.symbol("pkg.helpers#2")
    assert suffixed is not None and suffixed.kind.value == "class"

    # the cascade is disambiguated too: the submodule's run() and the class's run() are distinct.
    run_ids = sorted(s.id for s in g.by_name("run"))
    assert run_ids == ["pkg.helpers#2.run", "pkg.helpers.run"]
    # ...and the name index has no phantom duplicates.
    assert len(g.by_name("run")) == len(set(s.id for s in g.by_name("run")))


def test_finalize_upholds_the_core_uniqueness_postcondition(tmp_path):
    # The contract every backend's ``finalize`` must satisfy (see ``base.Indexer.finalize``): once
    # assembled, the neutral core sees NO id collision. ``Graph.collisions()`` is the core's
    # tripwire — it records any id two symbols both minted (what the old silent last-write-wins
    # hid). A conforming backend keeps it empty even on a collision-prone layout, because
    # ``finalize`` disambiguated before ``add_symbol`` ever ran. This is the conformance check a
    # second backend's suite replicates against its own id scheme.
    index = _build(tmp_path, {
        "pkg/__init__.py": "class helpers:\n    def run(self):\n        ...\n",
        "pkg/helpers.py": "def run():\n    ...\n",
    })
    assert index.graph.collisions() == []


def test_no_collision_leaves_ids_untouched(tmp_path):
    # The common case: distinct qualnames -> plain dotted ids, no #N anywhere.
    index = _build(tmp_path, {
        "pkg/__init__.py": "",
        "pkg/a.py": "def f():\n    ...\n",
        "pkg/b.py": "class C:\n    def m(self):\n        ...\n",
    })
    assert all("#" not in s.id for s in index.graph.symbols())
    assert {d.id for d in index.find_definition("pkg.b.C")} == {"pkg.b.C"}


def test_finalize_preserves_file_identity_when_nothing_collides(tmp_path):
    # LOAD-BEARING for the incremental cache (Phase C keys "unchanged" off object identity): a
    # collision-free project must be returned as the SAME FileIndex objects, not fresh copies.
    files = _ingest(tmp_path, {
        "pkg/__init__.py": "", "pkg/a.py": "def f():\n    ...\n", "pkg/b.py": "x = 1\n",
    })
    out = ensure_unique_ids(files)
    assert all(o is f for o, f in zip(out, files, strict=True))  # identity, not just equality


def test_finalize_rewrites_only_the_files_a_collision_touched(tmp_path):
    # A collision elsewhere must NOT churn unrelated files (else every patch drops to full resolve).
    files = _ingest(tmp_path, {
        "pkg/__init__.py": "class helpers:\n    def run(self):\n        ...\n",  # the class -> #2
        "pkg/helpers.py": "def run():\n    ...\n",   # submodule keeps its ids -> unchanged
        "pkg/other.py": "def unrelated():\n    ...\n",  # wholly unrelated -> unchanged
    })
    before = {fi.path: fi for fi in files}
    after = {fi.path: fi for fi in ensure_unique_ids(files)}
    p = tmp_path / "pkg"
    init, helpers, other = (str(p / f) for f in ("__init__.py", "helpers.py", "other.py"))
    assert after[init] is not before[init]    # the colliding class subtree was rewritten
    assert after[helpers] is before[helpers]  # its ids were unchanged -> same object
    assert after[other] is before[other]      # unrelated -> same object (reuse survives)


def test_finalize_repoints_refs_of_a_suffixed_symbol(tmp_path):
    # The suffixed class method makes an in-tree call; its edge must attribute to the SUFFIXED id,
    # not a now-nonexistent one — i.e. RawRef.src is repointed and there are no dangling edges.
    src = "def target():\n    ...\n\nclass helpers:\n    def run(self):\n        target()\n"
    index = _build(tmp_path, {
        "pkg/__init__.py": src,
        "pkg/helpers.py": "def run():\n    ...\n",
    })
    callers = {r.id for r in index.find_callers("pkg.target")}
    assert "pkg.helpers#2.run" in callers  # the moved method's ref.src followed it to the #2 id
    g = index.graph
    all_ids = {s.id for s in g.symbols()}
    dangling = [e.dst for s in g.symbols() for e in g.out_edges(s.id)
                if not g.is_external(e.dst) and e.dst not in all_ids]
    assert dangling == []
