"""The neutral calibration vocabulary + the oracle seam.

Language-agnostic, exactly like ``model`` / ``query`` in the engine: the verdict
enums and the ``RuntimeOracle`` / ``StaticOracle`` protocols name *what* a calibration
oracle answers, never *how* a given language answers it. A backend (``python/``) supplies
the how — a Python oracle uses ``sys.setprofile`` + ``importlib``; a future JS/TS oracle
would use its own tracer and module system, behind the same two protocols.

No backend registry until a real second language lands (the engine's rule): ``run.py``
constructs the Python oracles directly, the way the CLI wires ``default_indexers()``.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from typing import Protocol

from .edges import EdgeRecord

# A site (realpath, line) -> the callee ids observed executing there. The ids are in the
# backend's own symbol-id format, so they compare directly against an edge's ``dst``.
ObservedMap = dict[tuple[str, int], set[str]]


class Verdict(StrEnum):
    """How an edge's claimed target compares to what actually dispatched at its site."""

    UNEXERCISED = "unexercised"    # the site never ran — no evidence either way
    EXACT = "exact"                # the exact claimed target ran
    CONSTRUCTION = "construction"  # target is a class; its constructor ran (Foo() -> Foo.__init__)
    OVERRIDE = "override"          # a same-named member on a related class ran (override dispatch)
    PROTOCOL = "protocol"          # a same-named member ran on a class that structurally
    #                                implements the target's protocol/interface — dispatch that
    #                                nominal (INHERITS) kinship can't see; same story as OVERRIDE
    SAME_NAME = "same-name"        # a same-named member on an *unrelated* class ran (weak)
    UNTRACEABLE = "untraceable"    # the site ran but the target's kind is one the tracer can't see
    CONTRADICTED = "contradicted"  # the site ran with callees, none matching the target or its name


class StaticVerdict(StrEnum):
    """An independent, dispatch-free check of a decidable edge."""

    CONFIRMED = "confirmed"
    REFUTED = "refuted"
    SKIPPED = "skipped"  # not statically decidable here / couldn't resolve in this env


class RuntimeOracle(Protocol):
    """Ground truth from actually running the code: what dispatched at each call site."""

    def trace(self, driver: Callable[[], None], root: str) -> ObservedMap:
        """Run ``driver`` under a tracer; return each in-``root`` site's observed callees."""
        ...

    def judge(self, rec: EdgeRecord, observed: ObservedMap) -> Verdict:
        """Classify one CALL edge against the trace."""
        ...


class StaticOracle(Protocol):
    """An independent, dispatch-free audit of an edge — covers even unexercised code."""

    def audit(self, rec: EdgeRecord) -> tuple[StaticVerdict, str]:
        """Verify one decidable edge; return ``(verdict, method-label)``."""
        ...
