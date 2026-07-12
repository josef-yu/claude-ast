"""FileIndex <-> JSON. The store persists parse products (symbols + refs), not the
resolved graph — edges are cheap to rebind in memory on load. Keys are terse to
keep the per-file blob small at scale.
"""

from __future__ import annotations

import json

from ..ingest.product import FileIndex, RawRef
from ..model import EdgeKind, Span, Symbol, SymbolKind


def to_json(fi: FileIndex) -> str:
    return json.dumps(
        {
            "p": fi.path,
            "m": fi.module,
            "s": [_symbol_d(s) for s in fi.symbols],
            "r": [_ref_d(r) for r in fi.refs],
            "i": fi.imports,
        },
        separators=(",", ":"),
    )


def from_json(text: str) -> FileIndex:
    d = json.loads(text)
    return FileIndex(
        path=d["p"],
        module=d["m"],
        symbols=[_symbol(x) for x in d["s"]],
        refs=[_ref(x) for x in d["r"]],
        imports=d["i"],
    )


def _span_d(s: Span) -> list[object]:
    return [s.file, s.line, s.col, s.end_line, s.end_col]


def _span(v: list) -> Span:
    return Span(v[0], v[1], v[2], v[3], v[4])


def _symbol_d(s: Symbol) -> dict[str, object]:
    return {
        "i": s.id,
        "n": s.name,
        "k": s.kind.value,
        "s": _span_d(s.span),
        "g": s.signature,
        "d": s.doc,
        "pa": s.parent,
    }


def _symbol(v: dict) -> Symbol:
    return Symbol(
        id=v["i"],
        name=v["n"],
        kind=SymbolKind(v["k"]),
        span=_span(v["s"]),
        signature=v["g"],
        doc=v["d"],
        parent=v["pa"],
    )


def _ref_d(r: RawRef) -> dict[str, object]:
    d: dict[str, object] = {"s": r.src, "k": r.kind.value, "n": r.name, "a": _span_d(r.at)}
    if r.local_root:
        d["l"] = 1  # omit when false/absent to keep the common-case blob small
    if r.receiver_type is not None:
        d["rt"] = r.receiver_type
    return d


def _ref(v: dict) -> RawRef:
    return RawRef(
        src=v["s"],
        kind=EdgeKind(v["k"]),
        name=v["n"],
        at=_span(v["a"]),
        local_root=bool(v.get("l")),
        receiver_type=v.get("rt"),
    )
