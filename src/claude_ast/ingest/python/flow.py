"""Flow-sensitive reassignment tracking.

A reassigned local can hold different types at different points (``x = User(); … ; x = Post()``), so
one type-per-scope is wrong for it. When a function reassigns a *typed* local, ``flow_types``
computes, per TOP-LEVEL statement, the type live at that point (``FLOW``) plus the *other* types the
variable takes elsewhere (``MAY``, the union widening); ``refs._visit``'s body loop swaps that view
in for the statement's refs. Straight-line reassignments are tracked precisely (``x.save()`` sees
the nearest preceding assignment as ``FLOW``, the rest as ``MAY``); a variable reassigned inside
a branch or loop reports the whole may-set as ``FLOW`` at every use (no more-precise answer exists).
Keying by top-level statements is exact: a straight-line variable is by definition not reassigned
inside a compound, so nested refs correctly inherit their enclosing statement's view. Skipped
entirely (no keys) unless a typed local is reassigned — free on the hot path. A sound
over-approximation, not an iterative dataflow solver.
"""

from __future__ import annotations

import ast

from .scope import SCOPES, UNTYPED, RecType, VarType, assign_effect, binder_names


def _branch_reassigned(
    fn: ast.FunctionDef | ast.AsyncFunctionDef, flow_vars: frozenset[str],
    locals_: list[frozenset[str]],
) -> frozenset[str]:
    """The flow variables reassigned anywhere other than a top-level simple assignment (inside an
    ``if`` / loop / ``with`` / ``try`` body, or via a for-target / unpacking). Their type can't be
    read positionally from the top-level walk, so they report the whole may-set at every use."""
    branch: set[str] = set()
    for stmt in fn.body:
        if assign_effect(stmt, locals_) is not None:
            continue  # a top-level simple assignment — straight-line eligible
        branch |= binder_names([stmt]) & flow_vars
    return frozenset(branch)


def _may_types(
    fn: ast.FunctionDef | ast.AsyncFunctionDef, flow_vars: frozenset[str],
    locals_: list[frozenset[str]],
) -> dict[str, VarType]:
    """Each flow variable -> the union of every type it is assigned anywhere (its may-set). Untyped
    assignments contribute no name (they don't erase the known arms — a member call fans out to all
    of them); a variable with no nameable type maps to ``UNTYPED``. Nested scopes not entered."""
    acc: dict[str, list[VarType]] = {}

    def scan(node: ast.AST) -> None:
        if isinstance(node, SCOPES):
            return
        eff = assign_effect(node, locals_)
        if eff is not None:
            for name in eff[0]:
                if name in flow_vars:
                    acc.setdefault(name, []).append(eff[1])
        for child in ast.iter_child_nodes(node):
            scan(child)

    for stmt in fn.body:
        scan(stmt)
    return {n: _union(vs) for n, vs in acc.items()}


def _union(vals: list[VarType]) -> VarType:
    """Union receiver types across paths, dropping untyped arms (they add no name): the deduped
    union of type names, INFERENCE if any arm was inferred. ``UNTYPED`` when no arm named a type."""
    names: list[str] = []
    inferred = False
    for types, inf in vals:
        names.extend(types)
        inferred = inferred or inf
    seen: set[str] = set()
    merged = tuple(n for n in names if n not in seen and not seen.add(n))
    return (merged, inferred)


def flow_types(
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
    base: dict[str, RecType],
    locals_: list[frozenset[str]],
    flow_vars: frozenset[str],
) -> dict[int, dict[str, RecType]]:
    """Per-top-level-statement receiver-type overrides for reassigned locals, keyed by ``id(stmt)``
    — see the module note. ``flow_vars`` (from ``function_scope``) is the gate: empty -> no
    overrides. Every override entry has ``flow=True``; a straight-line variable's ``may`` is the
    types it takes at *other* positions, a branch variable's whole may-set is its ``FLOW`` type."""
    if not flow_vars:
        return {}
    branch = _branch_reassigned(fn, flow_vars, locals_)
    straight = flow_vars - branch
    may = _may_types(fn, flow_vars, locals_)
    # A branch variable's override is position-independent (its whole may-set is the FLOW answer at
    # every use), so build it once; only straight variables vary by statement.
    branch_rec = {v: RecType(*may.get(v, UNTYPED), (), True) for v in branch}
    env = {v: _entry_type(base, v) for v in straight}  # straight vars start at their entry type
    out: dict[int, dict[str, RecType]] = {}
    for stmt in fn.body:
        rec: dict[str, RecType] = dict(branch_rec)
        for v in straight:
            live_types, live_inf = env[v]
            full_types, _ = may.get(v, UNTYPED)
            widen = tuple(t for t in full_types if t not in live_types)
            rec[v] = RecType(live_types, live_inf, widen, True)
        out[id(stmt)] = rec
        eff = assign_effect(stmt, locals_)
        if eff is not None:
            for name in eff[0]:
                if name in straight:
                    env[name] = eff[1]
    return out


def _entry_type(base: dict[str, RecType], v: str) -> VarType:
    """A straight-line variable's type at function entry (from a parameter annotation, else untyped)
    — the value it holds before its first in-body assignment."""
    rec = base.get(v)
    return (rec.types, rec.inferred) if rec is not None else UNTYPED
