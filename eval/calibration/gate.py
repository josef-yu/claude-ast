"""CI calibration gate — freeze the honesty floor on claude-ast's own src.

    uv run python eval/calibration/gate.py

v4–v6 measured what stable looks like (definite ~99% strict on ~330 traced sites, medium ~85%,
zero static refutations, zero candidate false-definites). This gate re-runs the self-calibration
— deterministic, hermetic, ~a minute — and fails if any honesty invariant regresses, so a
resolver change that quietly starts minting false facts is caught by the harness that defined
them. It is a separate CI step, not a pytest test: the runtime driver *runs the test suite*
under the tracer, and a gate inside that suite would recurse.
"""

from __future__ import annotations

import sys
from pathlib import Path

_EVAL_DIR = Path(__file__).resolve().parent.parent
_REPO = _EVAL_DIR.parent
for _p in (str(_REPO / "src"), str(_EVAL_DIR)):  # engine + harness importable, as in run.py
    if _p not in sys.path:
        sys.path.insert(0, _p)

from calibration.edges import enumerate_edges, module_ids  # noqa: E402
from calibration.python.driver import build_driver  # noqa: E402
from calibration.python.runtime import PythonRuntimeOracle  # noqa: E402
from calibration.python.static import PythonStaticOracle  # noqa: E402
from calibration.report import reconcile, runtime_row  # noqa: E402
from calibration.verdicts import StaticVerdict  # noqa: E402
from claude_ast.index import Index  # noqa: E402

# Floors sit well under the measured stable state so a failure means a real regression, not
# sampling noise. MIN_DEFINITE_TRACED guards the vacuous pass: an unexercised edge is never a
# miss, so a silently-broken driver would otherwise sail every precision check at 0 coverage.
DEFINITE_STRICT_FLOOR = 0.95  # measured 0.99 (v5/v6 self-runs)
MEDIUM_STRICT_FLOOR = 0.70    # measured 0.85
MIN_DEFINITE_TRACED = 200     # measured ~330


def main() -> int:
    src, tests, fixtures = _REPO / "src", _REPO / "tests", _REPO / "tests" / "fixtures"
    index = Index.build(src, use_store=False)
    graph = index.graph
    modules = module_ids(graph)
    edges = enumerate_edges(graph)

    runtime = PythonRuntimeOracle(graph, modules)
    static = PythonStaticOracle(graph, modules, src)
    observed = runtime.trace(build_driver(src, tests, fixtures), str(src))

    call_edges = [e for e in edges if e.kind == "call"]
    call_verdicts = [runtime.judge(e, observed) for e in call_edges]
    static_verdicts = [static.audit(e) for e in edges]

    by_conf = {
        conf: runtime_row(
            conf,
            [v for e, v in zip(call_edges, call_verdicts, strict=True) if e.confidence == conf],
        )
        for conf in ("high", "medium")
    }
    refuted = sum(1 for v, _ in static_verdicts if v is StaticVerdict.REFUTED)
    _, candidates = reconcile(call_edges, call_verdicts, edges, static_verdicts)

    high, medium = by_conf["high"], by_conf["medium"]
    checks = [
        (
            f"definite traced sites >= {MIN_DEFINITE_TRACED} (driver sanity)",
            high.traceable >= MIN_DEFINITE_TRACED,
            str(high.traceable),
        ),
        (
            f"definite strict >= {DEFINITE_STRICT_FLOOR:.0%}",
            (high.strict or 0.0) >= DEFINITE_STRICT_FLOOR,
            f"{(high.strict or 0.0):.1%} of {high.traceable}",
        ),
        (
            f"medium strict >= {MEDIUM_STRICT_FLOOR:.0%}",
            (medium.strict or 0.0) >= MEDIUM_STRICT_FLOOR,
            f"{(medium.strict or 0.0):.1%} of {medium.traceable}",
        ),
        ("static refutations == 0", refuted == 0, str(refuted)),
        ("candidate false-definites == 0", not candidates, str(len(candidates))),
    ]

    failed = False
    for label, ok, actual in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {label}  [{actual}]")
        failed = failed or not ok
    for rec, why in candidates:
        loc = f" at {rec.file}:{rec.line}" if rec.file else ""
        print(f"      `{rec.src}` -> `{rec.dst}` ({rec.source}; {why}){loc}")
    print("calibration gate:", "FAILED" if failed else "passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
