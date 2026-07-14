"""Confidence-tier calibration — a deterministic mechanics benchmark (no LLM agents).

Measures whether claude-ast's ``definite``/``possible`` tiers are *calibrated*: does a
definite edge really dispatch where it claims (~100%), and is a possible edge honestly
less sure? Two sound oracles — a runtime dispatch trace and a static decidable audit —
score the resolver's own output against ground truth. Entry point: ``run.py``.
"""
