# Eval results — v1 (proof-of-harness)

**Verdict: no clear advantage for claude-ast over grep on these three Django ORM *planning*
tasks, at ~20% higher token cost.** The harness works (it discriminates per-ticket); the signal
is that these tasks are reasoning-bound, not navigation-bound, and the framing under-tests the tool.

## Setup

3 recently-fixed Django ORM tickets; each agent plans the fix (no code) against a `git worktree`
at the fix's **parent** commit. Two arms — **treatment** (claude-ast CLI) vs **control** (grep/read
only) — same model, prompt, checkout. N=3 trials/arm/ticket (18 solves). Each plan blind-judged
against a rubric hand-curated from the real merged patch. See [`README.md`](README.md).

## Blended (mean of 9 plans per arm)

| Metric | Treatment | Control |
|---|---|---|
| file-recall (named the right files) | **1.00** | 0.94 |
| approach (0–5, judge) | **4.00** | 3.67 |
| faithfulness (0–5) | 4.67 | 4.67 |
| crux identified (rate) | 0.67 | **0.78** |
| output tokens / plan (measured) | 28.2k | **23.4k** |

Treatment's quality edges (file-recall, approach) are small; control *beat* it on crux; faithfulness
tied; treatment cost ~20% more. Self-reported tool-calls were near-identical (~18 both arms).

## Per-ticket (where the blend hides the story)

| Ticket | shape | approach T / C | crux T / C |
|---|---|---|---|
| #37016 — `When()` → `Q()` invalid kwargs | cross-file, circular-import | 5.0 / 5.0 | 1.0 / 1.0 |
| #37057 — `UniqueConstraint` UNKNOWN | shared-method refactor | 2.0 / 1.3 | 0.0 / 0.33 |
| #20024 — `__in` None in `exclude()` | huge-file localization | 5.0 / 4.67 | 1.0 / 1.0 |

- **#37016 & #20024:** both arms near-aced them — grep found the answer too, so the tool added
  ~20% cost for no measurable gain.
- **#37057:** both arms largely failed — neither cracked "move the `Coalesce` out of the shared
  `Q.check` into the caller." Treatment found the right files; neither reliably got the crux.

## Why the tool didn't help (the finding)

The tickets **name their symbols** (`When`, `Q.check`, `build_filter`), so "find the definition" is
trivial for grep too — and the hard part (circular-import insight, three-valued logic, share-vs-caller
judgment) is the *model's reasoning*, which no navigation tool supplies. Treatment *added* claude-ast
alongside grep rather than *substituting* it — hence more tokens, same operations, same answers.

## Caveats (this is a proof-of-harness, not a verdict)

- **N=3** — deltas are within noise (crux 0.67 vs 0.78 could flip).
- **Framing biased *against* the tool** — naming the symbols up front neutralizes its discovery
  advantage; *planning* needs less exhaustive navigation than *implementing* (find every affected site),
  which is where `callers`/`references` should pay off.
- Judge subjectivity; soft control-leakage (control succeeding on its own argues against leakage).

## v2 direction

Give the tool the tasks it's actually *for*:

1. **Discovery / impact, symbols not named** — "symbol X is being changed; find every call site /
   reference / subclass that must be updated" — grep's weak spot (name-collision false positives,
   cross-file / dynamic false negatives).
2. **Set-valued, objectively scored** — anchor to real rename/deprecate/move commits that touched many
   sites; ground truth = the patch's actual affected set; score precision/recall/F1.
3. **More trials + tickets** to lift signal above noise.

---

# Eval results — v2 (discovery: find-all-callers)

**Verdict: a clean tie — treatment and control both scored perfect F1, at equal token cost.**

## Setup

One impact-analysis task: *"`django_file_prefixes` (moved in #37142) is being relocated — list every
`django/` (non-test) file that references it."* The target symbol is named; its **callers are not**
(the agent must discover them). Set-valued, so scored **deterministically** (file-set precision/recall/F1,
no judge). N=4/arm, worktree at the fix's parent commit, treatment (claude-ast) vs control (grep).

