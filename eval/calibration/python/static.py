"""Python static decidable audit — the honesty check the runtime trace can't reach.

The runtime oracle can only judge edges whose site the driver executed. This oracle covers
*every* edge whose correctness is statically decidable, verified **independently of the tool's
own resolver** via Python's own machinery — so it catches a false ``definite`` even in code the
tests never run:

- **import** (in-tree module target) — a ``.py`` / ``__init__.py`` must exist for that qualname.
- **inherits** — checked against the *runtime MRO*: import both classes, assert the base is an
  ancestor. Fully independent of how the resolver bound the base name.
- **builtin** — the leaf must be a real ``builtins`` member (or attribute of a builtin type).
- **external** — the target must actually import (top module + attribute chain).
- **existence** — an in-tree call/reference/receives-arg target must be a real symbol (a dangling
  id is a resolver bug); a weak bar, but the decidable one for a dispatch-free fact.
"""

from __future__ import annotations

import builtins
import importlib
from pathlib import Path

from claude_ast.model import Graph

from ..edges import EdgeRecord
from ..verdicts import StaticOracle, StaticVerdict
from .ids import resolve_object

_BUILTIN_NAMES = frozenset(dir(builtins))


class PythonStaticOracle(StaticOracle):
    """The Python answer to :class:`StaticOracle`, bound to the graph + source root it audits."""

    def __init__(self, graph: Graph, modules: frozenset[str], src_root: Path) -> None:
        self._graph = graph
        self._modules = modules
        self._src_root = src_root

    def audit(self, rec: EdgeRecord) -> tuple[StaticVerdict, str]:
        if rec.kind == "import" and not rec.external:
            return (self._module_file_exists(rec.dst), "import")
        if rec.kind == "inherits":
            return (self._mro_holds(rec.src, rec.dst), "mro")
        if rec.external:
            if rec.dst.startswith("builtins."):
                return (_builtin_exists(rec.dst), "builtin")
            return (_importable(rec.dst), "external")
        if rec.kind in ("call", "reference", "receives-arg"):
            confirmed = self._graph.symbol(rec.dst) is not None
            return (StaticVerdict.CONFIRMED if confirmed else StaticVerdict.REFUTED, "existence")
        return (StaticVerdict.SKIPPED, "n/a")

    def _module_file_exists(self, module_id: str) -> StaticVerdict:
        rel = self._src_root / module_id.replace(".", "/")
        exists = rel.with_suffix(".py").exists() or (rel / "__init__.py").exists()
        return StaticVerdict.CONFIRMED if exists else StaticVerdict.REFUTED

    def _mro_holds(self, sub_id: str, base_id: str) -> StaticVerdict:
        sub = self._load_class(sub_id)
        base = self._load_class(base_id)
        if sub is None or base is None:
            return StaticVerdict.SKIPPED
        return StaticVerdict.CONFIRMED if base in sub.__mro__ else StaticVerdict.REFUTED

    def _load_class(self, class_id: str) -> type | None:
        """The class object a symbol id names (via the shared resolver), or ``None`` if it isn't
        an importable class."""
        obj = resolve_object(class_id, self._modules)
        return obj if isinstance(obj, type) else None


def _builtin_exists(dst: str) -> StaticVerdict:
    rest = dst[len("builtins.") :]
    head, _, tail = rest.partition(".")
    if head not in _BUILTIN_NAMES:
        return StaticVerdict.REFUTED
    if not tail:
        return StaticVerdict.CONFIRMED
    has_attr = hasattr(getattr(builtins, head), tail)
    return StaticVerdict.CONFIRMED if has_attr else StaticVerdict.REFUTED


def _importable(dst: str) -> StaticVerdict:
    """Resolve a dotted external target by importing the longest module prefix, then getattr.

    ``os.path.join`` -> import ``os`` (then ``os.path``), getattr ``join``. Import failure is
    ``SKIPPED`` (an environment gap, not a refutation); a resolvable module with a missing
    attribute *is* a refutation.
    """
    parts = dst.split(".")
    mod = None
    depth = 0
    for i in range(len(parts), 0, -1):
        try:
            mod = importlib.import_module(".".join(parts[:i]))
            depth = i
            break
        except Exception:
            continue
    if mod is None:
        return StaticVerdict.SKIPPED
    obj: object = mod
    for attr in parts[depth:]:
        obj = getattr(obj, attr, None)
        if obj is None:
            return StaticVerdict.REFUTED
    return StaticVerdict.CONFIRMED
