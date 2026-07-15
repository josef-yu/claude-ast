# Confidence-tier calibration (mechanics benchmark)

Are claude-ast's confidence tiers **calibrated**? The engine promises "report, don't rule":
a `definite` edge is a fact (~100% right), a `possible` edge is an honest maybe (lower, and
labelled so). The v1–v3 evals could not test this — their tasks were binary (an edge is in
the answer set or not), and the synthesis flagged it as the open thread:

> **Confidence calibration** — the `definite`/`possible` tiering; untested (tasks were binary).

This is that test. Unlike v1–v3 it is a **mechanics benchmark, no agents**: it scores the
resolver's *own output* against ground truth, with no LLM in the loop — so the result is a
property of the tool, not of a model + judge.

## What "calibrated" means here

For every edge the resolver emits, ask *is this edge real?* and bucket by the tier/source it
was given. Calibration holds iff **strict precision falls monotonically** as confidence drops
(`high` ≥ `medium` ≥ `low`) and **`definite` ≈ 100%** (any definite miss is a "report, don't
rule" violation — a false-definite bug).

"Real" splits by what is decidable:

- **dispatch** — *did the call actually go there?* Undecidable statically for a dynamic
  language, so we use the interpreter itself.
- **binding / existence / inheritance / import** — decidable, so we verify independently.

## Two oracles (both sound; different reach)

Two protocols, `RuntimeOracle` and `StaticOracle` (`verdicts.py`), answered by the Python
backend in `python/` — the same language-seam split the engine uses (`ingest` vs
`ingest/python`). A JS/TS subject would supply its own oracles behind these protocols.

1. **Runtime dispatch trace** (`python/runtime.py`) — run a driver under `sys.setprofile`,
   record each `(caller_file, line) → {callee id}` actually observed, and classify every CALL
   edge against its site: `exact` / `construction` (a class call → its `__init__`) / `override`
   (dispatch to a sub/superclass override — the "possible" case) / `same-name` / `contradicted`
   / `untraceable` / `unexercised`. Sound but **partial** — an unexercised site is *no evidence*
   (counted against coverage, never precision). The join works because a callee's
   `__module__ + '.' + co_qualname` is exactly the tool's symbol id.

   Three CPython-specific normalizations keep it honest (each pinned by a spike): builtins fire
   `c_call`, not `call`; calling a **class** dispatches to a constructor; calling a **builtin
   type** or an **Enum** member is structurally invisible to the tracer → `untraceable`, excluded
   from the denominator rather than scored as a miss.

2. **Static decidable audit** (`python/static.py`) — over *every* edge (including unexercised
   ones), verify the decidable claims independently of the resolver: import targets resolve to a
   real `.py`, inheritance holds under the runtime `__mro__`, builtins/externals actually import.
   Complete where it applies; this is the net that catches a false-definite in code no test runs.

The two are **reconciled**: a definite edge is a candidate bug only if runtime *contradicted* it
**and** the static audit did not *confirm* it. An edge the tracer can't see but the static oracle
proves real is reported as a tracer blind spot, not buried as a failure.

## Subject

Any Python project — the reference subject is claude-ast's own `src/` (dogfood, self-contained,
reproducible; the driver is its own test suite + a direct indexing pass, so a large fraction of
its call sites run, and being well-typed it exercises every resolver source). A foreign project
(e.g. Django) is passed as an argument.

## Running

A **language is a subcommand** (only `python` today) that carries its own arguments — how you run
a subject to observe dispatch is language-specific, so the driver flags live on `python`, not on
the neutral top-level command. A second backend would add its own subcommand the same way.

```sh
uv run python eval/calibration/run.py python                    # subject = claude-ast's own src/
uv run python eval/calibration/run.py python PROJECT_ROOT        # a foreign project (import-sweep)
uv run python eval/calibration/run.py python PROJECT_ROOT --no-runtime   # static audit only
```

