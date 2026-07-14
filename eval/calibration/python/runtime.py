"""Python runtime dispatch oracle — ground truth from the interpreter itself.

The calibration claim is about *dispatch*: when claude-ast labels a call edge ``definite``,
does the call really go there? A static analyzer only has an opinion; the running interpreter
has the fact. So we execute a driver under ``sys.setprofile``, record every observed
``(caller_file, caller_line) -> {callee_id}`` at each call boundary, and judge each CALL edge
against what actually ran at its site.

Sound but partial: an observed dispatch is real (no false confirmations), but a site the driver
never runs is simply *unexercised* — never counted against precision, only against coverage.
Same honesty discipline as the v2/v3 evals.

The join hinges on the Python id shape (see ``ids``): a callee's ``__module__ + '.' +
co_qualname`` is exactly the tool's symbol id. Three Python-specific normalizations keep the
buckets honest, each found in the design spike:

- **C-level callees** — ``setprofile`` fires ``call`` only for *Python* callees; builtins and C
  extensions fire ``c_call`` (with the callable as ``arg``). Without handling it, every
  ``print()`` / ``str()`` edge looks wrongly contradicted.
- **construction** — calling a class dispatches to ``__init__`` (a dataclass's is a generated
  ``__create_fn__.<locals>.__init__``), so a definite ``Foo()`` edge to the *class* is confirmed
  by a constructor at its site, not contradicted.
- **override** — a ``possible`` edge names the statically-known member; runtime may dispatch to a
  sub/superclass override. That is the "possible" disclaimer materializing, bucketed apart from an
  exact hit and from a true contradiction.
"""

from __future__ import annotations

import builtins
import os
import sys
from collections import defaultdict
from collections.abc import Callable
from types import FrameType
from typing import Any

from claude_ast.model import Graph

from ..edges import EdgeRecord, ancestors, related_classes
from ..verdicts import ObservedMap, RuntimeOracle, Verdict
from .ids import class_of, leaf, module_of, resolve_object, strip_disambiguator

_CTOR = ("__init__", "__new__")