## Results (mean of 4 / arm; ground truth = 15 files)

| Metric | Treatment | Control |
|---|---|---|
| precision / recall / **F1** | 1.0 / 1.0 / **1.00** | 1.0 / 1.0 / **1.00** |
| output tokens / agent | 6,775 | 6,654 |
| tool-calls / files-read | 4.75 / 1.5 | 4.0 / 1.5 |

Both arms found all 15 files, every trial, at ~equal cost. As predicted: `django_file_prefixes` is a
**distinctive** name, so one `grep` returns the exact set with no false positives — nothing for symbol
resolution to add.

---

# Synthesis (v1 + v2)

Across *planning* (v1) and *find-all-callers* (v2), **claude-ast did not improve an LLM agent's accuracy
or efficiency on Django** — v1 cost +20% for no quality gain; v2 tied on both. The reason is consistent:
**LLM + grep is already sufficient** when symbols are distinctively named and the target is named. The
tool's theoretical edge — resolving *ambiguous* names, avoiding grep false-positives — is never exercised,
because tasks with objective (patch-derived) ground truth inherently involve distinctive names.

**Scope this honestly.** The finding is *"marginal on Django for batch LLM-agent tasks,"* not *"the tool
is wrong."* What these experiments do **not** measure, and where value may still exist:
- **Ambiguous-name disambiguation** — `callers Q.check` vs `grep ".check("` (drowns in false positives).
  Even here a capable LLM likely disambiguates by reading, so the edge, if any, is *reduced effort*, not a
  different answer. This needs *curated* ground truth (no patch gives it) — the open v3.
- **Confidence calibration** — the `definite`/`possible` tiering; untested (tasks were binary).
- **Messier / less-conventionally-named codebases** than Django, where text search is genuinely unreliable.
- **Interactive** (MCP, human-in-loop) use, vs the batch CLI harness here.

Bottom line: on a large but *well-structured* codebase, an LLM's own text-search competence sets a high
bar the tool didn't clear in these tasks. The differentiator matters most where text search fails — which
Django's clean conventions rarely trigger.

---

# Eval results — v3 (the tool's edge: ambiguous-name reverse-import)

**Verdict: accuracy tie, but the tool wins on effort — ~43% fewer output tokens and ~10x fewer file reads
for the same answer.** The first eval where claude-ast comes out ahead, and it does so exactly where predicted.

## Setup

Enumerate the modules that import a `base`-named module — `django.{template,views.generic,core.serializers}.base`.
`base` occurs 68x as a module and thousands of times as text, so grep genuinely drowns. Treatment leans on the
new **`importers`** reverse-import query; control greps. Ground truth from an **independent AST oracle**,
cross-checked to match the tool exactly (so treatment being right = the tool is *correct*). Deterministic
set-F1, N=4/arm/target, on a worktree.

## Results (mean of 12 / arm)

| Metric | Treatment | Control |
|---|---|---|
| precision / recall / **F1** | 0.90 / 1.0 / **0.944** | 0.90 / 1.0 / **0.944** |
| output tokens / agent | **5,560** | 9,738 |
| files read / agent | **0.08** | 0.83 |
| tool calls / agent | **3.4** | 6.4 |

Per target: `template.base` 1.0/1.0, `generic.base` 1.0/1.0, `serializers.base` 0.83/0.83 (tie throughout).

Accuracy is identical — a capable LLM+grep *disambiguates by reading* and reaches the same set. The tool's win
is **effort**: one resolved `importers` call vs grep-then-read-to-disambiguate.

