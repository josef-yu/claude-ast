"""Cross-file id uniqueness — the ``finalize`` pass.

Per-file ingest can't see that a submodule ``pkg/helpers.py`` and a same-named class in
``pkg/__init__.py`` both mint ``pkg.helpers`` (and their whole subtrees collide). Left alone the
graph silently clobbers one and duplicates ids in its name/child indexes. ``finalize`` makes ids
globally unique — the module (an import target) keeps the canonical id, the colliding member is
suffixed ``#N`` — so both constructs survive and the graph stays consistent.
"""

from claude_ast.index import Index


def _build(tmp_path, files: dict) -> Index:
    for rel, src in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
    return Index.build(tmp_path, use_store=False)


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


def test_no_collision_leaves_ids_untouched(tmp_path):
    # The common case: distinct qualnames -> plain dotted ids, no #N anywhere.
    index = _build(tmp_path, {
        "pkg/__init__.py": "",
        "pkg/a.py": "def f():\n    ...\n",
        "pkg/b.py": "class C:\n    def m(self):\n        ...\n",
    })
    assert all("#" not in s.id for s in index.graph.symbols())
    assert {d.id for d in index.find_definition("pkg.b.C")} == {"pkg.b.C"}
