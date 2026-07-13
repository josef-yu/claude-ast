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