**Limitation surfaced (`serializers.base`, 0.83 both):** both arms reported 2 importers the oracle *and* the
tool missed — `from django.core.serializers import base` style, where the imported name is a submodule. claude-ast's
`importers` recorded the *from-module* only, so it had a recall gap on `from parent import submodule`; the agents
caught the extras by reading. (My oracle shared the same gap, so the 0.83 understated *both* arms equally — the
tie stands.) **Fixed (this increment):** the resolver now also emits an edge for `from parent import submodule`
(from the resolved import map, in-tree modules only, deduped, no new persisted refs), so `serializers.base`
correctly returns all 7 — matching the agents. A reminder that the tool's set wasn't complete, now less so.

---

# Final synthesis (v1 + v2 + v3)

| Eval | Task | Symbol name | Accuracy | Effort |
|---|---|---|---|---|
| v1 | plan a fix | named in ticket | tie | tool **+20%** (loss) |
| v2 | find-all-callers | distinctive | tie | tie (wash) |
| v3 | find-all-importers | **ambiguous** | tie | tool **~2x cheaper** (win) |

**The tool never changes the final answer** — an LLM is thorough enough to reach it with grep + reading. **Its
value is *efficiency*, and only when the name is ambiguous:** then grep needs disambiguation reads and the tool
doesn't, so the resolved query is ~half the cost. On distinctive names (v1/v2) grep is already cheap, so there's
no win — and planning (v1) even costs *more*, because the tool's narrow query surface adds calls without
displacing the git-history/behavior/test work that dominates.

**Value regime:** ambiguous-name *resolution* queries (`callers`/`importers` where text search yields false
positives). Narrow on Django (clean, distinctive naming); it would widen on messier codebases. The honest
takeaway isn't "the tool is bad" — it's "for an LLM agent on a well-named codebase, the tool buys *effort*, not
*capability*, and only in its niche."

---

# Eval results — v4 (confidence-tier calibration: a mechanics benchmark, no agents)

**Verdict: the tiers are calibrated and honest.** `definite` edges are ~100% correct (98% of the executed
ones dispatch exactly where claimed; the 2% gap is entirely CPython tracer blind spots, each independently
confirmed — **zero real false-definites**), and strict dispatch precision falls **monotonically** as confidence
drops. "Report, don't rule" is not just a design slogan — it holds empirically. This is the open thread the
v1+v2 synthesis flagged ("confidence calibration… untested (tasks were binary)"), now closed.

## What's different from v1–v3

The first eval with **no LLM agents**. v1–v3 measured agent *task outcomes* (does claude-ast help vs grep);
this measures the resolver's *own output* against ground truth. So the result is a property of the tool, not
of a model + judge. Subject = **claude-ast's own `src/`** (dogfood, self-contained, reproducible). Harness +
method in [`calibration/`](calibration/); run with `uv run python eval/calibration/run.py`.

## Two oracles (both sound)

- **Runtime dispatch trace** — the driver (the test suite + a direct indexing pass) runs under `sys.setprofile`;
  each CALL edge is judged against what *actually dispatched* at its site. Sound but partial (an unexercised
  site is no-evidence, never a miss). This is the calibration curve.
- **Static decidable audit** — over *every* edge, verify the decidable claims independently of the resolver
  (import → a real `.py`; inheritance → the runtime `__mro__`; builtins/externals → they import). Catches a
  false-definite even in code no test runs.

Reconciled: a definite edge is a candidate bug only if runtime *contradicted* it **and** static did not
*confirm* it.

## The calibration curve (dispatch precision by confidence level)

| confidence | tier | CALL edges | traceable | strict | family |
|---|---|--:|--:|--:|--:|
| **high** | definite | 449 | 280 | **98%** | 98% |
| **medium** | possible | 61 | 50 | **84%** | 84% |
| **low** | possible | 10 | 10 | **50%** | 90% |

Strict = the exact target (or its constructor) ran; family = strict + dispatch to a sub/superclass override.
Monotone 98 → 84 → 50: **higher confidence really does mean higher precision.** The `low` name-match tier is
the story it was built for — only 50% of its guesses hit the exact leaf, but **90% land in the right
inheritance family** (40% are an override of the named member), and 0% are contradicted.

