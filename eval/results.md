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
