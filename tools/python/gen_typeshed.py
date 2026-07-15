"""Generator for the frozen typeshed type tables (Tier-2 stub resolution).

Reads a pinned typeshed (via ``typeshed_client``, a generation-time-only dependency) and
distills it into two frozen literals committed under
``src/claude_ast/ingest/python/_typeshed_table.py``:

- ``MODULES``: ``module -> {name: (kind, result_type)}`` where kind is
  ``value|func|class|submodule`` and result_type is a concrete type qualname, ``""`` (OPAQUE —
  member real but type not modeled, so a chain stops there), or a class qualname for a class.
- ``CLASSES``: ``class_qualname -> {member: (kind, result_type)}``, MRO-flattened.

The engine consults these as pure dict lookups at index time (never imports typeshed_client),
so warm==cold holds. Everything version/platform-dependent is resolved offline here and
INTERSECTED across the supported matrix, so no in-range project ever gets a false edge.

Conservatism is the invariant: a mis-read must degrade to OPAQUE/absent (a miss), never to a
confident wrong type (a false edge). Anything not understood -> OPAQUE. Usage::

    uv run python tools/python/gen_typeshed.py stats     # coverage report
    uv run python tools/python/gen_typeshed.py generate  # write the table
    uv run python tools/python/gen_typeshed.py check     # CI freshness gate

(``typeshed-client`` is a locked dev dependency — plain ``uv run`` uses the pinned version, so
the table regenerates identically everywhere; an unpinned ``--with typeshed-client`` overlay
could drift to a newer bundled typeshed.)
"""

from __future__ import annotations

import ast
import builtins
import hashlib
import sys
from pathlib import Path

import typeshed_client as tc

OPAQUE = ""  # member exists but result type not modeled -> emit its edge, stop chaining
SELF = "Self"  # a `Self`/typing.Self return -> the resolver substitutes the actual receiver type
_BUILTINS = frozenset(dir(builtins))
_SPECIAL_OPAQUE = frozenset({"Any", "object", "NoReturn", "Never", "None"})
_NONE_ISH = frozenset({"None", "MaybeNone"})
_NON_BASE = frozenset({"Generic", "Protocol", "object"})

_ROOT = Path(__file__).resolve().parents[2]
_OUT = _ROOT / "src" / "claude_ast" / "ingest" / "python" / "_typeshed_table.py"

SUPPORTED_VERSIONS: tuple[tuple[int, int], ...] = ((3, 12), (3, 13), (3, 14))
PLATFORM = "linux"  # platform-specific modules (winreg/fcntl/…) are a documented follow-up
GENERATOR_VERSION = 4  # bumped: case-exact stub lookup — no phantom modules on macOS/Windows


def spec_fingerprint() -> str:
    """Hash of everything that determines the table's shape (versions/platform/generator logic)."""
    payload = repr((sorted(SUPPORTED_VERSIONS), PLATFORM, GENERATOR_VERSION))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]

Table = dict[str, dict[str, tuple[str, str]]]


