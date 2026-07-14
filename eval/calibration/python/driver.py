"""Subject drivers — how the subject's code is made to *run* so the tracer can observe dispatch.

The runtime oracle needs the code to actually execute. The one hard constraint is that a driver
must run **in the same process** under ``sys.setprofile`` — a subprocess isn't traced — so a
driver can't be an arbitrary shell command; it's an in-process entry point. That still fully
generalizes: rather than a dedicated driver per project, the caller picks one via CLI args —

- ``build_import_driver`` — import every indexed module (universal; import-time dispatch only);
- ``build_pytest_driver`` — run a pytest suite in-process;
- ``build_script_driver`` / ``build_module_driver`` — run a script / ``python -m`` entry point
  in-process with a chosen argv (this is how a project's own test runner becomes a driver, e.g.
  Django's ``tests/runtests.py`` — no Django-specific code here).

``build_driver`` is the one exception kept as code: claude-ast's own rich driver (its test suite
+ a dogfood pass over queries), used when no subject is given. A test runner calls ``sys.exit``,
so the script/module drivers swallow ``SystemExit`` and restore ``sys.argv``.
"""

from __future__ import annotations

import importlib
import runpy
import sys
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

from claude_ast.index import Index


def build_driver(src: Path, tests: Path, fixtures: Path) -> Callable[[], None]:
    """A no-arg callable that runs the suite then the dogfood pass, both under the tracer.

    The rich driver for claude-ast's own source — its test suite exercises the resolvers and
    the dogfood pass drives every query, so most of its own call sites actually run.
    """

    def driver() -> None:
        _run_pytest(tests)
        _dogfood(src, fixtures)

    return driver


def build_import_driver(module_ids: Iterable[str]) -> Callable[[], None]:
    """A universal driver for an arbitrary project: import every module the index found.

    We can't know a foreign project's test runner, but importing each module under the tracer
    still executes its module-level code — top-level calls, decorators, and class creation
    (which runs base-class dispatch) — so import-time CALL/INHERITS edges get observed. Function
    *bodies* run only when called, so runtime coverage is import-time only; the static audit is
    the volume on a foreign project. Parents import before children (sorted), each guarded so a
    settings-dependent module that raises on import just drops out of coverage.
    """
    ids = sorted(module_ids)

    def driver() -> None:
        ok = failed = 0
        for i, mid in enumerate(ids):
            try:
                importlib.import_module(mid)
                ok += 1
            except BaseException:  # noqa: BLE001 — any import-time failure just skips this module
                failed += 1
            if (i + 1) % 500 == 0:
                print(f"[calibration] import-sweep {i + 1}/{len(ids)}…", file=sys.stderr)
        print(f"[calibration] import-sweep: {ok} imported, {failed} failed", file=sys.stderr)

    return driver


def build_pytest_driver(path: Path, argv: Sequence[str] = ()) -> Callable[[], None]:
    """Run a pytest suite at ``path`` in-process under the tracer, with extra ``argv``."""

    def driver() -> None:
        _run_pytest(path, argv)

    return driver


def build_script_driver(path: Path, argv: Sequence[str] = ()) -> Callable[[], None]:
    """Run a Python file as ``__main__`` in-process (``python path argv…``) under the tracer.

    The general way to turn a project's own test runner into a driver — e.g. Django's
    ``tests/runtests.py``. ``sys.argv`` is set for the run and restored after; a runner's
    ``sys.exit`` is swallowed so scoring still happens. The script's own directory is put on
    ``sys.path`` (as ``python <script>`` does, but ``runpy.run_path`` does not) so a runner can
    import its siblings — e.g. Django's ``--settings=test_sqlite`` living beside ``runtests.py``.
    """

    def driver() -> None:
        _run_as_main(
            lambda: runpy.run_path(str(path), run_name="__main__"),
            str(path), argv, syspath0=str(path.parent),
        )

    return driver


def build_module_driver(name: str, argv: Sequence[str] = ()) -> Callable[[], None]:
    """Run a module as ``__main__`` in-process (``python -m name argv…``) under the tracer."""

    def driver() -> None:
        _run_as_main(
            lambda: runpy.run_module(name, run_name="__main__", alter_sys=True), name, argv
        )

    return driver


def _run_as_main(
    run: Callable[[], object], target: str, argv: Sequence[str], syspath0: str | None = None
) -> None:
    saved_argv, saved_path = sys.argv, sys.path[:]
    sys.argv = [target, *argv]
    if syspath0 is not None:
        sys.path.insert(0, syspath0)  # mimic `python <script>` adding the script's dir to path
    try:
        run()
    except SystemExit:
        pass  # a test runner exits with its failure count — that's not our exit
    finally:
        sys.argv, sys.path[:] = saved_argv, saved_path


def _run_pytest(path: Path, argv: Sequence[str] = ()) -> None:
    try:
        import pytest
    except ImportError:
        print("[calibration] pytest unavailable — skipping suite driver", file=sys.stderr)
        return
    # -p no:cacheprovider avoids writing .pytest_cache; -q trims noise.
    pytest.main(["-q", "-p", "no:cacheprovider", str(path), *argv])


def _dogfood(src: Path, fixtures: Path) -> None:
    for root in (src, fixtures):
        idx = Index.build(root, use_store=False)
        for sym in idx.graph.symbols():
            idx.find_callers(sym.id)
            idx.find_dependencies(sym.id)
            idx.find_references(sym.id)
        idx.repo_map(budget=2000)
