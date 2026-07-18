# claude-ast

AST-backed code navigation for Claude Code — precise, structural answers over a
real Python syntax tree, with **graded confidence** for a dynamic language.

Claude Code navigates code by grepping text. Text can't tell a definition from a
call, follow an import, or name the callers of a function. `claude-ast` answers
those as precise queries over a Python AST index — and, where a dynamic language
makes an answer uncertain, it tells you *how sure it is* rather than guessing.

```console
$ claude-ast repo-map src --focus claude_ast.query --budget 300
claude_ast.query.relations
  class Reference    # One end of a relationship: the other symbol, plus how/where and how sure.
  def find_callers(graph, symbol) -> list[Reference]    # Symbols that call `symbol`.
  ...
$ claude-ast callers claude_ast.cli._cmd_index src
src/claude_ast/cli.py:50  [definite] call  claude_ast.cli.main
```

## Status

The **P1 engine and the P2 resolver stack are complete**, validated on the Django
source tree (~17.7k symbols in the `django/` package; warm re-index ~0.3s). The
**P3 delivery layer is complete too**: the **MCP server** (`claude-ast-mcp`) serves
the engine to Claude Code over stdio, and a **live watcher** keeps its index fresh
as you edit — each save re-resolves only the files a change actually affects
(reverse-import + heuristic-name closure), not the whole project. Nothing here
calls an LLM — it's fully deterministic and local.

## What it does

One shared AST index, three query families:

- **Lookup** — `find_definition`, `outline`
- **Relationships** — `find_callers`, `find_references`, `find_dependencies` (each result tiered `definite` / `possible`)
- **Orientation** — `repo_map` (a ranked, token-budgeted skeleton)

## Architecture

```
ingest → resolve (per backend) → store (sqlite + in-mem graph) → query (+ ranker) → server (mcp)
                                        ↑
                                     watch — feeds changed files back in
```

- **`model/`** — the normalized contract (`Symbol` / `Edge` / `Resolution{source, confidence}`) every layer speaks.
- **`ingest/`** — language backends behind an `Indexer` protocol; `ingest/python/` is the one backend (all `ast` lives there). Produces symbols + syntactic edges.
- **`store/`** — SQLite snapshot for warm restart + per-file incremental, behind a `Store` protocol.
- **`query/`** — pure functions over the graph: lookups, relationships, and `repo_map` (confidence-weighted PageRank).
- **`index.py`** — the `Index` orchestrator (and `IndexSession`, the long-lived, patchable view the server serves).

Design principles: **own the whole stack** (our own parser + resolvers; external
engines can slot in later), **deterministic & local** (no LLM, no external
service, no API cost), and **report, don't rule** — every edge carries an honest
confidence tier, so `definite` really means definite.

## Resolution

Every edge is tiered `definite` or `possible` and tagged with how it was found, so
`definite` really means definite and a guess is never dressed up as a fact. The ladder:

