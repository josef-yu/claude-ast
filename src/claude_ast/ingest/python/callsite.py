"""Call-site type observations — the deterministic 'lint' half of the value layer.

Where the type resolvers infer what a receiver call *dispatches to* (MEDIUM/possible —
open-world subclassing keeps it uncertain), this pass reports what a call site *passes*:
``g(User())`` means the function ``g`` was observed receiving a ``User`` at that argument
position. That is a syntactic fact, not a dispatch guess, so it mints a **definite**
``RECEIVES_ARG`` edge (``g -> User``) — the honest home for the ``definite`` label the
escalation debate kept trying to pin on the wrong (dispatch) edge.

Lint-grade and one-hop by design: it observes a construction directly present at a call
site and stops. It never propagates an observed type *forward* (into calls the parameter
then makes) or unifies across sites — that road is type inference, which the engine
deliberately does not walk. Scope this increment: bare-name **FUNCTION** callees only (so
arg position *k* aligns with param *k* with no ``self`` offset), positional
class-construction args only. Constructor (``Widget()``) and method (``obj.m()``) callees,
keyword / ``*args`` positions, and non-construction args are deferred.
"""

from __future__ import annotations

from collections.abc import Sequence

from ...model import Edge, EdgeKind, Resolution, SymbolKind
from ..product import FileIndex
from .binding import bind, resolve_type_name
from .resolve_index import ResolveIndex


def observe_arg_types(
    files: Sequence[FileIndex],
    ctx: ResolveIndex,
) -> list[Edge]:
    """Definite ``RECEIVES_ARG`` edges from the concrete types seen at call sites.

    For each name-callee ``CALL`` ref carrying ``arg_types``, resolve the callee through
    ``bind`` (the one name-resolution authority) and keep it only if it is an in-tree
    FUNCTION; then resolve each construction's class in the *caller's* scope and emit a
    ``callee -> class`` edge at the call-site span. Iterates files/refs in the given
    (sorted) order, so the edge list is deterministic and warm rebuilds reproduce it.
    """
    by_id, all_ids = ctx.by_id, ctx.all_ids
    internal_roots, reexports = ctx.internal_roots, ctx.reexports
    out: list[Edge] = []
    for fi in files:
        module_defs = ctx.module_defs[fi.module]
        for ref in fi.refs:
            if ref.local_root or ref.kind is not EdgeKind.CALL or not ref.arg_types:
                continue
            bound = bind(ref.name, module_defs, fi.imports, all_ids, internal_roots, reexports)
            if bound is None:
                continue
            callee_id, is_external = bound
            callee = by_id.get(callee_id)
            # Functions only: a class callee (constructor) and a method callee both shift
            # arg-index vs param-index by the receiver, which this pass does not model yet;
            # an external callee has no in-tree parameter to profile.
            if is_external or callee is None or callee.kind is not SymbolKind.FUNCTION:
                continue
            for type_name in ref.arg_types:
                if type_name is None:
                    continue
                type_id = resolve_type_name(
                    type_name, module_defs, fi.imports, all_ids, reexports, by_id
                )
                if type_id is not None:
                    kind = EdgeKind.RECEIVES_ARG
                    out.append(Edge(callee_id, type_id, kind, Resolution.observed(), ref.at))
    return out