class PythonRuntimeOracle(RuntimeOracle):
    """The Python answer to :class:`RuntimeOracle`, bound to the graph whose edges it judges."""

    def __init__(self, graph: Graph, modules: frozenset[str]) -> None:
        self._graph = graph
        self._modules = modules
        self._forms: dict[str, str | None] = {}  # dst -> its runtime code-identity, cached

    def trace(self, driver: Callable[[], None], root: str) -> ObservedMap:
        """Run ``driver`` under ``setprofile``; return each in-``root`` site's observed callees.

        Only sites whose *caller* file is under ``root`` are kept — the only ones an edge can
        point from — so the map stays bounded by the subject's own call sites no matter how much
        library code the driver touches.
        """
        root = os.path.realpath(root)
        observed: ObservedMap = defaultdict(set)

        def profiler(frame: FrameType, event: str, arg: Any) -> None:
            # `call`: ``frame`` is the callee's frame, caller is f_back. `c_call` (builtins, C
            # extensions): ``frame`` is the *caller's* frame and ``arg`` is the C callable —
            # setprofile fires no `call` for these.
            if event == "call":
                back = frame.f_back
                if back is None or not back.f_code.co_filename.startswith(root):
                    return
                callee = f"{frame.f_globals.get('__name__', '')}.{frame.f_code.co_qualname}"
                site = (os.path.realpath(back.f_code.co_filename), back.f_lineno)
            elif event == "c_call":
                if not frame.f_code.co_filename.startswith(root):
                    return
                qual = getattr(arg, "__qualname__", None) or getattr(arg, "__name__", None)
                if qual is None:
                    return
                callee = f"{getattr(arg, '__module__', None) or 'builtins'}.{qual}"
                site = (os.path.realpath(frame.f_code.co_filename), frame.f_lineno)
            else:
                return
            observed[site].add(callee)

        prior = sys.getprofile()
        sys.setprofile(profiler)
        try:
            driver()
        except Exception as exc:  # noqa: BLE001 — the trace is best-effort; keep what was collected
            print(f"[calibration] driver raised {type(exc).__name__}: {exc} — "
                  f"scoring the partial trace", file=sys.stderr)
        finally:
            sys.setprofile(prior)
        return observed

    def judge(self, rec: EdgeRecord, observed: ObservedMap) -> Verdict:
        """Classify one CALL edge against the trace. Callers pass only ``kind == 'call'`` edges."""
        if rec.file is None or rec.line is None:
            return Verdict.UNEXERCISED
        seen = observed.get((rec.file, rec.line))
        if not seen:
            return Verdict.UNEXERCISED

        obs = {strip_disambiguator(o) for o in seen}
        dst = strip_disambiguator(rec.dst)
        if dst in obs or self._observed_form(dst) in obs:
            return Verdict.EXACT

        dst_leaf = leaf(dst)
        constructs = rec.dst_kind == "class" or (rec.external and dst_leaf[:1].isupper())
        if constructs and self._is_construction(dst, dst_leaf, rec, obs):
            return Verdict.CONSTRUCTION

        same_leaf = [o for o in obs if leaf(o) == dst_leaf]
        if same_leaf:
            is_method = rec.dst_kind == "method"
            related = related_classes(self._graph, class_of(dst)) if is_method else frozenset()
            if any(class_of(o) in related for o in same_leaf):
                return Verdict.OVERRIDE
            if is_method and self._implements_protocol(class_of(dst), same_leaf):
                return Verdict.PROTOCOL
            return Verdict.SAME_NAME
        if self._is_untraceable(rec, dst):
            return Verdict.UNTRACEABLE
        return Verdict.CONTRADICTED

    def _observed_form(self, dst: str) -> str | None:
        """The trace-form id of ``dst``'s runtime object — ``__globals__['__name__'] + '.' +
        __code__.co_qualname`` — so a call to a factory/wrapper-produced or aliased Python callable
        still matches. The runtime callee's *code* qualname is its definition site (Django's
        ``gettext_lazy`` is a ``lazy(...)`` product, so its code name is
        ``lazy.<locals>.__wrapper__``, which no ``functools.wraps`` can change), and that is what
        the tracer records. Reconstructing the same string from the resolved object makes the match
        object identity, not a name guess. Cached; ``None`` for a C callee / class / unresolvable
        id (handled elsewhere)."""
        if dst not in self._forms:
            obj = resolve_object(dst, self._modules)
            code = getattr(obj, "__code__", None)
            glob = getattr(obj, "__globals__", None)
            self._forms[dst] = (
                f"{glob.get('__name__', '')}.{code.co_qualname}"
                if code is not None and glob is not None
                else None
            )
        return self._forms[dst]

    def _implements_protocol(self, proto_id: str, observed: list[str]) -> bool:
        """The target's class is a ``typing.Protocol`` and an observed same-leaf callee's class
        structurally implements it. Structural typing leaves no INHERITS edge, so protocol
        dispatch (``stubs: StubProvider`` running ``StdlibStubs.type_member``) is invisible to
        ``related_classes`` — this is the structural counterpart of the OVERRIDE bucket, checked
        against the runtime objects (``__protocol_attrs__``), never by name."""
        proto = resolve_object(proto_id, self._modules)
        if not (isinstance(proto, type) and getattr(proto, "_is_protocol", False)):
            return False
        attrs = getattr(proto, "__protocol_attrs__", None)
        if not attrs:
            return False
        for o in observed:
            impl = resolve_object(class_of(o), self._modules)
            if isinstance(impl, type) and all(hasattr(impl, a) for a in attrs):
                return True
        return False

    def _is_untraceable(self, rec: EdgeRecord, dst: str) -> bool:
        """Targets whose call CPython's ``setprofile`` structurally cannot report, so a
        non-hit is *no evidence*, not counter-evidence (see the module docstring's spike):

        - a **builtin type** construction (``str()`` / ``tuple()``) emits no event at all —
          the tracer fires ``c_call`` for builtin *functions*, never for calling a *type*;
        - an **Enum** member call (``Confidence(x)``) dispatches through ``EnumType.__call__``
          and returns a cached member, so the member class's constructor never runs.
        """
        if rec.external and dst.startswith("builtins.") and "." not in dst[len("builtins.") :]:
            return isinstance(getattr(builtins, dst[len("builtins.") :], None), type)
        if rec.dst_kind == "class":
            return any(a.startswith("enum.") for a in ancestors(self._graph, dst))
        return False

    def _is_construction(self, dst: str, dst_leaf: str, rec: EdgeRecord, obs: set[str]) -> bool:
        """A construction ran at the site: a constructor on ``dst`` (or an in-tree ancestor,
        or — matched by class leaf — an external class), or a dataclass's generated init."""
        owners = ancestors(self._graph, dst) if rec.dst_kind == "class" else {dst}
        dst_mod = module_of(dst, self._modules)
        for o in obs:
            parts = o.split(".")
            owner_leaf = parts[-2] if len(parts) >= 2 else ""
            if parts[-1] in _CTOR and (class_of(o) in owners or owner_leaf == dst_leaf):
                return True
            if "__create_fn__" in o and dst_mod is not None and o.startswith(dst_mod + "."):
                return True
        return False