| Tier | Source | Resolves |
|------|--------|----------|
| `definite` | syntactic | direct calls & attribute reads, imports (absolute · relative · package re-exports), inheritance |
| `definite` | external | library/stdlib targets as `external` nodes — from-import calls, module-rooted attributes (`os.path.join`), builtins (`len`); kept out of ranking |
| `definite` | call-site | `RECEIVES_ARG` — the concrete type flowing into a parameter (`g(User())` → `g` receives `User`); an observation, never a dispatch claim |
| `possible` | annotation · inference | typed receivers — `u: User`, `x = User()`, `self.m()` / `self.attr` → the member (called *or* read), threaded across multi-member chains (`self.a.b` via `a`'s declared or constructed type, including instance attributes set as `self.a = A()`) and cross-file through bases and re-exports. A **union** parameter annotation fans out to one edge per arm (`u: User \| Admin` → both `User.m` and `Admin.m`); `Optional[X]` / `X \| None` collapse to `X` |
| `possible` | stub | members on external **stdlib** types (`p: Path; p.exists()`), from a frozen, generated member table |
| `possible` | heuristic | name-match for an untyped receiver — a bare `obj.m`, or the last hop of a chain whose intermediate is an untyped data attribute (`self.x.m` with `self.x` untyped) — capped so an over-common name stays silent |

Both a method call and a bare **attribute read** (`obj.attr` with no call) flow through this one
ladder: a call emits a `CALL` edge restricted to callable targets, a read emits a `REFERENCE`
edge that may also land on a data attribute — so `find_references` now sees pure reads, not just
calls. `claude-ast index` reports the coverage and tier/source split it achieves (on its own
`src/`: ~72% of references bound). Consumers dial the floor with `--min-confidence`
(default `medium`) — the reliable set by default, the `low` heuristics only on demand.

## CLI

```console
claude-ast index <path>              # build/update the index; print a summary
claude-ast status <path>             # index freshness (cold vs. warm snapshot)
claude-ast def <name> [path]         # where a name is defined
claude-ast outline <module> [path] [--focus <symbol>]   # a module's symbols (submodules collapsed)
claude-ast callers <symbol> [path] [--min-confidence high|medium|low] [--reassignments split|off|union] [-s/--source]
claude-ast deps <symbol> [path] [--min-confidence high|medium|low] [--reassignments split|off|union] [-s/--source]
claude-ast importers <module> [path] [-s/--source]                                    # modules that import a module
claude-ast repo-map [path] [--focus <id>] [--budget N]
```

`-s/--source` (with optional `--context N`) prints the code at each resolved site — a
"grep with no false positives," the follow-up read folded in. `importers` is the reverse of
the module import graph (`import a` / `from a import x` / relative imports all resolved to one
qualname) — the direction text search does worst.

`--reassignments` dials how a **reassigned local**'s edges surface (see Resolution): `split`
(default) reports the type live at each use (`x = User(); …; x = Admin(); x.save()` → `Admin.save`
at that line), `off` drops those flow-derived edges entirely, `union` adds the may-set widening
(every type the variable takes anywhere). A trimmed result is never silent — when `--min-confidence`
or `--reassignments` hides edges, the query prints `(N hidden: …)` so you know to widen. The MCP
relation tools carry the same `reassignments` argument and a structured `suppressed` count.

A **mistyped id reads differently from a true empty answer**: `callers`/`deps`/`importers` on an
unknown id report `no such symbol: 'X'  (did you mean: …?)` and exit `2`, while a real symbol that
genuinely has none says `no callers of 'X'` and exits `1` — so an empty result is never a silent
typo. The MCP relation tools return the same distinction structurally (`{found, results, suggestions}`).

`outline` is shallow by default — a module's own definitions, with child submodules as
collapsed one-line leaves (a package table-of-contents). `--focus <symbol>` (an id under the
module) expands just the submodule containing it, revealing that symbol's neighbourhood while
the rest stay collapsed; an id not under the module is ignored, so you get the plain outline.

The index persists at `<root>/.claude-ast/index.db` (self-ignoring;
`CLAUDE_AST_CACHE_DIR` relocates it centrally).

## MCP server

```console
claude-ast-mcp [path]                # serve the index to Claude Code over stdio
```

A FastMCP stdio server — one long-lived process per project — exposing the read-only
queries above as tools (`find_definition`, `outline`, `find_callers`, `find_dependencies`,
`repo_map`), returning structured JSON with `min_confidence` on the relation tools. A
background `watchfiles` thread patches the held index on `.py` edits and atomically swaps
it in, so a query is never stale. Diagnostics go to stderr; stdout carries the protocol.

## Development

```console
uv sync
uv run pytest
uv run ruff check
uv run pyright
```

See `CLAUDE.md` for project conventions and `tests/README.md` for the test
architecture. The frozen stdlib stub table is regenerated with
`uv run python tools/python/gen_stubs.py` (`check` gates freshness in CI).

## Deferred

Landed features are above; these are the known gaps, kept out of scope on purpose:

- **P2 resolvers** — an environment-aware provider for *third-party* stubs (`django-stubs`
  et al.; bounded ROI, since their hardest types are mypy-plugin-computed and absent from
  `.pyi`); call-site observations for external and method/constructor callees. *(Decorator
  tracking has landed: `@property`/`@cached_property` are a distinct non-callable, chain-threadable
  `property` kind, and `@staticmethod` is flagged so its `self` is not read as the instance.
  Resolving a **custom** in-tree property decorator from its own definition — does it produce a
  `__get__` descriptor? — is a clean resolve-time refinement left for when a codebase needs it.)*
- **Framework-convention rungs** — Django-aware resolution the generic ladder can't see: the
  manager convention (`Model.objects…`), celery task attributes (`fn.delay(…)` → the task
  function), router `register(…)` / `as_view()` targets. Convention-based, so `possible` at
  best — never dressed up as definite.
- **importers scope** — function-scoped imports are excluded from the module graph by design
  (not a module-wide dependency), but they are real dependencies for impact analysis; add an
  opt-in flag that includes them, flagged as function-local.
- **repo-map ranking** — test files reference everything, so they float to the top and crowd
  the budget; down-rank test code in the ranker.
- **id scheme** — the structured module/member id redesign: symbol ids are flat opaque
  dotted qualnames (disambiguated with `#N`) today; a structured, per-axis-unique scheme
  is a future refinement, not a current correctness gap.
- **P3 refinements** — persisting live-session edits back to the snapshot; **last-good-parse
  retention** (a mid-edit syntax error drops the file from the served view today — keep the
  previous good parse, marked stale, until the file parses again). *(Incremental resolve on
  patch and a swap-invalidated rank cache have landed — a save now re-resolves only the
  reverse-import + heuristic-name closure of the change, and repo_map memoizes per graph.)*
- **Second language** — a JS/TS backend. The seam is already built for it: `ast` is confined
  to `ingest/python/`, external ids are backend-owned, and tooling is partitioned under
  `tools/<language>/`.
