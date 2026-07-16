"""The module-tree adjacency — submodules are children of their package.

A module symbol carries its package as ``parent`` (``pkg.helpers`` -> ``pkg``), so the graph is a
real package -> module -> member tree rather than a flat set of parentless module roots. The
neutral walks key off ``SymbolKind.MODULE`` (a member belongs to its own submodule, not the
package above it), and ``outline`` treats a child submodule as a boundary, not a member.
"""

from claude_ast.index import Index
from claude_ast.model import Graph, Symbol
from claude_ast.query.repomap import _module_and_depth


def _build(tmp_path, files: dict) -> Index:
    for rel, src in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
    return Index.build(tmp_path, use_store=False)


def _sym(g: Graph, sid: str) -> Symbol:
    s = g.symbol(sid)
    assert s is not None, f"missing symbol {sid!r}"
    return s


_PKG = {
    "pkg/__init__.py": "VERSION = 1\n",
    "pkg/models.py": "class User:\n    def save(self):\n        ...\n",
    "pkg/sub/__init__.py": "",
    "pkg/sub/deep.py": "def helper():\n    ...\n",
}


def test_submodules_are_children_of_their_package(tmp_path):
    g = _build(tmp_path, _PKG).graph
    # module symbols carry their package qualname as parent; top-level package -> None
    assert _sym(g, "pkg").parent is None
    assert _sym(g, "pkg.models").parent == "pkg"
    assert _sym(g, "pkg.sub").parent == "pkg"
    assert _sym(g, "pkg.sub.deep").parent == "pkg.sub"
    # the package's children include its submodules (the tree adjacency)
    kids = {c.id for c in g.children("pkg")}
    assert {"pkg.models", "pkg.sub", "pkg.VERSION"} <= kids
    assert {c.id for c in g.children("pkg.sub")} == {"pkg.sub.deep"}


def test_outline_shows_own_members_and_submodules_as_collapsed_leaves(tmp_path):
    index = _build(tmp_path, _PKG)
    entries = index.outline("pkg")
    ids = {e.id for e in entries}
    assert "pkg.VERSION" in ids  # own members shown
    assert {"pkg.models", "pkg.sub"} <= ids  # submodules present — but as leaves...
    # ...i.e. named, NOT descended into (their members are absent from pkg's outline).
    assert "pkg.models.User" not in ids and "pkg.sub.deep.helper" not in ids
    # a submodule outlines its own members in full.
    assert {e.id for e in index.outline("pkg.models")} == {"pkg.models", "pkg.models.User",
                                                            "pkg.models.User.save"}


def test_outline_focus_reveals_only_the_focused_submodule(tmp_path):
    index = _build(tmp_path, _PKG)
    # focus a symbol under pkg.models: that submodule expands (its members appear), while the
    # unrelated pkg.sub stays a collapsed leaf.
    entries = index.outline("pkg", focus="pkg.models.User.save")
    ids = {e.id for e in entries}
    assert {"pkg.models.User", "pkg.models.User.save"} <= ids  # the focused branch is revealed
    assert "pkg.sub" in ids and "pkg.sub.deep.helper" not in ids  # the other submodule stays a leaf
    assert [e.id for e in entries if e.id == "pkg.models.User.save"]  # focus itself present

    # a focus that is not under the module degrades to the shallow view (no error, no expansion).
    shallow = {e.id for e in index.outline("pkg", focus="some.other.thing")}
    assert "pkg.models.User" not in shallow and "pkg.models" in shallow


def test_outline_focus_edge_cases(tmp_path):
    index = _build(tmp_path, _PKG)
    # a multi-level subpackage spine expands every submodule ON the path (pkg.sub -> pkg.sub.deep)
    ids = {e.id for e in index.outline("pkg", focus="pkg.sub.deep.helper")}
    assert {"pkg.sub", "pkg.sub.deep", "pkg.sub.deep.helper"} <= ids
    assert "pkg.models.User" not in ids  # the off-path submodule stays a collapsed leaf
    # focus == the module itself -> just the shallow view (nothing to expand)
    assert {e.id for e in index.outline("pkg", focus="pkg")} == {e.id for e in index.outline("pkg")}
    # focus == a direct submodule -> that submodule expands
    assert "pkg.models.User" in {e.id for e in index.outline("pkg", focus="pkg.models")}


def test_member_attributes_to_its_own_submodule_not_the_package(tmp_path):
    g = _build(tmp_path, _PKG).graph
    save = _sym(g, "pkg.models.User.save")
    module, depth = _module_and_depth(g, save)
    assert module == "pkg.models"  # the submodule, not the topmost package "pkg"
    assert depth == 2  # save -> User -> pkg.models


def test_namespace_package_gap_reparents_to_nearest_real_package(tmp_path):
    # PEP 420: pkg/ns/ has NO __init__.py, so 'pkg.ns' has no module symbol. finalize corrects the
    # submodule's parent from the missing 'pkg.ns' up to the nearest real package 'pkg', so the
    # subtree stays reachable (up-walk unaffected).
    index = _build(tmp_path, {
        "pkg/__init__.py": "VERSION = 1\n",
        "pkg/ns/leaf.py": "def leaf_fn():\n    ...\n",  # no pkg/ns/__init__.py
    })
    g = index.graph
    assert g.symbol("pkg.ns") is None                    # the namespace package still has no symbol
    assert _sym(g, "pkg.ns.leaf").parent == "pkg"        # corrected: skips the missing 'pkg.ns'
    assert "pkg.ns.leaf" in {c.id for c in g.children("pkg")}      # reachable from the top package
    assert "pkg.ns.leaf" in {e.id for e in index.outline("pkg")}   # and shown by outline (a leaf)
    module, depth = _module_and_depth(g, _sym(g, "pkg.ns.leaf.leaf_fn"))
    assert module == "pkg.ns.leaf" and depth == 1        # up-walk still stops at its own module


def test_submodule_never_parented_under_a_same_named_non_module(tmp_path):
    # A namespace-package dir whose name equals a class in the parent __init__: the class 'sub'
    # (id 'pkg.sub') is not a module, so the submodule 'pkg.sub.mod' must NOT hang off the class —
    # it corrects up to the nearest real package 'pkg'.
    g = _build(tmp_path, {
        "pkg/__init__.py": "class sub:\n    def m(self):\n        ...\n",
        # no pkg/sub/__init__.py, so 'pkg.sub' resolves only to the class
        "pkg/sub/mod.py": "def f():\n    ...\n",
    }).graph
    assert _sym(g, "pkg.sub").kind.value == "class"      # 'pkg.sub' is the class, not a module
    assert _sym(g, "pkg.sub.mod").parent == "pkg"        # the module corrects up past the class
    assert "pkg.sub.mod" not in {c.id for c in g.children("pkg.sub")}  # not mixed into the class
