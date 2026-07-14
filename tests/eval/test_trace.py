"""Calibration trace persistence — the accumulate-across-runs seam.

The multi-run workflow (`--trace-out` / `--trace-in`) hinges on an ObservedMap surviving a
round-trip exactly and on merges being a true union: a site lost or a callee dropped would
silently shrink the runtime denominator and misreport coverage.
"""

from pathlib import Path

from calibration.trace import load_trace, merge_trace, save_trace


def test_trace_round_trips_exactly(tmp_path: Path) -> None:
    observed = {
        ("/a/b.py", 3): {"m.f", "m.C.__init__"},
        ("/a/c.py", 10): {"n.g"},
    }
    save_trace(observed, tmp_path / "t.json")
    assert load_trace(tmp_path / "t.json") == observed


def test_merge_unions_sites_and_callees() -> None:
    into = {("/a.py", 1): {"x"}, ("/b.py", 2): {"y"}}
    merge_trace(into, {("/a.py", 1): {"z"}, ("/c.py", 3): {"w"}})
    assert into == {("/a.py", 1): {"x", "z"}, ("/b.py", 2): {"y"}, ("/c.py", 3): {"w"}}


def test_empty_trace_round_trips(tmp_path: Path) -> None:
    save_trace({}, tmp_path / "t.json")
    assert load_trace(tmp_path / "t.json") == {}