## By resolution source (the possible tier, unpacked)

| source | tier | edges | traceable | strict | family |
|---|---|--:|--:|--:|--:|
| syntactic | definite | 449 | 280 | 98% | 98% |
| inference (`x = Foo()`) | possible | 10 | 7 | 100% | 100% |
| annotation (`u: User`) | possible | 22 | 21 | 95% | 95% |
| stub (external `.pyi`) | possible | 29 | 22 | 68% | 68% |
| heuristic (name-match) | possible | 10 | 10 | 50% | 90% |

## Static audit — the honesty net

Independently, over all 606 decidable edges: **100% confirmed, 0 refuted** — imports (76) resolve to real
files, inheritance (8) holds under the MRO, builtins (195) and externals (77) all import, and every in-tree
call/reference target (248) is a real symbol. No definite edge that both oracles could reach was contradicted.

## The one subtlety that mattered (why the numbers are trustworthy)

A naive runtime oracle reported definite precision at **82%** — because it mislabeled two things as
contradictions that are not: (1) calling a class dispatches to `__init__` (a dataclass's is a generated
`__create_fn__`), and (2) CPython's `setprofile` fires *no event* for calling a builtin type (`str()`,
`tuple()`) and routes Enum calls through `EnumType.__call__`. Handling construction, `c_call`, and marking the
structurally-invisible kinds `untraceable` (excluded from the denominator, not scored as misses) lifted it to
**98% with zero real false-definites**. The remaining 3 runtime "contradictions" are all C-extension re-export
naming (`hashlib.sha256` → `_hashlib.openssl_sha256`), each confirmed real by the static audit. Distinguishing
*no evidence* from *counter-evidence* is the whole discipline of a calibration benchmark.

## Caveats

- **One well-typed codebase.** claude-ast's own src has few `low`/`stub` edges (N=10 heuristic), so those rows
  are directional, not tight. A messier, less-typed codebase would populate them.
- **Runtime coverage is 67% of definite CALL edges** (300/449 executed). The unexercised remainder leans on the
  static audit, whose in-tree-*call* check is only an *existence* bar (the target is a real symbol) — weaker
  than the dispatch or import/mro/builtin checks. So "zero false-definites" is strongest for the ~355 edges
  with a strong independent check; honest, but not a proof over every single edge.
- **`stub`/external dispatch is only partially traceable** (C methods report under impl names), so its 68% is a
  floor, not a verdict on the stub resolver.

## Synthesis with v1–v3

The agent evals said *the tool buys effort, not capability, and only for ambiguous names.* This one says
something orthogonal and load-bearing: *when the tool does answer, its confidence labels are honest* — a
`definite` you can build on, a `possible` that is genuinely, measurably less sure. That honesty is the whole
premise of "report, don't rule" for a dynamic language, and it is the first thing an LLM consumer needs to
trust the `min_confidence` dial.

---

# Eval results — v4 · Django at scale (same harness, real large codebase) — *interim*

**Verdict (so far): the honesty holds at 122k edges.** The static audit confirms `definite` ≈ 100% across all of
Django; the runtime dispatch trace (one test app) reads **96% strict** after a fix, monotone `high 96% > medium
84% > low 17%`. Zero real false-definites among executed edges. The new signals at scale: the `low` name-match
tier honestly *collapses* to 17% on a big dynamic codebase, and the run surfaced one genuine over-resolution.

## Setup

