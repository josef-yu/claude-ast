"""The normalized model — the central contract of claude-ast.

Every backend populates it, every resolver refines it, every query reads it.
Nothing else in the system touches ``ast`` directly; it all speaks this model.

Kept as slotted dataclasses (not pydantic): indexing creates these in the
millions, so we want the memory and speed of ``__slots__``. Pydantic is reserved
for the MCP/config boundary, where validation earns its keep.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

type SymbolId = str
"""A fully-qualified, disambiguating identifier, e.g. ``auth.models.User.save``."""


class Confidence(StrEnum):
    """How sure we are that an edge is real.

    Originates in type resolution and flows into every query that depends on the
    edge. For a dynamic language, honest confidence beats grep's false precision.
    """

    HIGH = "high"      # resolved — a definite edge
    MEDIUM = "medium"  # inferred with some uncertainty
    LOW = "low"        # a guess (e.g. a name-match heuristic)

    @property
    def tier(self) -> str:
        """The two-tier surface Claude sees: ``definite`` vs ``possible``."""
        return "definite" if self is Confidence.HIGH else "possible"


class ResolutionSource(StrEnum):
    """Where an edge's target came from, roughly ordered by trust."""

    SYNTACTIC = "syntactic"    # imports, direct calls, defs — no types needed
    ANNOTATION = "annotation"  # a PEP 484 annotation resolved the type
    INFERENCE = "inference"    # local inference (``x = Foo()``)
    STUB = "stub"              # a ``.pyi`` / typeshed stub
    CHECKER = "checker"        # an external type-checker (deferred)
    HEURISTIC = "heuristic"    # name-match fallback
    CALLSITE = "callsite"      # observed at call sites


@dataclass(frozen=True, slots=True)
class Resolution:
    """Provenance + confidence for an edge.

    This one value is what makes the resolver stack *additive* and lets
    mixed-typing codebases work without any project-level special-casing.
    """

    source: ResolutionSource
    confidence: Confidence

    @classmethod
    def syntactic(cls) -> Resolution:
        """The always-on, always-certain base — no type resolution needed."""
        return cls(ResolutionSource.SYNTACTIC, Confidence.HIGH)


class SymbolKind(StrEnum):
    MODULE = "module"
    CLASS = "class"
    FUNCTION = "function"
    METHOD = "method"
    VARIABLE = "variable"
    PARAMETER = "parameter"


class EdgeKind(StrEnum):
    """A directed relationship ``src -> dst`` observed at a source location."""

    CONTAINS = "contains"    # a module/class contains a nested symbol
    IMPORT = "import"
    CALL = "call"
    REFERENCE = "reference"  # an attribute read / name use that isn't a call
    INHERITS = "inherits"


@dataclass(slots=True)
class Span:
    """A source location. Lines are 1-based, columns 0-based (matching ``ast``)."""

    file: str
    line: int
    col: int = 0
    end_line: int | None = None
    end_col: int | None = None


@dataclass(slots=True)
class Symbol:
    id: SymbolId
    name: str
    kind: SymbolKind
    span: Span
    signature: str | None = None  # the def/class line, body dropped — for repo_map
    doc: str | None = None        # first line of the docstring — the free "summary"
    parent: SymbolId | None = None


@dataclass(slots=True)
class Edge:
    src: SymbolId
    dst: SymbolId
    kind: EdgeKind
    resolution: Resolution
    at: Span | None = None  # where the edge occurs (e.g. the call site)

    # Heuristic multiplicity is modelled as multiple edges: an unresolved
    # ``obj.save()`` becomes one LOW edge per candidate ``*.save``, each carrying
    # its own resolution. Queries then tier results by ``resolution.confidence``.
