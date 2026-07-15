"""Oracle reconciliation — the one definition of "candidate false-definite".

The report renders it and the CI gate enforces it, so its edge cases are pinned here: a
runtime contradiction the static audit confirms is a blind spot (never a candidate); one it
cannot confirm is; a static refutation of a definite edge is a candidate regardless of the
runtime verdict; and the possible tier never produces candidates at all.
"""

from calibration.edges import EdgeRecord
from calibration.report import reconcile
from calibration.verdicts import StaticVerdict, Verdict


def _edge(dst: str, tier: str = "definite") -> EdgeRecord:
    confidence = "high" if tier == "definite" else "medium"
    return EdgeRecord(
        src="m.f", dst=dst, kind="call", tier=tier, source="syntactic",
        confidence=confidence, file="/m.py", line=1, external=False, dst_kind="function",
    )


def test_static_confirmed_contradiction_is_a_blind_spot_not_a_candidate() -> None:
    edges = [_edge("m.g")]
    blind, candidates = reconcile(
        edges, [Verdict.CONTRADICTED], edges, [(StaticVerdict.CONFIRMED, "existence")]
    )
    assert blind == 1 and candidates == []


def test_unconfirmed_contradiction_is_a_candidate() -> None:
    edges = [_edge("m.g")]
    blind, candidates = reconcile(
        edges, [Verdict.CONTRADICTED], edges, [(StaticVerdict.SKIPPED, "n/a")]
    )
    assert blind == 0 and [rec.dst for rec, _ in candidates] == ["m.g"]


def test_static_refutation_is_a_candidate_even_when_runtime_confirms() -> None:
    # the static audit outranks a lucky-looking trace: a refuted definite is always a candidate
    edges = [_edge("m.g")]
    _, candidates = reconcile(
        edges, [Verdict.EXACT], edges, [(StaticVerdict.REFUTED, "existence")]
    )
    assert [why for _, why in candidates] == ["static-refuted (existence)"]


def test_possible_tier_never_produces_candidates() -> None:
    edges = [_edge("m.g", tier="possible")]
    blind, candidates = reconcile(
        edges, [Verdict.CONTRADICTED], edges, [(StaticVerdict.REFUTED, "existence")]
    )
    assert blind == 0 and candidates == []


def test_unexercised_edges_are_not_candidates() -> None:
    edges = [_edge("m.g")]
    blind, candidates = reconcile(
        edges, [Verdict.UNEXERCISED], edges, [(StaticVerdict.SKIPPED, "n/a")]
    )
    assert blind == 0 and candidates == []
