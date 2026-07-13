# claude-ast application eval

Does claude-ast help an agent **plan a real change** to a large unfamiliar codebase
(Django), versus an agent with only text tools? This is an *application* test of the
tool's value on its actual use case — not a mechanics benchmark — so a positive result is
convincing and a null result is ambiguous (plan quality also depends on the model + judge).

## Design

- **Task:** a real Django ticket. Each agent produces an **implementation plan** (not code):
  the approach, and a structured **touch-set** — the files/symbols it would modify. Planning
  is where codebase comprehension pays off, and it isolates the tool's contribution from raw
  coding skill (and is far cheaper than running full implementations).
- **Two arms**, identical but for the toolset:
  - **treatment** — claude-ast CLI (`def` / `callers` / `deps` / `outline` / `repo-map`) + read.
  - **control** — `grep` / `glob` / read only; the tool is neither mentioned nor present.
- **Ground truth** — hand-curated, but **anchored to the real merged patch**. Each agent works
  in a `git worktree` at the fix's **parent** commit (no fix present); the fixing commit's diff
  is distilled into a per-ticket [`rubric.md`](tickets/). Objective *and* uncheatable.
- **Interface:** CLI. **Scope:** proof-of-harness — does it discriminate at all.

## Scoring

- **Touch-set precision/recall** — the agent's named files/symbols vs the patch's actual
  changed set. Semi-objective; catches hallucinated locations (grep guesses) and missed sites.
- **Plan quality** — a **blind** LLM judge (arm identity stripped) scores against the rubric:
  correctness of approach, completeness of affected sites, and **faithfulness** (are the claimed
  locations/relationships true).
- **Effort** — tool calls, files read, tokens, turns per arm (workflow telemetry).
- Reported **per ticket and per arm**, with win-rates — to see *where* the tool helps.

## Tickets (v1)

| Ticket | Shape | Why chosen |
|---|---|---|
| [#37016](tickets/37016/) — `When()` propagates invalid kwargs to `Q()` | 3 files; import / circular-dep reasoning | fix location driven by the import graph, not the symptom — where `deps`/`callers` should beat grep |
| [#37057](tickets/37057/) — `UniqueConstraint` mishandles `UNKNOWN` condition | 2 files; caller-context reasoning | the fix moves logic out of a *shared* method into the *caller* — needs `callers(Q.check)` + domain knowledge |
| [#20024](tickets/20024/) — `__in` with `None` in `exclude()` | 1 huge file; deep localization | contrast case: find the needle in `sql/query.py` (~2,600 lines) — tests whether the tool helps where cross-file reasoning doesn't |

## Running (once the framework is approved)

1. **Setup (done for this run):** worktrees at parent commits, indexed with an external cache
   so the tree stays pristine for the control arm (see *Current run* below).
2. **Treatment arm workflow:** pipeline `(ticket × N trials)` → agent-with-claude-ast → plan +
   touch-set + effort.
3. **Control arm workflow:** same, grep-only.
4. **Score + synthesize:** touch-set P/R + blind judge per plan; aggregate accuracy / faithfulness
   / effort. `N = 3` trials/arm to average out model stochasticity.

## Threats to validity

- **Control leakage** — subagents have `Bash`; enforcement is prompt + no index in the tree +
  not mentioning the tool. Soft; flagged in results.
- **Judge bias** — mitigated by blind grading, rubric-anchoring, and the semi-objective touch-set P/R.
- **Single-ticket variance** — 3 tickets × N trials is a proof, not a verdict.
- **Tool-vs-reasoning confound** — same model both arms, so the *delta* isolates the tool.

## Current run

Django checkout: `/Users/josephyu/Development/django` at `main` (6.2.dev). Worktrees + external
index cache live under the session scratchpad:

```
eval/wt-37016  @ 123fa3a3f3   (parent of fix 3b161e6096)
eval/wt-37057  @ 63c56cda13   (parent of fix 61a62be313)
eval/wt-20024  @ 3fb37ef411   (parent of fix cec10f992b)
```

Treatment invocation pattern (index pre-built into the external cache):

```sh
cd /Users/josephyu/Development/claude-ast
CLAUDE_AST_CACHE_DIR=<scratchpad>/eval/idx uv run claude-ast <cmd> <worktree>
```
