"""Attribute-read reference cases for the golden eval — the REFERENCE edge kind.

A bare attribute read (``obj.attr`` with no call) resolves through the *same* receiver ladder as a
method call, but emits a REFERENCE edge and can land on a **data** attribute, not just a callable.
Tiers mirror the call ladder: a self / annotated receiver is possible; an untyped receiver
name-matches at LOW; a name-rooted read of an in-tree module member binds definite.
"""

from . import core
from .core import Base


class Widget(Base):
    """Reads attributes through the ladder; ``label`` is a data attribute (a class VARIABLE)."""

    label = "widget"

    def title(self) -> str:
        """Self-attribute read -> the class's own data attribute (inference, possible)."""
        return self.label


def describe(w: Widget) -> str:
    """Annotated receiver read: ``w: Widget`` -> Widget.label at the possible tier (annotation)."""
    return w.label


def ceiling() -> int:
    """Name-rooted read of an in-tree module variable -> a definite REFERENCE to the variable."""
    return core.BASE_LIMIT


def sniff(obj) -> str:
    """Untyped receiver read -> a LOW name-match to every ``*.label`` (heuristic)."""
    return obj.label


class Hub:
    """Threads a typed-attribute chain: ``self.widget`` is a ``Widget``, so ``.label`` resolves."""

    widget: Widget  # a typed data attribute -> Widget

    def reach(self) -> str:
        """Multi-member read: ``self.widget`` (Widget) . ``label`` -> Widget.label (possible)."""
        return self.widget.label