class Extractor:
    """Distills one (version, platform) of typeshed into MODULES + CLASSES tables."""

    def __init__(self, version: tuple[int, int], platform: str) -> None:
        self.ctx = tc.get_search_context(version=version, platform=platform)
        self.resolver = tc.Resolver(self.ctx)
        self.modules: Table = {}
        self.classes: Table = {}
        self._in_progress: set[str] = set()
        self._listings: dict[Path, frozenset[str]] = {}
        self.skipped = 0

    def _names(self, module: str) -> dict | None:
        try:
            names = tc.get_stub_names(module, search_context=self.ctx)
        except Exception:
            return None
        if names is None or not self._case_exact(module):
            return None
        return names

    def _case_exact(self, module: str) -> bool:
        """The stub's on-disk spelling matches ``module`` exactly.

        On a case-insensitive filesystem (a macOS/Windows dev machine) the finder happily opens
        ``email/message.pyi`` for the query ``email.Message`` — minting phantom modules that a
        case-sensitive CI then (correctly) fails to reproduce. The path the finder returns echoes
        the *queried* case, so the only trustworthy spelling is the directory listing.
        """
        try:
            path = tc.get_stub_file(module, search_context=self.ctx)
        except Exception:
            return False
        if path is None:
            return False
        parts = module.split(".")
        if path.name == "__init__.pyi":
            path = path.parent  # a package: its directory carries the leaf name
            expected = parts
        else:
            expected = [*parts[:-1], f"{parts[-1]}.pyi"]
        for name in reversed(expected):
            if name not in self._listing(path.parent):
                return False
            path = path.parent
        return True

    def _listing(self, directory: Path) -> frozenset[str]:
        cached = self._listings.get(directory)
        if cached is None:
            try:
                cached = frozenset(entry.name for entry in directory.iterdir())
            except OSError:
                cached = frozenset()
            self._listings[directory] = cached
        return cached

    # --- resolution -------------------------------------------------------------------
    def qualify(self, name: str, module: str) -> str | None:
        """Resolve a (possibly dotted) type name referenced in ``module`` to its qualname."""
        for candidate in (f"{module}.{name}", name):
            try:
                res = self.resolver.get_fully_qualified_name(candidate)
            except Exception:
                res = None
            if res is None:
                continue
            src = getattr(res, "source_module", None)  # ImportedInfo -> real origin
            if src is not None:
                return ".".join(src) + "." + name.rsplit(".", 1)[-1]
            return candidate if "." in candidate else f"{module}.{name}"
        if name.split(".")[0] in _BUILTINS:
            return name if "." in name else f"builtins.{name}"
        return None

    def _deref(self, info, module: str):
        """Follow ImportedInfo/ImportedName to a concrete (NameInfo, module) or ('MODULE', qual)."""
        if getattr(info, "source_module", None) is not None:  # ImportedInfo
            return self._deref(info.info, ".".join(info.source_module))
        node = getattr(info, "ast", None)
        if node is not None and node.__class__.__name__ == "ImportedName":
            src = ".".join(node.module_name)
            nm = node.name
            if nm is None:
                return ("MODULE", src)
            names = self._names(src)
            tgt = names.get(nm) if names else None
            if tgt is None:
                return ("MODULE", f"{src}.{nm}") if self._names(f"{src}.{nm}") else None
            return self._deref(tgt, src)
        return (info, module)

    # --- annotation -> concrete qualname or OPAQUE -----------------------------------
    def normalize(self, node: ast.expr | None, module: str, owner: str | None) -> str:
        if node is None or isinstance(node, ast.Constant):
            return OPAQUE
        if isinstance(node, ast.Name):
            if node.id == "Self":
                return SELF  # covariant: resolved to the receiver type at chain time, not `owner`
            if node.id in _SPECIAL_OPAQUE:
                return OPAQUE
            return self.qualify(node.id, module) or OPAQUE
        if isinstance(node, ast.Attribute):
            if node.attr == "Self":  # typing.Self / typing_extensions.Self / _typeshed.Self
                return SELF
            dotted = _dotted(node)
            return (self.qualify(dotted, module) or OPAQUE) if dotted else OPAQUE
        if isinstance(node, ast.Subscript):
            head = node.value
            if isinstance(head, ast.Name) and head.id == "Optional":
                return self.normalize(node.slice, module, owner)
            if isinstance(head, ast.Name) and head.id == "Union":
                return OPAQUE
            return self.normalize(head, module, owner)  # list[str] -> list (member access safe)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            parts = _union_parts(node)
            non_none = [p for p in parts if not _is_none(p)]
            return self.normalize(non_none[0], module, owner) if len(non_none) == 1 else OPAQUE
        return OPAQUE

    # --- classify one NameInfo into (kind, result_type) ------------------------------
    def classify(self, info, module: str, owner: str | None) -> tuple[str, str] | None:
        d = self._deref(info, module)
        if d is None:
            return None
        if d[0] == "MODULE":
            return ("submodule", d[1])
        info, module = d
        node = info.ast
        if node.__class__.__name__ == "OverloadedName":
            rets = {self.normalize(f.returns, module, owner)
                    for f in node.definitions if isinstance(f, ast.FunctionDef)}
            return ("method" if owner else "func", rets.pop() if len(rets) == 1 else OPAQUE)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            kind = "property" if _is_property(node) else ("method" if owner else "func")
            return (kind, self.normalize(node.returns, module, owner))
        if isinstance(node, ast.ClassDef):
            cls_q = f"{module}.{info.name}"
            self.ensure_class(cls_q, module, node)
            return ("class", cls_q)
        if isinstance(node, ast.AnnAssign):
            return ("value", self.normalize(node.annotation, module, owner))
        return None  # Assign aliases / submodules: the evaluator uses MODULES-membership

    # --- class extraction with MRO flatten -------------------------------------------
    def ensure_class(self, qual: str, module: str, node: ast.ClassDef | None) -> None:
        if qual in self.classes or qual in self._in_progress:
            return
        self._in_progress.add(qual)
        members: dict[str, tuple[str, str]] = {}
        if node is None:
            module, _, cname = qual.rpartition(".")
            info = (self._names(module) or {}).get(cname)
            cand = getattr(info, "ast", None) if info else None
            node = cand if isinstance(cand, ast.ClassDef) else None
        if node is not None:
            for base in node.bases:
                bq = self._base_qualname(base, module)
                if bq and bq != "builtins.object":
                    self.ensure_class(bq, bq.rpartition(".")[0], None)
                    members.update(self.classes.get(bq, {}))
            child = (self._names(module) or {}).get(node.name)
            for cname, cinfo in ((child.child_nodes if child else None) or {}).items():
                if cname.startswith("_"):
                    continue
                try:
                    r = self.classify(cinfo, module, qual)
                except Exception:
                    r = None
                    self.skipped += 1
                if r is not None:
                    members[cname] = r
        self.classes[qual] = members
        self._in_progress.discard(qual)

    def _base_qualname(self, base: ast.expr, module: str) -> str | None:
        head = base.value if isinstance(base, ast.Subscript) else base
        dotted = _dotted(head) if isinstance(head, ast.Name | ast.Attribute) else None
        if dotted is None or dotted.split(".")[-1] in _NON_BASE:
            return None
        return self.qualify(dotted, module)

    # --- module extraction -----------------------------------------------------------
    def extract_module(self, module: str) -> None:
        names = self._names(module)
        if names is None:
            return
        table: dict[str, tuple[str, str]] = {}
        for name, info in names.items():
            if name.startswith("_"):
                continue
            try:
                r = self.classify(info, module, None)
            except Exception:
                r = None
                self.skipped += 1
            if r is not None:
                table[name] = r
        self.modules[module] = table


