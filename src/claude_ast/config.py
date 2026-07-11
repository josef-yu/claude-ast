"""Runtime configuration — resolver selection, trust mode, watch scope.

Read from ``.claude-ast/config.toml`` (via stdlib ``tomllib``) with sane
defaults that mirror the design brief. The full schema lands with the resolver
stack (P2); this is the P0 skeleton.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class Config:
    """Defaults: ``both-with-flags``, subtype-tolerant, call-site tracing off."""

    trust: str = "both-with-flags"  # trust-annotations | trust-observed | both-with-flags
    call_site_tracing: bool = False  # the expensive resolver — opt-in
    exclude: tuple[str, ...] = (".venv", "__pycache__", ".git", ".claude-ast")


DEFAULT = Config()
