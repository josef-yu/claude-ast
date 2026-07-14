"""The Python backend's calibration subcommand — its own args + handler, behind the seam.

The neutral ``run`` command knows nothing about drivers: how you *run* a subject to observe
dispatch is language-specific (pytest, a ``runpy`` script, an import-sweep), so those flags live
on the ``python`` subcommand this module contributes. A future JS/TS backend would add its own
subcommand (``--driver npm|node…``) the same way — no shared-arg abstraction until a second
language exists to validate it (the engine's rule).

``add_subcommand`` registers the parser + its handler; ``run`` builds the index, wires the Python
oracles + the chosen driver behind the neutral protocols, and prints the report.
"""

from __future__ import annotations

import argparse
import shlex
import sys
from collections.abc import Callable
from pathlib import Path

from claude_ast.index import Index

from ..edges import enumerate_edges, module_ids
from ..report import format_report
from ..verdicts import ObservedMap, RuntimeOracle, StaticOracle
from .driver import (
    build_driver,
    build_import_driver,
    build_module_driver,
    build_pytest_driver,
    build_script_driver,
)
from .runtime import PythonRuntimeOracle
from .static import PythonStaticOracle


def add_subcommand(
    subparsers: argparse._SubParsersAction, src: Path, tests: Path, fixtures: Path
) -> None:
    """Register the ``python`` subcommand. ``src``/``tests``/``fixtures`` are the claude-ast
    default subject (the reference project, indexed when no ``project`` is given)."""
    p = subparsers.add_parser(
        "python",
        help="calibrate a Python project",
        description="Confidence-tier calibration for a Python project.",
    )
    p.add_argument(
        "project",
        nargs="?",
        help="import root to index + score (dir whose subdirs are the top-level packages). "
        "Default: claude-ast's own src/.",
    )
    p.add_argument(
        "--no-runtime",
        action="store_true",
        help="skip the runtime dispatch trace; run the static decidable audit only.",
    )
    p.add_argument(
        "--driver",
        choices=("import", "pytest", "script", "module"),
        help="how to run the subject under the tracer. import = import every indexed module "
        "(default for a foreign project); pytest/script/module run a suite / a Python file / a "
        "'python -m' entry point in-process with --argv. Default with no project: claude-ast's "
        "own test suite + dogfood.",
    )
    p.add_argument(
        "--target",
        help="the pytest path / script path / module name for --driver (paths are resolved "
        "against the project root).",
    )
    p.add_argument(
        "--argv",
        default="",
        help="argv for the pytest/script/module driver, as one shell-quoted string, e.g. "
        "--argv '--settings=test_sqlite --parallel=1 auth'.",
    )
    p.set_defaults(func=lambda args: run(args, src, tests, fixtures))


def run(args: argparse.Namespace, src: Path, tests: Path, fixtures: Path) -> int:
    if args.project:
        subject = Path(args.project).resolve()
        if str(subject) not in sys.path:
            sys.path.insert(0, str(subject))  # so the static oracle + import-sweep can load it
        is_self = False
    else:
        subject = src  # already on sys.path
        is_self = True

    index = Index.build(subject, use_store=False)
    graph = index.graph
    modules = module_ids(graph)
    edges = enumerate_edges(graph)

    runtime: RuntimeOracle = PythonRuntimeOracle(graph, modules)
    static: StaticOracle = PythonStaticOracle(graph, modules, subject)
    driver, driver_desc = _build_driver(args, subject, is_self, modules, src, tests, fixtures)

    print(f"[calibration] subject={subject} edges={len(edges)} "
          f"coverage(metrics)={index.metrics.coverage:.1%}", file=sys.stderr)
    print(f"[calibration] driver: {driver_desc}", file=sys.stderr)
    observed: ObservedMap = {}
    if driver is not None:
        observed = runtime.trace(driver, str(subject))
        print(f"[calibration] observed {len(observed)} in-subject call sites", file=sys.stderr)

    call_edges = [e for e in edges if e.kind == "call"]
    call_verdicts = [runtime.judge(e, observed) for e in call_edges]
    static_verdicts = [static.audit(e) for e in edges]

    print(format_report(call_edges, call_verdicts, edges, static_verdicts))
    return 0


def _build_driver(
    args: argparse.Namespace,
    subject: Path,
    is_self: bool,
    modules: frozenset[str],
    src: Path,
    tests: Path,
    fixtures: Path,
) -> tuple[Callable[[], None] | None, str]:
    """Pick the in-process driver from the subcommand's args; returns ``(driver, description)``."""
    if args.no_runtime:
        return None, "none (static audit only)"

    driver_argv = shlex.split(args.argv)
    kind = args.driver or ("self" if is_self else "import")

    if kind == "self":
        return build_driver(src, tests, fixtures), "test suite + dogfood"
    if kind == "import":
        return build_import_driver(modules), f"import-sweep ({len(modules)} modules)"

    if not args.target:
        raise SystemExit(f"--driver {kind} requires --target")
    if kind == "module":
        return build_module_driver(args.target, driver_argv), f"module {args.target} {driver_argv}"
    target = Path(args.target)
    if not target.is_absolute():
        target = subject / target
    if kind == "pytest":
        return build_pytest_driver(target, driver_argv), f"pytest {target} {driver_argv}"
    return build_script_driver(target, driver_argv), f"script {target} {driver_argv}"