def _is_property(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """A ``@property`` / ``@cached_property`` accessor — a value, not a callable member, so a call
    on it (``p.name()``) must decline while a chain *access* of it threads its return type."""
    for deco in node.decorator_list:
        leaf = deco.attr if isinstance(deco, ast.Attribute) else getattr(deco, "id", None)
        if leaf in ("property", "cached_property"):
            return True
    return False


def _dotted(node: ast.expr) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _union_parts(node: ast.expr) -> list[ast.expr]:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _union_parts(node.left) + _union_parts(node.right)
    return [node]


def _is_none(node: ast.expr) -> bool:
    return (isinstance(node, ast.Name) and node.id in _NONE_ISH) or (
        isinstance(node, ast.Constant) and node.value is None
    )


def enumerate_stdlib(ctx) -> list[str]:
    """Every stdlib stub module: the canonical top-level set + BFS submodule discovery."""
    seen: set[str] = set()
    queue = sorted(n for n in sys.stdlib_module_names if not n.startswith("_"))
    while queue:
        m = queue.pop()
        if m in seen:
            continue
        try:
            names = tc.get_stub_names(m, search_context=ctx)
        except Exception:
            names = None
        if names is None:
            continue
        seen.add(m)
        for name in names:
            if not name.startswith("_") and f"{m}.{name}" not in seen:
                try:
                    if tc.get_stub_names(f"{m}.{name}", search_context=ctx) is not None:
                        queue.append(f"{m}.{name}")
                except Exception:
                    pass
    return sorted(seen)


def _extract_one(version: tuple[int, int], platform: str) -> Extractor:
    ex = Extractor(version, platform)
    for module in enumerate_stdlib(ex.ctx):
        ex.extract_module(module)
    return ex


def _intersect(tables: list[Table]) -> Table:
    """Keep a member only if present in EVERY table; if its (kind, type) disagrees across
    versions, keep the kind but drop the type to OPAQUE; if even the kind disagrees, drop it.
    Version-skew-safe: nothing survives that could be a false edge on some in-range version."""
    out: Table = {}
    for key in set.intersection(*(set(t) for t in tables)):
        maps = [t[key] for t in tables]
        merged: dict[str, tuple[str, str]] = {}
        for name in set.intersection(*(set(m) for m in maps)):
            vals = {m[name] for m in maps}
            if len(vals) == 1:
                merged[name] = next(iter(vals))
            elif len({v[0] for v in vals}) == 1:  # same kind, differing type -> OPAQUE
                merged[name] = (next(iter(vals))[0], OPAQUE)
            # else: kind disagrees -> drop entirely
        out[key] = merged
    return out


def _generate_tables() -> tuple[Table, Table]:
    extractors = [_extract_one(v, PLATFORM) for v in SUPPORTED_VERSIONS]
    modules = _intersect([e.modules for e in extractors])
    classes = _intersect([e.classes for e in extractors])
    _assert_no_case_collisions(modules)
    return modules, classes


def _assert_no_case_collisions(modules: Table) -> None:
    """Two MODULE keys differing only by case are a case-insensitive-filesystem artifact (the
    macOS trap ``_case_exact`` closes) — fail loudly on every platform rather than commit one.
    Modules only: they come from filesystem lookups, whereas class names come from stub ASTs,
    where a case pair is legitimate Python (``ast.excepthandler`` vs ``ast.ExceptHandler``)."""
    seen: dict[str, str] = {}
    for key in modules:
        low = key.lower()
        if low in seen:
            raise SystemExit(f"case-colliding module keys: {seen[low]!r} vs {key!r}")
        seen[low] = key


def _render(modules: Table, classes: Table) -> str:
    def block(name: str, table: Table) -> list[str]:
        lines = [f"{name}: dict[str, dict[str, tuple[str, str]]] = {{"]
        for key in sorted(table):
            members = ", ".join(f"{m!r}: {table[key][m]!r}" for m in sorted(table[key]))
            lines.append(f"    {key!r}: {{{members}}},")
        lines.append("}")
        return lines

    header = [
        "# GENERATED by tools/python/gen_typeshed.py — DO NOT EDIT. Regenerate with that command.",
        "# typeshed-derived type tables, intersected over "
        f"{[f'{a}.{b}' for a, b in SUPPORTED_VERSIONS]} on {PLATFORM!r}.",
        "# kind in {value, func, class, submodule}; result_type is a qualname, '' = OPAQUE, or a",
        "# class qualname. Consulted at index time as a pure hermetic lookup.",
        "",
    ]
    return "\n".join(
        header + block("MODULES", modules) + [""] + block("CLASSES", classes)
        + ["", f"FINGERPRINT = {spec_fingerprint()!r}", ""]
    )


def _generate() -> None:
    modules, classes = _generate_tables()
    _OUT.write_text(_render(modules, classes))
    print(f"wrote {_OUT.relative_to(_ROOT)}: {len(modules)} modules, {len(classes)} classes",
          file=sys.stderr)


def _check() -> None:
    modules, classes = _generate_tables()
    current = _OUT.read_text() if _OUT.exists() else ""
    if _render(modules, classes) != current:
        raise SystemExit(
            "typeshed table is stale — run `uv run python tools/python/gen_typeshed.py generate`"
        )
    print("typeshed table is up to date", file=sys.stderr)


def _stats() -> None:
    ex = _extract_one((3, 13), "linux")
    mod_members = sum(len(v) for v in ex.modules.values())
    cls_members = sum(len(v) for v in ex.classes.values())
    print(f"modules: {len(ex.modules)}  ({mod_members} members)")
    print(f"classes: {len(ex.classes)}  ({cls_members} members)")
    print(f"skipped (per-name errors): {ex.skipped}")
    print("\ncanonical checks:")
    print("  sys.stdout          =", ex.modules.get("sys", {}).get("stdout"))
    print("  os.path.join        =", ex.modules.get("os.path", {}).get("join"))
    print("  Path.cwd            =", ex.classes.get("pathlib.Path", {}).get("cwd"))
    print("  Path.exists         =", ex.classes.get("pathlib.Path", {}).get("exists"))
    tio = ex.classes.get("typing.TextIO", {})
    print("  TextIO has getvalue =", "getvalue" in tio)
    biggest = sorted(ex.classes.items(), key=lambda kv: -len(kv[1]))[:5]
    print("\nbiggest classes:", [(k, len(v)) for k, v in biggest])


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "stats"
    if command == "stats":
        _stats()
    elif command == "generate":
        _generate()
    elif command == "check":
        _check()
    else:
        raise SystemExit(__doc__)
