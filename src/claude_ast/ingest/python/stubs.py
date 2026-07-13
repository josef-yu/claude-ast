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

# The generated frozen table + the fingerprint of the spec it was built from. Guarded so
# the generator (which imports this module for the spec) works before the table exists.
try:
    from ._stub_table import FINGERPRINT as TABLE_FINGERPRINT
    from ._stub_table import TABLE
except ImportError:  # not yet generated (bootstrap)
    TABLE: dict[str, frozenset[str]] = {}
    TABLE_FINGERPRINT = ""


class StubProvider(Protocol):
    """Answers "does external type ``type_qualname`` have a callable member ``attr``?".

    Knowledge only — the caller forms the external member id and mints the node, so a
    stub-resolved ``pathlib.Path.exists`` is the SAME external node as a directly-imported
    one. ``False`` DECLINES (member absent or type out of scope); it never guesses.
    """

    def member(self, type_qualname: str, attr: str) -> bool: ...


class StdlibStubs:
    """The default hermetic provider: a pure lookup over the frozen stdlib table."""

    def member(self, type_qualname: str, attr: str) -> bool:
        return attr in TABLE.get(type_qualname, frozenset())


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
