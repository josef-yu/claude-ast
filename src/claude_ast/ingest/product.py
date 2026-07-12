"""Per-file ingest products — the stable unit of incremental work.

A ``FileIndex`` is what the ingester produces for one file: the symbols it
defines plus its raw (as-yet-unresolved) references. The resolved ``Graph`` is
derived by merging all FileIndexes, binding references to symbol ids, and
enriching with the resolver stack. Change one file -> swap its FileIndex ->
rebuild the graph. This split is what makes incremental cheap and the
concurrency snapshot-swap natural.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..model import Edge, EdgeKind, Span, Symbol, SymbolId


@dataclass(slots=True)
class RawRef:
    """A reference site, not yet bound to a target symbol.

    ``name`` is the dotted path as written (``foo``, ``os.path.join``,
    ``self.save``). Syntactic binding (P1) resolves names-in-scope and imports.
    ``local_root`` marks a value receiver — the root name is a local (``self``,
    a parameter, a local var) — which syntactic binding must *never* touch (a
    local can shadow an import, so binding it would forge a wrong edge); these
    are left for the type resolvers. ``receiver_type`` is the receiver's annotated
    type name (``User``, ``models.User``) when a parameter annotation gives it —
    the fact the annotation resolver binds; ``None`` when there is no such fact.
    """

    src: SymbolId  # the enclosing symbol making the reference
    kind: EdgeKind
    name: str
    at: Span
    local_root: bool = False
    receiver_type: str | None = None


@dataclass(slots=True)
class FileIndex:
    path: str
    module: SymbolId
    symbols: list[Symbol]
    refs: list[RawRef] = field(default_factory=list)
    # Local name -> target qualname, a backend's alias map used when binding refs
    # that came from another module (e.g. Python imports). Backend-specific in
    # content, generic in shape.
    imports: dict[str, str] = field(default_factory=dict)


type FileStamp = tuple[int, int]
"""A cheap change-detector for a file: (mtime_ns, size). Cache hit => skip reparse."""


@dataclass(slots=True)
class CachedFile:
    """A persisted parse product plus the stamp it was parsed at."""

    stamp: FileStamp
    file: FileIndex


@dataclass(slots=True)
class ProjectIngest:
    files: list[FileIndex]
    skipped: list[str]  # paths we couldn't read/parse (kept out of the index)
    fresh: dict[str, CachedFile]  # newly (re)parsed this run — to persist
    present: set[str]  # every current source path — for pruning deletions


@dataclass(slots=True)
class ResolveResult:
    """A backend's resolution output: resolved edges plus the external
    (library/stdlib) target nodes those edges reference.

    Externals are produced here — not persisted per-file — and rebuilt on every
    graph assembly. The external-id scheme is the backend's own (Python mints a
    bare qualname; a JS/TS backend may encode package/version, since npm allows
    several versions to coexist); the neutral model only ever sees ``EXTERNAL``.
    """

    edges: list[Edge]
    externals: list[Symbol]
