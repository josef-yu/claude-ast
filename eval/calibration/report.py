"""Aggregate edge verdicts into the calibration tables + a verdict.

Two views, both grouped by confidence tier (and by resolution source within it):

- **runtime dispatch** — over CALL edges the driver executed: what fraction dispatched to
  the exact target (strict), to the target *or* an override (family), and what fraction the
  interpreter contradicted. This is the calibration curve — it should fall from ``definite``
  to ``possible``. The precision denominator is the *traceable* subset: a site that ran but
  whose target is a tracer blind spot (builtin-type/enum call → ``untraceable``) is absence of
  evidence, not counter-evidence, and is excluded rather than scored as a miss.
- **static audit** — over every decidable edge: the independent confirm/refute rate.

The two are then *reconciled*: a definite edge is a candidate false-definite only if runtime
contradicted it **and** the static audit did not confirm it. An edge the tracer can't see but
the static oracle confirms is not a bug — it is reported as such, not buried.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from .edges import EdgeRecord
from .verdicts import StaticVerdict, Verdict

_CONFIRM = (Verdict.EXACT, Verdict.CONSTRUCTION)
_FAMILY = (Verdict.EXACT, Verdict.CONSTRUCTION, Verdict.OVERRIDE, Verdict.PROTOCOL)


@dataclass(frozen=True, slots=True)
class RuntimeRow:
    label: str
    total: int
    counts: dict[str, int]  # Verdict value -> n

    @property
    def executed(self) -> int:
        return self.total - self.counts.get(Verdict.UNEXERCISED.value, 0)

    @property
    def untraceable(self) -> int:
        return self.counts.get(Verdict.UNTRACEABLE.value, 0)

    @property
    def traceable(self) -> int:
        """Executed sites whose target the tracer can observe — the honest denominator."""
        return self.executed - self.untraceable

    def _rate(self, kinds: Sequence[Verdict]) -> float | None:
        if self.traceable == 0:
            return None
        return sum(self.counts.get(k.value, 0) for k in kinds) / self.traceable

    @property
    def strict(self) -> float | None:
        return self._rate(_CONFIRM)

    @property
    def family(self) -> float | None:
        return self._rate(_FAMILY)

    @property
    def contradiction(self) -> float | None:
        return self._rate((Verdict.CONTRADICTED,))


def runtime_row(label: str, verdicts: Sequence[Verdict]) -> RuntimeRow:
    return RuntimeRow(label, len(verdicts), dict(Counter(v.value for v in verdicts)))


@dataclass(frozen=True, slots=True)
class StaticRow:
    label: str
    confirmed: int
    refuted: int
    skipped: int

    @property
    def decided(self) -> int:
        return self.confirmed + self.refuted

    @property
    def precision(self) -> float | None:
        return self.confirmed / self.decided if self.decided else None


def static_row(label: str, verdicts: Sequence[StaticVerdict]) -> StaticRow:
    c = Counter(v.value for v in verdicts)
    return StaticRow(
        label,
        c.get(StaticVerdict.CONFIRMED.value, 0),
        c.get(StaticVerdict.REFUTED.value, 0),
        c.get(StaticVerdict.SKIPPED.value, 0),
    )


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:.0f}%"


def _tier_order(tier: str) -> int:
    return {"definite": 0, "possible": 1}.get(tier, 2)


def _runtime_line(r: RuntimeRow, indent: str = "") -> str:
    return (
        f"| {indent}{r.label} | {r.total} | {r.executed} | {r.traceable} | "
        f"{_pct(r.strict)} | {_pct(r.family)} | {_pct(r.contradiction)} | {r.untraceable} |"
    )


def format_report(
    call_edges: Sequence[EdgeRecord],
    call_verdicts: Sequence[Verdict],
    all_edges: Sequence[EdgeRecord],
    static_verdicts: Sequence[tuple[StaticVerdict, str]],
) -> str:
    """Render the full calibration report as markdown."""
    static_by_edge = {rec: v for rec, (v, _) in zip(all_edges, static_verdicts, strict=True)}
    lines: list[str] = ["# Confidence-tier calibration (mechanics benchmark, no agents)", ""]

    # --- headline: the calibration curve by confidence level (the axis being calibrated) ---
    by_conf: dict[str, list[Verdict]] = {}
    for rec, v in zip(call_edges, call_verdicts, strict=True):
        by_conf.setdefault(rec.confidence, []).append(v)
    order = {"high": 0, "medium": 1, "low": 2}
    lines += [
        "## Calibration curve — dispatch precision by confidence level",
        "",
        "The calibration axis: strict dispatch precision should fall as confidence drops "
        "(``high`` = definite; ``medium`` + ``low`` = possible). Read ``high`` together with the "
        "static audit — the runtime number is a *floor* wherever a call reaches a **C-level "
        "callee** (a builtin-type construction, an enum member, or a C function the tracer reports "
        "under its implementation name), which has no Python code object to match; those edges are "
        "reconciled against the static audit rather than scored as misses (see below).",
        "",
        "| confidence | tier | edges | trace | strict | family |",
        "|---|---|--:|--:|--:|--:|",
    ]
    for conf in sorted(by_conf, key=lambda c: order.get(c, 9)):
        r = runtime_row(conf, by_conf[conf])
        tier = "definite" if conf == "high" else "possible"
        lines.append(
            f"| **{conf}** | {tier} | {r.total} | {r.traceable} | "
            f"{_pct(r.strict)} | {_pct(r.family)} |"
        )
    lines.append("")

    # --- runtime dispatch, by tier then by (tier, source) ---
    by_tier: dict[str, list[Verdict]] = {}
    by_ts: dict[tuple[str, str], list[Verdict]] = {}
    for rec, v in zip(call_edges, call_verdicts, strict=True):
        by_tier.setdefault(rec.tier, []).append(v)
        by_ts.setdefault((rec.tier, rec.source), []).append(v)

    lines += [
        "## Runtime dispatch precision (CALL edges executed by the test suite)",
        "",
        "Strict = exact target or its constructor ran. Family = strict + dispatch to a sub/"
        "superclass override or a structural (protocol/interface) implementor. Contra = the site "
        "ran, produced callees, but the named member (or its family) never did. *Untr* "
        "(untraceable) sites — a builtin-type or enum call the tracer can't observe — are "
        "excluded from the precision denominator (*trace*).",
        "",
        "| group | edges | exec | trace | strict | family | contra | untr |",
        "|---|--:|--:|--:|--:|--:|--:|--:|",
    ]
    for tier in sorted(by_tier, key=_tier_order):
        lines.append(_runtime_line(runtime_row(f"**{tier}**", by_tier[tier])))
        for (t, src), vs in sorted(by_ts.items()):
            if t == tier:
                lines.append(_runtime_line(runtime_row(src, vs), indent="&nbsp;&nbsp;"))
    lines.append("")

    # --- static audit, by method then tier ---
    by_method: dict[tuple[str, str], list[StaticVerdict]] = {}
    for rec, (v, method) in zip(all_edges, static_verdicts, strict=True):
        by_method.setdefault((method, rec.tier), []).append(v)
    lines += [
        "## Static decidable audit (all edges, independent of the resolver)",
        "",
        "| check | tier | edges | confirmed | refuted | skipped | precision |",
        "|---|---|--:|--:|--:|--:|--:|",
    ]
    for (method, tier), vs in sorted(by_method.items()):
        if method == "n/a":
            continue
        r = static_row(method, vs)
        lines.append(
            f"| {method} | {tier} | {len(vs)} | {r.confirmed} | {r.refuted} | "
            f"{r.skipped} | {_pct(r.precision)} |"
        )
    lines.append("")

    # --- reconciliation: a candidate false-definite fails BOTH oracles ---
    candidates: list[tuple[EdgeRecord, str]] = []
    blind_spot = 0
    for rec, v in zip(call_edges, call_verdicts, strict=True):
        if rec.tier != "definite" or v is not Verdict.CONTRADICTED:
            continue
        if static_by_edge.get(rec) is StaticVerdict.CONFIRMED:
            blind_spot += 1  # tracer can't see it, but it's provably real
        else:
            candidates.append((rec, f"runtime-contradicted, static={static_by_edge.get(rec)}"))
    for rec, (v, method) in zip(all_edges, static_verdicts, strict=True):
        if rec.tier == "definite" and v is StaticVerdict.REFUTED:
            candidates.append((rec, f"static-refuted ({method})"))

    lines += ["## Candidate false-definites (definite edges *both* oracles fail)", ""]
    if blind_spot:
        lines.append(
            f"*{blind_spot} definite edges were runtime-contradicted but independently "
            f"**confirmed** by the static audit — the call reached a **C-level callee** with no "
            f"Python code object to match by identity (builtin-type construction, enum member, or "
            f"a C function reported under its implementation name), not a misresolution. "
            f"(Factory/wrapper-produced callees, whose code name is their definition site rather "
            f"than the bound name, are matched by object identity and count as hits.)*"
        )
        lines.append("")
    if not candidates:
        lines.append("None — every definite edge is confirmed by at least one oracle.")
    else:
        lines.append(f"{len(candidates)} definite edge(s) neither oracle confirms:")
        for rec, why in candidates:
            loc = f" at {rec.file}:{rec.line}" if rec.file else ""
            lines.append(f"- `{rec.src}` → `{rec.dst}` ({rec.source}; {why}){loc}")
    lines.append("")
    return "\n".join(lines)
