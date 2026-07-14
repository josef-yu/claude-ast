"""Python backend — the module-level import graph (IMPORT edges + reverse `importers`).

The reverse direction is the point: a module's importers spell it many ways (`import a` /
`from a import x` / relative `from ..a import x`), all resolved to one qualname. Only in-tree
module targets become edges; function-local and external imports are excluded.
"""

from claude_ast.index import Index


def test_importers_are_the_reverse_import_graph(tmp_path):
    (tmp_path / "a.py").write_text("def x():\n    return 1\n")
    (tmp_path / "b.py").write_text("from a import x\n\n\ndef y():\n    return x()\n")
    index = Index.build(tmp_path)

    assert {(r.id, r.kind) for r in index.find_importers("a")} == {("b", "import")}
    assert ("a", "import") in {(d.id, d.kind) for d in index.find_dependencies("b")}


def test_relative_import_resolves_in_the_import_graph(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "core.py").write_text("class Base:\n    ...\n")
    (pkg / "svc.py").write_text("from .core import Base\n")
    index = Index.build(tmp_path)

    # `from .core import Base` doesn't textually contain `pkg.core`, but binding resolved it.
    assert {r.id for r in index.find_importers("pkg.core")} == {"pkg.svc"}


def test_circular_imports_are_recorded_without_breaking(tmp_path):
    # A static graph handles what the runtime can't: both directions are simply edges.
    (tmp_path / "a.py").write_text("from b import beta\n\n\ndef alpha():\n    return 1\n")
    (tmp_path / "b.py").write_text("from a import alpha\n\n\ndef beta():\n    return 1\n")
    index = Index.build(tmp_path)

    assert {r.id for r in index.find_importers("a")} == {"b"}
    assert {r.id for r in index.find_importers("b")} == {"a"}  # the cycle, faithfully, no loop


def test_external_import_is_not_an_in_tree_edge(tmp_path):
    (tmp_path / "m.py").write_text("import os\n\n\ndef f():\n    return os.getcwd()\n")
    index = Index.build(tmp_path)

    assert index.find_importers("os") == []
    assert not [d for d in index.find_dependencies("m") if d.kind == "import"]


def test_from_parent_import_submodule_registers_the_submodule(tmp_path):
    # `from pkg import base` imports the SUBMODULE pkg.base — not only the package pkg. The
    # recall gap the eval surfaced: without this, `importers pkg.base` missed such importers.
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "base.py").write_text("class Base:\n    ...\n")
    (pkg / "user.py").write_text("from pkg import base\n\n\ndef f():\n    return base.Base\n")
    index = Index.build(tmp_path)

    assert {r.id for r in index.find_importers("pkg.base")} == {"pkg.user"}  # the submodule
    assert "pkg.user" in {r.id for r in index.find_importers("pkg")}         # and the package


def test_from_import_of_a_submodule_carries_its_span(tmp_path):
    # The submodule edge must locate its import statement (importers --source depends on it).
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "sub.py").write_text("")
    (tmp_path / "main.py").write_text("import os\nfrom pkg import sub\n")
    index = Index.build(tmp_path)

    (ref,) = index.find_importers("pkg.sub")
    assert ref.id == "main" and ref.at is not None and ref.at.line == 2


def test_function_local_import_is_not_a_module_dependency(tmp_path):
    (tmp_path / "a.py").write_text("def x():\n    return 1\n")
    (tmp_path / "b.py").write_text("def y():\n    from a import x\n    return x()\n")
    index = Index.build(tmp_path)

    assert index.find_importers("a") == []  # a local import is not a module-wide dependency