Same harness as v4, pointed at a foreign project: `run.py python /path/to/django`. Subject = the Django **repo
root** (6.2.dev), **122,822 edges** indexed. Runtime driver = Django's *own* test runner, in-process under
`sys.setprofile` with no Django-specific harness code: `--driver script --target tests/runtests.py --argv
"--settings=test_sqlite --parallel=1 dispatch"` (21 tests, **5,721 call sites**). Static audit runs over every
edge. Django's runtime deps (`asgiref`/`sqlparse`/`tzdata`) were installed into the venv. Two general driver
bugs were fixed to get here: `runpy.run_path` doesn't add the script's dir to `sys.path` like `python <script>`
does (so `--settings=test_sqlite` wasn't importable), and a driver that raises now degrades to a partial trace
instead of aborting.

*"So far":* the runtime trace is **one small test app** — it exercises 1,621 of 63,086 definite edges (2.6%).
The runtime curve is a slice; the static audit is the whole-codebase picture. Broader apps would widen it.

## Calibration curve (dispatch precision by confidence level)

| confidence | tier | edges | traceable | strict | family |
|---|---|--:|--:|--:|--:|
| **high** | definite | 63,086 | 1,621 | **96%** | 96% |
| **medium** | possible | 21,783 | 197 | 84% | 88% |
| **low** | possible | 19,047 | 1,181 | **17%** | 27% |

By source (possible unpacked): `inference` 85% strict, `stub` 64%, `heuristic` **17% / 27% family**.

## Static decidable audit (all 122k edges, independent of the resolver)

| check | tier | edges | confirmed | refuted | precision |
|---|---|--:|--:|--:|--:|
| existence | definite | 36,484 | 36,484 | 0 | 100% |
| builtin | definite | 16,584 | 16,584 | 0 | 100% |
| import | definite | 10,027 | 10,027 | 0 | 100% |
| external | definite | 10,053 | 9,811 | 8 | 100% |
| mro | definite | 8,844 | 2,823 | 0 | 100% |

`mro` is mostly *skipped* (6,021 of 8,844) — Django model classes need `django.setup()`'s app registry to import,
so the runtime-MRO check declines rather than guesses. Honest skip, not a pass.

## The 74% → 96% fix (a measure-first debugging note worth keeping)

The runtime definite number *first* read **74% strict / 19% "contradicted"**, alarmingly unlike claude-ast's 98%.
A diagnostic bucketing of the 309 contradicted-but-static-confirmed edges killed two wrong hypotheses before any
code changed: multi-line call attribution (**measured false** — `setprofile`'s caller line is exactly the AST
callee line) and module-aliasing `os.path`→`posixpath` (only 4/309). The real cause was **287/309 (93%): module
names bound to factory/wrapper-produced callables** — `gettext_lazy = lazy(...)`, whose runtime *code* qualname is
its definition site `lazy.<locals>.__wrapper__` (which `functools.wraps` can't change, since the tracer reads
`frame.f_code.co_qualname`, not the object's `__qualname__`). The fix matches by **object identity**: resolve the
edge's target to its runtime object and compare `__globals__['__name__'] + '.' + __code__.co_qualname` — the exact
string the tracer builds. Result: definite **74% → 96%**, contradicted **19% → 2%**, blind-spots **309 → 32**
(residual = genuine C-level callees with no Python code object to match). Self-run rose too (98→99, med 84→98).

## Findings

- **Static honesty holds at scale.** Every decidable definite check is 100% — imports resolve to real files,
  inheritance holds under the MRO, builtins/externals import. The 96% runtime number is a floor that the
  independent static audit corroborates upward, not inflation.
- **The `low` tier collapses to 17%** (vs 50% on claude-ast) — exactly what the weakest, name-match tier *should*
  do on a large dynamic codebase saturated with same-named methods (`save`/`get`/`render`). `inference` stays
  strong (85%). This is the calibration story working: the tiers separate more, not less, at scale.
- **One genuine over-resolution surfaced** (5 of 8 candidate false-definites): `sys.stdout.getvalue` recorded as
  a *definite* external edge through a **value** attribute (`sys.stdout` is a `TextIO`, not a submodule, so
  `getvalue`'s existence is type-dependent). It folds into the deferred typeshed-Tier-2 stub item (add a
  module-vs-value "shape" dimension + an external analog of the internal-root-defer rule at `binding._classify`).
  The other 3 candidates (`ctypes.WinDLL`, `uuid.uuid7`) are platform/version env artifacts of auditing on
  macOS/py3.13 — not tool defects.

## Caveats

- **Runtime = one test app.** 2.6% of definite edges exercised; the curve is directional, the static audit is the
  whole. A multi-app run is the obvious next step.
- **`mro` coverage is partial** without full app setup; `stub`/external dispatch is only partly traceable (C
  methods report under impl names).
- **8 "candidates" ≠ 8 bugs** — 3 are environment artifacts, 5 are the one `sys.stdout.getvalue`-shaped issue.

---

# Eval results — v5 (does the typeshed Tier-2 work pay off?)

**Verdict: yes — the Tier-2 arc grew honest coverage ~45%, the new edges are well-calibrated (stub 95% dispatch
on src), the calibration curve stayed monotone, and it added zero false-definites.** This is the loop v4 opened:
v4 measured the tool and surfaced #2; the fix ballooned into a full typeshed type-resolution layer (external chains,
in-tree chains, assignment inference, `Self`-covariance, property-kind); v5 re-measures the same tool to see whether
all that new resolution *earned its keep* — and whether it stayed honest.

## What changed since v4

Between v4 and v5 the resolver gained: external module-rooted chain resolution (the #2 fix — `sys.stdout.getvalue`
declines, `os.path.join` stays definite), arbitrary-depth call-return chaining (`re.compile(p).match(s).group()`),
typeshed member/return tables (full stdlib, ~1,187 classes), in-tree call-return chaining + assignment inference
(`make().run()`, `s = make(); s.inner()`), covariant `Self`, property-kind detection, and the retirement of the
old 24-type stub table for the full-stdlib typeshed table.

## The result (claude-ast's own src, full runtime coverage)

| metric | v4 | v5 |
|---|--:|--:|
| **possible-tier edges** | 71 | **103** (+45%) |
| &nbsp;&nbsp;— stub | 29 | 46 |
| &nbsp;&nbsp;— annotation | 22 | 34 |
| &nbsp;&nbsp;— inference | 10 | 13 |
| definite edges | 449 | 486 |
| possible ÷ resolved | 16% | **21%** |
| calibration curve (strict, high/med/low) | 98 / 84 / 50 | **99 / 85 / 50** |
| candidate false-definites | 0 | **0** |

Runtime dispatch by source (v5, src): `stub` **95%** strict (37 traceable), `inference` 100% (8), `annotation`
69% (29), `heuristic` 50% / 90% family (10). Static audit: **100% confirmed on every check**, new possible edges
included (external-possible 23/23, existence-possible 57/57, builtin-possible 23/23).

## Reading it

- **Honest coverage grew, and it's the *right* tier.** The +32 possible edges are stub/chain/return resolutions —
  member calls and chains that v4 simply dropped. The share of resolved edges landing in the hedged `possible`
  tiers rose 16% → 21% (controlling for the ~7% code growth). The tool now *says more*, at the honesty tier where
  saying-more is safe.
- **The new edges dispatch where they claim.** The headline is `stub` at **95% strict** — the new external
  member/chain edges genuinely go to the named target 95% of the time they run. Combined with the static audit's
  100% confirmation, the new MEDIUM edges are real *and* calibrated.
- **Honesty held.** The curve is still monotone (99 > 85 > 50), and there are still **zero false-definites** — the
  #2 fix, the chaining, and the in-tree typing added no false facts. "Report, don't rule" survived a large increase
  in what the tool reports.

## Django (static confirms it at scale; runtime is a thin floor)

Statically, all of it holds on Django: **#2 stays fixed** (candidates 8 → 3, the remaining three are `WinDLL`/
`uuid7` platform/version env artifacts, not bugs), and **stub edges grew 690 → 1,222** (+532) — the value-attribute
over-resolutions downgraded from false-definite to honest MEDIUM, plus new chain edges. Definite dropped 63,086 →
62,535 (the downgrades), possible rose 40,830 → 41,428.

