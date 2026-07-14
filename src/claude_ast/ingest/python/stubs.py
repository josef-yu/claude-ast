"""Stub resolution — module shape and member/return types for external (out-of-tree) types.

The value resolvers and the chain evaluator decline the moment a receiver's type is external:
``p: Path; p.exists()`` or ``sys.stdout.getvalue()`` can't resolve because the type is not
in-tree. This module supplies that knowledge — from typeshed — behind a ``StubProvider`` seam.

Only a hermetic **stdlib** provider ships today. It consults the FROZEN tables in
``_typeshed_table.py`` (generated offline by ``tools/python/gen_typeshed.py`` — never introspected
at index time), so the index stays deterministic and warm==cold holds. The seam is knowledge-only
— the backend forms the external id and mints the node, so a stub-resolved ``pathlib.Path.exists``
is the SAME node as a directly-imported one. That lets a future environment-aware provider
(PEP 561 / ``django-stubs``) slot in behind the same seam, returning ``None`` to defer. No cache
fingerprint is needed — resolution runs at graph assembly and edges are never persisted, so an
environment change self-corrects on the next build.
"""

from __future__ import annotations

from typing import Protocol

try:
    from ._typeshed_table import CLASSES as _TS_CLASSES
    from ._typeshed_table import MODULES as _TS_MODULES
except ImportError:  # not yet generated (bootstrap)
    _TS_MODULES: dict[str, dict[str, tuple[str, str]]] = {}
    _TS_CLASSES: dict[str, dict[str, tuple[str, str]]] = {}


class StubProvider(Protocol):
    """Typeshed knowledge for external (out-of-tree) types, behind a seam so a future
    environment-aware provider (PEP 561 / ``django-stubs``) composes in without touching the
    resolvers. Every method returns ``None`` to DECLINE (out of scope) — never a guess.

    ``has_module`` / ``module_member`` / ``type_member`` are the shape+type lookups the resolvers
    thread: a ``(kind, result_type)`` pair where kind is ``value|func|class|submodule|method`` and
    result_type is a qualname, ``""`` (OPAQUE), the ``Self`` sentinel, or a class qualname.
    """

    def has_module(self, qualname: str) -> bool: ...
    def module_member(self, module: str, name: str) -> tuple[str, str] | None: ...
    def type_member(self, type_qualname: str, attr: str) -> tuple[str, str] | None: ...


class StdlibStubs:
    """The default hermetic provider: pure lookups over the frozen typeshed tables."""

    def has_module(self, qualname: str) -> bool:
        return qualname in _TS_MODULES

    def module_member(self, module: str, name: str) -> tuple[str, str] | None:
        return _TS_MODULES.get(module, {}).get(name)

    def type_member(self, type_qualname: str, attr: str) -> tuple[str, str] | None:
        return _TS_CLASSES.get(type_qualname, {}).get(attr)


STDLIB_STUBS: StubProvider = StdlibStubs()
