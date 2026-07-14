"""Regression net for the generated typeshed Tier-2 table (``_typeshed_table.py``).

A fast, dependency-free read over the committed frozen literal — no ``typeshed_client``, no
generation. It pins the entries the chain evaluator will build on (especially the ones that
resolve finding #2) and the structural invariants, so a bad regeneration is caught cheaply.
The authoritative freshness check is ``tools/python/gen_typeshed.py check`` (regenerate + diff).
"""

from claude_ast.ingest.python import _typeshed_table as t

_KINDS = {"value", "func", "class", "submodule", "method", "property"}


def test_canonical_entries_resolve_finding_2() -> None:
    # sys.stdout is a VALUE typed TextIO (not a submodule) — the crux of #2.
    assert t.MODULES["sys"]["stdout"] == ("value", "typing.TextIO")
    # ...and TextIO has no getvalue, so `sys.stdout.getvalue()` will decline (no false edge).
    assert "getvalue" not in t.CLASSES["typing.TextIO"]
    # os.path IS a submodule, so `os.path.join(...)` stays a definite module fact.
    assert "os.path" in t.MODULES
    assert t.MODULES["os.path"]["join"][0] == "func"


def test_return_types_enable_chaining() -> None:
    # Path.cwd() -> Self (covariant, resolved to the receiver at chain time), .exists() -> bool.
    assert t.CLASSES["pathlib.Path"]["cwd"] == ("method", "Self")
    assert t.CLASSES["pathlib.Path"]["exists"] == ("method", "builtins.bool")
    # inherited members are MRO-flattened onto the subclass.
    assert "joinpath" in t.CLASSES["pathlib.Path"]  # defined on PurePath


def test_structural_invariants() -> None:
    assert len(t.MODULES) > 100 and len(t.CLASSES) > 500
    for table in (t.MODULES, t.CLASSES):
        for members in table.values():
            for entry in members.values():
                assert isinstance(entry, tuple) and len(entry) == 2
                kind, result = entry
                assert kind in _KINDS
                assert isinstance(result, str)  # a qualname or "" (OPAQUE)


def test_fingerprint_present() -> None:
    assert isinstance(t.FINGERPRINT, str) and len(t.FINGERPRINT) == 16
