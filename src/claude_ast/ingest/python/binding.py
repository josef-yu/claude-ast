"""Name/import resolution for the Python backend — shared by syntactic binding and the
type resolvers.

Resolves a reference (or a type name) to an in-tree symbol id, following package
*re-exports*: ``from pkg import X`` where ``pkg/__init__`` re-exports ``X`` from a
submodule. The import binds ``X`` at ``pkg.X``, but the symbol lives where it is defined
(``pkg.sub.X``); following the re-export makes the name bind to the real definition.
"""

from __future__ import annotations

import builtins

from ...model import Span, Symbol, SymbolId, SymbolKind

# Python's builtin names — a name that is neither defined nor imported here, but is a
# builtin, resolves to it (Python's name lookup falls back to builtins). Computed from
# the running interpreter, which the tool pins to a modern Python; builtins are stable.
_BUILTINS = frozenset(dir(builtins))


def bind(
    name: str,
    module_defs: dict[str, str],
    imports: dict[str, str],
    all_ids: set[str],
    internal_roots: set[str],
    reexports: dict[str, dict[str, str]],
) -> tuple[str, bool] | None:
    """Resolve a reference name to ``(target_id, is_external)`` or ``None``.

    Handles a bare name and an attribute chain (``os.path.join``): the root is bound
    via the module's own defs or its imports, then the trailing attribute path is
    appended and classified. A name that resolves to nothing in-scope but is a Python
    builtin (``len``, ``Exception``, ``str.join``) binds to a ``definite`` external
    ``builtins.*`` node — a real reference, checked last so a local ``def len`` wins.
    """
    if name in module_defs:
        return _classify(module_defs[name], all_ids, internal_roots, reexports)
    if name in imports:
        return _classify(imports[name], all_ids, internal_roots, reexports)
    root, _, rest = name.partition(".")
    if rest:
        base = module_defs.get(root) or imports.get(root)
        if base is not None:
            return _classify(f"{base}.{rest}", all_ids, internal_roots, reexports)
    if root in _BUILTINS:
        return f"builtins.{name}", True
    return None


def _classify(
    target: str,
    all_ids: set[str],
    internal_roots: set[str],
    reexports: dict[str, dict[str, str]],
) -> tuple[str, bool] | None:
    """A resolved qualname -> ``(target, is_external)``, or ``None`` to defer to P2.

    A package re-export is followed first, so ``from pkg import X`` binds to X's real
    defining module. An indexed symbol is a definite in-tree edge; a target whose top
    package is not in the project is a definite external edge; a target rooted *in* the
    project but not (yet) a known symbol is a value/dynamic attribute the P2 type
    resolvers own — deferred rather than minted as a bogus external.
    """
    if target not in all_ids:
        target = follow_reexports(target, all_ids, reexports)
    if target in all_ids:
        return target, False
    if target.partition(".")[0] in internal_roots:
        return None
    return target, True


def follow_reexports(target: str, all_ids: set[str], reexports: dict[str, dict[str, str]]) -> str:
    """Follow package re-export aliases until the target names a real symbol.

    ``target`` is ``M.X``; if module ``M`` imports ``X`` (``from .sub import X`` in its
    ``__init__`` or body), then ``M.X`` is an alias for that import's target — follow it.
    Chains are followed with a cycle guard; the original is returned if nothing re-exports it.
    """
    seen: set[str] = set()
    while target not in all_ids and target not in seen:
        seen.add(target)
        container, _, alias = target.rpartition(".")
        nxt = reexports.get(container, {}).get(alias)
        if nxt is None:
            break
        target = nxt
    return target


def resolve_type_name(
    name: str,
    module_defs: dict[str, str],
    imports: dict[str, str],
    all_ids: set[SymbolId],
    reexports: dict[str, dict[str, str]],
    by_id: dict[SymbolId, Symbol],
) -> SymbolId | None:
    """A type name in a file's scope -> its in-tree CLASS id, or ``None``.

    Resolves a bare or dotted name (``User``, ``models.User``) through the file's own
    definitions and imports — the same inputs syntactic binding uses, including package
    re-exports — and keeps it only if it lands on an in-tree class. An external or
    non-class target is ``None``: the members of an unindexed class can't be looked up,
    and a name that resolves to a function/variable is not a type.

    Shared by the value resolvers (an annotated/constructed receiver's type) and the
    call-site pass (an observed argument's constructor), so one authority decides what a
    type name means — the seam holds: no caller parses a dotted id itself.
    """
    target = module_defs.get(name) or imports.get(name)
    if target is None:
        root, _, rest = name.partition(".")
        if rest:
            base = module_defs.get(root) or imports.get(root)
            if base is not None:
                target = f"{base}.{rest}"
    if target is None:
        return None
    target = follow_reexports(target, all_ids, reexports)
    sym = by_id.get(target)
    return target if sym is not None and sym.kind is SymbolKind.CLASS else None


def resolve_external_type_name(
    name: str,
    module_defs: dict[str, str],
    imports: dict[str, str],
    all_ids: set[SymbolId],
    internal_roots: set[str],
    reexports: dict[str, dict[str, str]],
) -> str | None:
    """A type name in a file's scope -> its EXTERNAL qualname, or ``None``.

    The external counterpart of ``resolve_type_name``: resolves ``name`` through the file's
    defs/imports/dotted (plus the builtin fallback, so ``d: dict`` -> ``builtins.dict``),
    then returns the qualname ONLY when it is genuinely external — ``None`` for an in-tree
    symbol (the caller already tried in-tree) or a project-rooted-but-unknown name (a value
    the resolvers own, never a stub target). This is exactly the qualname ``resolve_type_name``
    computes and discards on its external path; the stub layer consults it.
    """
    target = module_defs.get(name) or imports.get(name)
    if target is None:
        root, _, rest = name.partition(".")
        if rest:
            base = module_defs.get(root) or imports.get(root)
            if base is not None:
                target = f"{base}.{rest}"
    if target is None and name in _BUILTINS:
        target = f"builtins.{name}"
    if target is None:
        return None
    target = follow_reexports(target, all_ids, reexports)
    if target in all_ids:
        return None  # in-tree — resolve_type_name owns it
    if target.partition(".")[0] in internal_roots:
        return None  # project-rooted but unknown — defer, never stub a project attr
    return target


def external_symbol(qualname: str) -> Symbol:
    """A leaf node for a library/stdlib target: an edge sink with no in-tree source.

    The id is the imported qualname — versionless, because one Python environment
    resolves each package to a single version (unlike npm). A JS/TS backend is free
    to mint a richer external id; the neutral layer treats it as opaque.
    """
    name = qualname.rsplit(".", 1)[-1]
    return Symbol(qualname, name, SymbolKind.EXTERNAL, Span("<external>", 0))
