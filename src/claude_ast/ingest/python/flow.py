"""Flow-sensitive reassignment tracking.

A reassigned local can hold different types at different points (``x = User(); … ; x = Post()``), so
one type-per-scope is wrong for it. When a function reassigns a *typed* local, ``flow_types``
computes the type live at the entry of each TOP-LEVEL statement, and ``refs._visit``'s body loop
swaps that view in for the statement's refs. Straight-line reassignments are tracked precisely
(``x.save()`` sees the nearest preceding assignment); a variable also reassigned inside a branch or
loop instead reports the UNION of all its types at every use — an honest may-set (the member call
fans out), never a wrong-exclusive claim. Keying by top-level statements is exact: a straight-line
variable is by definition not reassigned inside a compound, so nested refs correctly inherit their
enclosing statement's view. Skipped entirely (no keys) unless a typed local is reassigned — free on
the hot path. A sound over-approximation, not an iterative dataflow solver.
"""

from __future__ import annotations

import ast

from .scope import SCOPES, UNTYPED, VarType, assign_effect, binder_names


def _branch_reassigned(
    fn: ast.FunctionDef | ast.AsyncFunctionDef, flow_vars: frozenset[str],
    locals_: list[frozenset[str]],
) -> frozenset[str]:
    """The flow variables reassigned anywhere other than a top-level simple assignment (inside an
    ``if`` / loop / ``with`` / ``try`` body, or via a for-target / unpacking). Their type can't be
    read positionally from the top-level walk, so they fall back to the union of all their types."""
    branch: set[str] = set()
    for stmt in fn.body:
        if assign_effect(stmt, locals_) is not None:
            continue  # a top-level simple assignment — straight-line eligible
        branch |= binder_names([stmt]) & flow_vars
    return frozenset(branch)


def _may_set(
    fn: ast.FunctionDef | ast.AsyncFunctionDef, branch_vars: frozenset[str],
    locals_: list[frozenset[str]],
) -> dict[str, VarType]:
    """Each branch-reassigned variable -> the union of every type it is assigned (an honest may-set;
    a member call fans out). A variable with an untypeable assignment declines (untyped: we can't
    bound it). Nested scopes are their own; not descended."""
    acc: dict[str, list[VarType]] = {}

    def scan(node: ast.AST) -> None:
        if isinstance(node, SCOPES):
            return
        eff = assign_effect(node, locals_)
        if eff is not None:
            for name in eff[0]:
                if name in branch_vars:
                    acc.setdefault(name, []).append(eff[1])
        for child in ast.iter_child_nodes(node):
            scan(child)

    for stmt in fn.body:
        scan(stmt)
    return {n: _join_types(vs) for n, vs in acc.items()}


def _join_types(vals: list[VarType]) -> VarType:
    """Union receiver types across paths: an untyped arm makes the whole join untyped (we can't be
    sure), else the deduped union of names with INFERENCE provenance if any arm was inferred."""
    if not vals or any(not v[0] for v in vals):
        return UNTYPED
    names: list[str] = []
    for v in vals:
        names.extend(v[0])
    seen: set[str] = set()
    merged = tuple(n for n in names if n not in seen and not seen.add(n))
    return (merged, any(v[1] for v in vals))


def flow_types(
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
    base: dict[str, tuple[tuple[str, ...], bool]],
    locals_: list[frozenset[str]],
    flow_vars: frozenset[str],
) -> dict[int, dict[str, VarType]]:
    """Per-top-level-statement type overrides for reassigned locals, keyed by ``id(stmt)`` — see the
    module note. ``flow_vars`` (from ``function_scope``) is the gate: empty -> no overrides."""
    if not flow_vars:
        return {}
    branch = _branch_reassigned(fn, flow_vars, locals_)
    may = _may_set(fn, branch, locals_)
    straight = flow_vars - branch
    env = {v: base.get(v, UNTYPED) for v in straight}  # straight vars start at their entry type
    out: dict[int, dict[str, VarType]] = {}
    for stmt in fn.body:
        out[id(stmt)] = {**env, **may}  # straight (positional) + branch (may-set) — disjoint
        eff = assign_effect(stmt, locals_)
        if eff is not None:
            for name in eff[0]:
                if name in straight:
                    env[name] = eff[1]
    return out
