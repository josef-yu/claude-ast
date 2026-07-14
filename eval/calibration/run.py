"""Run the confidence-tier calibration and print the report as markdown.

    uv run python eval/calibration/run.py python                       # claude-ast's own src/
    uv run python eval/calibration/run.py python PROJECT_ROOT           # a foreign project
    uv run python eval/calibration/run.py python PROJECT_ROOT --no-runtime   # static audit only

The neutral dispatcher. A **language is a subcommand** that carries its own arguments — how you
run a subject to observe dispatch is language-specific, so those flags live on the ``python``
subcommand the Python backend contributes (``python/cli.py``), not here. One backend exists today
(mirroring the engine: no registry until a real second language lands); a second would register
its own subcommand the same way.

Bootstraps ``src/`` + ``eval/`` onto ``sys.path`` (the engine is importable via
``pythonpath=["src"]`` for pytest, not installed), then delegates to the chosen subcommand.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_EVAL_DIR = Path(__file__).resolve().parent.parent
_REPO = _EVAL_DIR.parent
_SRC = _REPO / "src"
for _p in (str(_SRC), str(_EVAL_DIR)):  # make claude_ast + the calibration package importable
    if _p not in sys.path:
        sys.path.insert(0, _p)

from calibration.python import cli as python_cli  # noqa: E402

_TESTS = _REPO / "tests"
_FIXTURES = _TESTS / "fixtures"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="calibration",
        description="Confidence-tier calibration (mechanics benchmark, no agents).",
    )
    subparsers = parser.add_subparsers(dest="language", required=True, metavar="LANGUAGE")
    python_cli.add_subcommand(subparsers, _SRC, _TESTS, _FIXTURES)

    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
