"""Stub resolution — member calls on external (out-of-tree) types.

The value resolvers decline the moment a receiver's type is external: ``p: Path;
p.exists()`` yields no edge because ``Path`` is not an in-tree class. This module lets
those resolve — ``Path.exists`` is a knowable member — behind a ``StubProvider`` seam.

Only a hermetic **stdlib** provider ships today. It consults a FROZEN member table
(``_stub_table.py``, generated offline by ``tools/python/gen_stubs.py`` — never introspected at
index time), so the index stays deterministic and warm==cold holds. The Protocol is
deliberately shaped ``member(type_qualname, attr) -> bool`` (knowledge only — the backend
mints the external node, so a stub-resolved ``pathlib.Path.exists`` is the SAME node as a
directly-imported one). That lets a future environment-aware provider (PEP 561 /
``django-stubs``) slot in behind the same seam without touching the resolver: a composite
tries stdlib, then the environment, returning ``False`` to defer. No cache fingerprint is
needed — resolution runs at graph assembly and edges are never persisted, so an environment
change self-corrects on the next build.
"""

from __future__ import annotations

import hashlib
from typing import Protocol

# The generated frozen tables + the fingerprint of the spec each was built from. Guarded so
# the generators (which import this module for the spec) work before the tables exist.
try:
    from ._stub_table import FINGERPRINT as TABLE_FINGERPRINT
    from ._stub_table import TABLE
except ImportError:  # not yet generated (bootstrap)
    TABLE: dict[str, frozenset[str]] = {}
    TABLE_FINGERPRINT = ""

try:
    from ._typeshed_table import CLASSES as _TS_CLASSES
    from ._typeshed_table import MODULES as _TS_MODULES
except ImportError:  # not yet generated (bootstrap)
    _TS_MODULES: dict[str, dict[str, tuple[str, str]]] = {}
    _TS_CLASSES: dict[str, dict[str, tuple[str, str]]] = {}


class StubProvider(Protocol):
    """Typeshed knowledge for external (out-of-tree) types, behind a seam so a future
    environment-aware provider (PEP 561 / ``django-stubs``) composes in without touching the
    resolvers. Every method returns ``None``/``False`` to DECLINE (out of scope) — never a guess.

    ``member`` answers member-existence (the value-receiver path). ``has_module`` /
    ``module_member`` / ``type_member`` are the shape+type lookups the chain evaluator threads:
    a ``(kind, result_type)`` pair where kind is ``value|func|class|submodule|method`` and
    result_type is a qualname, ``""`` (OPAQUE), or a class qualname.
    """

    def member(self, type_qualname: str, attr: str) -> bool: ...
    def has_module(self, qualname: str) -> bool: ...
    def module_member(self, module: str, name: str) -> tuple[str, str] | None: ...
    def type_member(self, type_qualname: str, attr: str) -> tuple[str, str] | None: ...


class StdlibStubs:
    """The default hermetic provider: pure lookups over the frozen stdlib tables."""

    def member(self, type_qualname: str, attr: str) -> bool:
        return attr in TABLE.get(type_qualname, frozenset())

    def has_module(self, qualname: str) -> bool:
        return qualname in _TS_MODULES

    def module_member(self, module: str, name: str) -> tuple[str, str] | None:
        return _TS_MODULES.get(module, {}).get(name)

    def type_member(self, type_qualname: str, attr: str) -> tuple[str, str] | None:
        return _TS_CLASSES.get(type_qualname, {}).get(attr)


STDLIB_STUBS: StubProvider = StdlibStubs()


# --- generator spec: the single source of truth for tools/python/gen_stubs.py AND the freshness
# tests (tests/backends/python/test_stubs.py). Editing these without regenerating the table
# is exactly what `test_stub_table_matches_its_spec_fingerprint` catches. ---

# (module, type) pairs to introspect. ``dir()`` flattens the MRO, so inherited members
# (``PurePath``'s on ``Path``, ``date``'s on ``datetime``) are captured without listing bases.
ALLOWLIST: tuple[tuple[str, str], ...] = (
    ("builtins", "str"),
    ("builtins", "bytes"),
    ("builtins", "bytearray"),
    ("builtins", "list"),
    ("builtins", "dict"),
    ("builtins", "set"),
    ("builtins", "frozenset"),
    ("builtins", "tuple"),
    ("pathlib", "Path"),
    ("pathlib", "PurePath"),
    ("datetime", "datetime"),
    ("datetime", "date"),
    ("datetime", "time"),
    ("datetime", "timedelta"),
    ("collections", "deque"),
    ("collections", "defaultdict"),
    ("collections", "OrderedDict"),
    ("collections", "Counter"),
    ("decimal", "Decimal"),
    ("re", "Pattern"),
    ("re", "Match"),
    ("uuid", "UUID"),
    ("io", "StringIO"),
    ("io", "BytesIO"),
)

# The Python minor versions the frozen table is the INTERSECTION over — every member in the
# table exists on all of them, so no version-skew false edge for a project anywhere in range.
SUPPORTED_VERSIONS: tuple[str, ...] = ("3.12", "3.13", "3.14")

# Bump when the generator's FILTERING LOGIC changes (callable-only, dunder-drop, ...), so the
# freshness test flags a table built by the old logic even when the spec above is unchanged.
GENERATOR_VERSION = 1


def spec_fingerprint() -> str:
    """A hash of everything that determines the table's shape.

    The generator stamps it into the emitted table; the freshness test recomputes it and
    compares. A mismatch means the spec (allowlist / versions / generator logic) changed
    without regenerating — i.e. run ``uv run python tools/python/gen_stubs.py``.
    """
    payload = repr((sorted(ALLOWLIST), sorted(SUPPORTED_VERSIONS), GENERATOR_VERSION))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]
