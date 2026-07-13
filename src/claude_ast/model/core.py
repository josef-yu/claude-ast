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

    @property
    def rank(self) -> int:
        """A total order (HIGH > MEDIUM > LOW) so a query can ask for edges *at least*
        this sure — the knob that lets a consumer widen from the reliable default down
        to the low-confidence guesses only when it needs the recall."""
        return {Confidence.HIGH: 2, Confidence.MEDIUM: 1, Confidence.LOW: 0}[self]


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

    @classmethod
    def inferred(cls) -> Resolution:
        """A value-typed inference (e.g. ``self.m()`` -> the enclosing class's member).

        MEDIUM/possible, never definite: the statically-named target is real, but
        polymorphic dispatch means a subclass may override it at runtime. ``self``'s
        receiver type is known *exactly* (the enclosing class) — the most certain of
        the value cases — so a later precedence model must not let a mere annotation
        out-rank it, despite ANNOTATION out-trusting INFERENCE in the source order.
        """
        return cls(ResolutionSource.INFERENCE, Confidence.MEDIUM)

    @classmethod
    def annotated(cls) -> Resolution:
        """A receiver typed by a PEP 484 annotation (``u: User`` -> ``User.save``).

        MEDIUM/possible: an annotation may name a supertype / Protocol / ABC while the
        runtime instance is a subclass overriding the member, so the named target may
        not be the one actually called.
        """
        return cls(ResolutionSource.ANNOTATION, Confidence.MEDIUM)

    @classmethod
    def stubbed(cls) -> Resolution:
        """A member resolved on an external type via a stub (``p: Path; p.exists()``).

        MEDIUM/possible on the same footing as an annotation: the stub confirms the member
        *exists* on the named type, but the annotation may name a supertype a subclass
        overrides, so dispatch stays open. Absent stub data DECLINES — never a guess, never HIGH.
        """
        return cls(ResolutionSource.STUB, Confidence.MEDIUM)

    @classmethod
    def observed(cls) -> Resolution:
        """A type observed flowing into a parameter at a call site (``g(User())`` -> g gets User).

        HIGH/definite — and honestly so, unlike the dispatch resolvers above. This edge
        reports *what was passed*, not *what a method call dispatches to*: a concrete
        construction at a real call site is a syntactic fact, and open-world subclassing
        cannot falsify it (an unobserved caller passing a subclass only *adds* another
        observation, it never retracts this one). The definiteness lives on the
        observation itself — never laundered onto a derived receiver-dispatch edge, which
        stays MEDIUM. That distinction is what keeps this the first non-syntactic definite
        edge that "report, don't rule" actually permits.
        """
        return cls(ResolutionSource.CALLSITE, Confidence.HIGH)

    @classmethod
    def heuristic(cls) -> Resolution:
        """A name-match guess for an untyped receiver (``obj.save()`` -> every ``*.save``).

        LOW/possible: the receiver's type is unknown, so this is one candidate among
        several picked purely by name — the weakest tier, a last resort that still
        reports honestly rather than staying silent.
        """
        return cls(ResolutionSource.HEURISTIC, Confidence.LOW)


class SymbolKind(StrEnum):
    MODULE = "module"
    CLASS = "class"
    FUNCTION = "function"
    METHOD = "method"
    VARIABLE = "variable"
    PARAMETER = "parameter"
    EXTERNAL = "external"  # a library/stdlib target referenced but not indexed (no in-tree source)


class EdgeKind(StrEnum):
    """A directed relationship ``src -> dst`` observed at a source location."""

    CONTAINS = "contains"    # a module/class contains a nested symbol
    IMPORT = "import"
    CALL = "call"
    REFERENCE = "reference"  # an attribute read / name use that isn't a call
    INHERITS = "inherits"
    RECEIVES_ARG = "receives-arg"  # a call site was observed passing dst (a type) into src's param


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