The Django *runtime* trace (the `dispatch` app, with `asgiref`/`sqlparse`/`tzdata` via `uv run --with`) confirms
**definite ≈ 97% strict, #2 fixed**, but says little about the *new* edges: only **25 of 1,222 stub edges (2%)**
were exercised, and those trace at **52%** — a thin, C-method-attribution-limited floor (Django's stub edges skew to
builtin `str`/`dict` methods the tracer reports under impl names), not a calibration verdict. The static audit
confirms 100% of them regardless. So **src's 95% is the clean signal; Django's 52% is a coverage floor** — a
stdlib-heavy multi-app Django run would be needed for a solid Django stub sample, but src already answers it.

## Synthesis (v4 → v5)

v4 asked *"are the tiers honest?"* and found yes, but the tool answered a narrow slice. v5 asks *"did widening the
slice keep it honest?"* — and it did: **~45% more honest coverage, 95%-calibrated new edges, monotone curve, zero
false-definites.** The typeshed Tier-2 investment paid off exactly where it should — it made the tool *report more*
without making it *lie more*.

## Caveats

- **Code-growth confound.** src grew ~7% between v4 and v5 (new modules), so raw edge counts overstate slightly;
  the `possible ÷ resolved` ratio (16% → 21%) controls for it and is the fairer read.