`PROJECT_ROOT` is an **import root**: the directory whose subdirectories are the top-level
packages, so indexed ids are fully qualified *and* importable. For Django that is the **repo
root** (which contains `django/`), not `django/` itself — the latter would drop the `django.`
prefix and break `importlib`. The root is added to `sys.path` for the static oracle and drivers.

**The driver is the seam's one moving part** — an *in-process* entry point (a subprocess wouldn't
be traced), chosen by flags rather than a dedicated per-project module:

| invocation | driver |
|---|---|
| `python` (no project) | claude-ast's own test suite + a dogfood pass (rich) |
| `python ROOT` | **import-sweep**: import every indexed module (import-time dispatch only) |
| `python ROOT --driver pytest --target tests/` | run a pytest suite in-process |
| `python ROOT --driver script --target run.py --argv "…"` | run a Python file as `__main__` |
| `python ROOT --driver module --target pkg.cli --argv "…"` | run `python -m pkg.cli` in-process |
| `python ROOT --no-runtime` | skip tracing — static audit only |

Two flags widen what one process can reach:

- **`--init "CODE"`** — a Python snippet exec'd (project on `sys.path`) before any oracle imports
  subject code. E.g. `--init "import django; django.setup()"` populates the app registry so the
  static audit can import Django's model classes and *decide* their `mro` checks instead of
  skipping them.
- **`--trace-in T.json … --trace-out T.json`** — accumulate runtime coverage across **separate
  processes**. One traced run can only exercise so much (`setprofile` is slow; a crash forfeits
  the trace), so run one driver per process with `--trace-out`, then score once against the union
  via repeated `--trace-in` (with `--no-runtime` for a pure scoring pass). A trace is keyed by
  `(file, line)` sites — valid only for the exact checkout it was recorded on.

```sh
# per-app traced runs (separate processes), then one union-scored pass with mro unlocked:
run python ROOT --driver script --target tests/runtests.py --argv "… dispatch"    --trace-out t-dispatch.json
run python ROOT --driver script --target tests/runtests.py --argv "… httpwrappers" --trace-out t-http.json
run python ROOT --no-runtime --trace-in t-dispatch.json --trace-in t-http.json \
    --init "import django; django.setup()"
```

So a project's own test runner *is* a driver, with no special-casing — e.g. Django:

```sh
uv run python eval/calibration/run.py python /path/to/django \
  --driver script --target tests/runtests.py \
  --argv "--settings=test_sqlite --parallel=1 dispatch"
```

(That needs Django's own deps — `asgiref` etc. — importable in the running env.) On a foreign
project, function *bodies* run only when a driver calls them, so import-sweep gives import-time
dispatch only and the **static audit is the volume**; `--no-runtime` is the fast first pass on a
large codebase. The trace is best-effort: if a driver raises, the partial trace is still scored
and the static audit still runs.

Deterministic (no sampling, no model) — the numbers reproduce run to run. Layout: `edges.py`
(neutral edge model + graph helpers), `verdicts.py` (neutral vocabulary + oracle protocols),
`report.py` (neutral aggregation + the reconcile definition), `trace.py` (cross-run observation
maps), `run.py` (neutral dispatcher), `gate.py` (the CI gate); `python/` holds the backend —
`cli.py` (the `python` subcommand + its args), the runtime/static oracles, `ids.py`, `driver.py`.

## The CI gate

```sh
uv run python eval/calibration/gate.py
```

Re-runs the self-calibration and fails on any honesty regression, with floors set well under the
measured stable state (v5/v6): definite strict ≥ 95% (measured 99%), medium strict ≥ 70%
(measured 85%), zero static refutations, zero candidate false-definites — plus a driver-sanity
floor (≥ 200 traced definite sites), because an unexercised edge is never a miss and a broken
driver would otherwise pass vacuously. A separate CI step, not a pytest test: the driver *runs
the test suite* under the tracer, and a gate inside the suite would recurse.
