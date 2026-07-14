"""Persist and merge runtime observation maps — dispatch coverage accumulated across runs.

One traced process can only exercise so much (``sys.setprofile`` makes a big suite slow, and a
crash forfeits the whole trace), so the honest way to widen the runtime denominator is several
*separate* runs — one driver each — scored against the **union** of their observations:
``--trace-out`` persists a run's map, ``--trace-in`` folds prior maps into the next scoring.
Neutral like the rest of the vocabulary: an ``ObservedMap`` is language-agnostic; only the ids
inside it are backend-shaped.

A map is keyed by ``(realpath, line)`` sites, so it is valid only for the exact checkout it was
traced on — an edit shifts lines and mis-joins silently. Accumulate within one code state.
"""

from __future__ import annotations

import json
from pathlib import Path

from .verdicts import ObservedMap


def save_trace(observed: ObservedMap, path: Path) -> None:
    """Write ``observed`` as JSON: ``{file: {line: [callee, …]}}``, sorted for stable diffs."""
    data: dict[str, dict[str, list[str]]] = {}
    for (file, line), callees in observed.items():
        data.setdefault(file, {})[str(line)] = sorted(callees)
    path.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")


def load_trace(path: Path) -> ObservedMap:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {
        (file, int(line)): set(callees)
        for file, lines in raw.items()
        for line, callees in lines.items()
    }


def merge_trace(into: ObservedMap, other: ObservedMap) -> None:
    """Union ``other``'s observations into ``into`` (sites merge; callee sets union)."""
    for site, callees in other.items():
        into.setdefault(site, set()).update(callees)