- **Small runtime samples.** `stub`/`inference`/`annotation` on src are 37/8/29 traceable edges — directional, not
  tight. The static audit (100%, large-N) is the load-bearing correctness check.
- **Django runtime is a floor**, not a verdict, for the new edges (2% stub coverage, C-method attribution); its
  runtime driver also needs `asgiref`/`sqlparse`/`tzdata`, currently supplied via `uv run --with` (not in the lock).

---

# v5 addendum — the annotation 95% → 69% drop, explained (and two fixes)

**Verdict: not a resolver regression — a composition shift the harness misread.** Chasing the one number v5
left unexplained (annotation strict fell 95% → 69% between v4 and v5) surfaced one real engine defect and one
harness classification gap; with both fixed, **annotation family is 100%** (strict stays 69%, correctly).

## Fix 1 — chain edges now carry honest source provenance (engine)

Both chain resolvers stamped `Resolution.annotated()` unconditionally — including `self`-rooted chains, inferred
receivers (`s = make(); …`), and chains threaded through *body-inferred* return types (`def make(): return
Service()`). `Symbol.return_type` now records whether it was declared or inferred (`return_type_inferred`,
schema 17), and a chain edge is ANNOTATION only when **every** fact it used was declared — any inferred hop makes
it INFERENCE. Pinned by five new backend tests (including a warm-rebuild round-trip of the provenance flag).
On claude-ast's own fully-annotated src this moved **zero** edges — which is itself the diagnostic: the 69% was
not mislabeled inference.

## Fix 2 — protocol dispatch is family, not a miss (harness)

All 9 annotation misses were the same shape: the edge names a **`typing.Protocol` member**
(`stubs: StubProvider` → `StubProvider.type_member`) and runtime dispatched to a structural implementor
(`StdlibStubs.type_member`). Structural typing leaves no INHERITS edge, so `related_classes` couldn't see the
kinship and bucketed it `SAME_NAME`. A new `PROTOCOL` verdict checks the runtime objects (`__protocol_attrs__`,
never by name) and counts toward *family* — the exact analogue of OVERRIDE: the annotation named the static
type; dispatch went to an implementation. That is the `possible` disclaimer materializing, not an error.

## Why v4 → v5 moved

The Tier-2 refactor threaded `stubs: StubProvider` parameters through the resolvers, adding 12 annotation
edges of which 9 are protocol-typed receivers — a population shift toward exactly the dispatch the old
family definition couldn't credit.

## The curve after both fixes (src, full runtime coverage)

| confidence | tier | trace | strict | family (was) |
|---|---|--:|--:|--:|
| **high** | definite | 331 | 99% | 99% (99%) |
| **medium** | possible | 75 | 85% | **97%** (85%) |
| **low** | possible | 10 | 50% | **100%** (90%) |

By source: annotation 69% strict / **100% family**; stub 95%/95%; inference 100%; heuristic 50% / 100% family.
Zero candidate false-definites, static audit 100% confirmed — both unchanged.

## Reading it

The strict column is untouched — these fixes claim nothing new about exact dispatch. What changed is the
*explanation* of the gap: every annotation miss is now provably override-or-protocol dispatch, i.e. the
uncertainty MEDIUM was designed to disclose, with **0% contradicted**. The relabeling matters little on this
fully-annotated codebase but matters a lot on unannotated ones (Django's chains thread through inferred returns,
which were polluting the ANNOTATION bucket) — the Django re-run should now attribute per-source precision
honestly.

## Django re-run (post-fix): confirmed

Same invocation as v5 (repo root, `dispatch` app traced). Everything stable — definite **97% strict**, static
audit 100% on every decided check, candidate false-definites still exactly the 3 known platform/version env
artifacts (`WinDLL` ×2, `uuid7`; 31 further runtime contradictions were all static-confirmed C-level blind
spots). The relabel lands as predicted: Django's **ANNOTATION bucket is now empty** (the codebase is
essentially unannotated), and every in-tree typed resolution sits honestly in INFERENCE — a homogeneous,
well-calibrated population where a mislabeled mixture used to be. The PROTOCOL verdict correctly stayed silent
(low unchanged: Django's polymorphism is nominal, already credited via INHERITS) — evidence it credits
structure, not leniency. Unchanged: stub's floor (2% coverage, C-method attribution), `mro`'s setup-gated
skips, one-app runtime slice.

### Calibration curve (dispatch precision by confidence level)

| confidence | tier | edges | traceable | strict | family |
|---|---|--:|--:|--:|--:|
| **high** | definite | 62,535 | 1,606 | **97%** | 97% |
| **medium** | possible | 22,381 | 208 | 81% | 85% |
| **low** | possible | 19,047 | 1,181 | **17%** | 27% |

### By resolution source (the possible tier, unpacked)

| source | tier | edges | traceable | strict | family |
|---|---|--:|--:|--:|--:|
| inference | possible | 21,159 | 183 | **85%** | 90% |
| stub | possible | 1,222 | 25 | 52% | 52% |
| heuristic | possible | 19,047 | 1,181 | 17% | 27% |
| annotation | possible | **0** | — | — | — |

### Static decidable audit (all 122k edges, independent of the resolver)

| check | tier | edges | confirmed | refuted | skipped | precision |
|---|---|--:|--:|--:|--:|--:|
| existence | definite | 36,484 | 36,484 | 0 | 0 | 100% |
| existence | possible | 40,206 | 40,206 | 0 | 0 | 100% |
| builtin | definite | 16,536 | 16,536 | 0 | 0 | 100% |
| builtin | possible | 531 | 531 | 0 | 0 | 100% |
| import | definite | 10,027 | 10,027 | 0 | 0 | 100% |
| external | definite | 9,550 | 9,313 | 3 | 234 | 100% |
| external | possible | 691 | 691 | 0 | 0 | 100% |
| mro | definite | 8,844 | 2,823 | 0 | 6,021 | 100% |

(`external`'s 3 refutations are the adjudicated env artifacts above; its 234 skips are modules that don't
import in this venv, and `mro`'s 6,021 skips are the model classes gated on `django.setup()` — honest skips,
not passes.)
