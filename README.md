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

The **P1 engine is complete** and validated on the Django source tree (2,873
files / ~60k symbols indexed in seconds; warm re-index ~0.7s). It's built
engine-first and driven via the CLI; the MCP server (P3) and the type-resolver
stack (P2) are next. Nothing here calls an LLM — it's fully deterministic and
local.

## What it does

One shared AST index, three query families:

- **Lookup** — `find_definition`, `outline`
- **Relationships** — `find_callers`, `find_references`, `find_dependencies` (each result tiered `definite` / `possible`)
- **Orientation** — `repo_map` (a ranked, token-budgeted skeleton)

## Architecture

```
ingest → resolve (per backend) → store (sqlite + in-mem graph) → query (+ ranker) → server (mcp)
                                        ↑
                                     watch — feeds changed files back in  [P3]
```

- **`model/`** — the normalized contract (`Symbol` / `Edge` / `Resolution{source, confidence}`) every layer speaks.
- **`ingest/`** — language backends behind an `Indexer` protocol; `ingest/python/` is the one backend (all `ast` lives there). Produces symbols + syntactic edges.
- **`store/`** — SQLite snapshot for warm restart + per-file incremental, behind a `Store` protocol.
- **`query/`** — pure functions over the graph: lookups, relationships, and `repo_map` (confidence-weighted PageRank).
- **`index.py`** — the `Index` orchestrator (the facade the CLI and later the MCP server use).

Design principles: **own the whole stack** (our own parser + resolvers; external
engines can slot in later), **deterministic & local** (no LLM, no external
service, no API cost), and **report, don't rule** — every edge carries an honest
confidence tier, so `definite` really means definite.

## CLI

```console
claude-ast index <path>              # build/update the index; print a summary
claude-ast status <path>             # index freshness (cold vs. warm snapshot)
claude-ast def <name> [path]         # where a name is defined
claude-ast outline <module> [path]   # a module's symbols
claude-ast callers <symbol> [path] [--min-confidence high|medium|low]   # who calls a symbol
claude-ast deps <symbol> [path] [--min-confidence high|medium|low]      # what a symbol uses
claude-ast repo-map [path] [--focus <id>] [--budget N]
```

`callers` / `deps` take `--min-confidence` (default `medium`): the consumer's dial from
the reliable set (definite + typed guesses) down to the `low` name-match heuristics —
fetched only when the recall is worth the noise. The engine always *reports* every edge
at honest confidence; the caller decides how much to pull. This is the knob the P3 MCP
tools will expose so the model can widen its own view on demand.

The index persists at `<root>/.claude-ast/index.db` (self-ignoring;
`CLAUDE_AST_CACHE_DIR` relocates it centrally).

## Development

```console
uv sync
uv run pytest
uv run ruff check
uv run pyright
```

See `CLAUDE.md` for project conventions and `tests/README.md` for the test
architecture.

## Roadmap

- **Landed since P1:** the **external-reference boundary** — library/stdlib targets surface as `external` nodes on `find_dependencies`, as `definite` edges kept out of ranking (deterministic — import text only). Covers from-import calls, external base classes, and **module-rooted attribute calls** (`os.path.join()`, dotted bases like `abc.ABC`); the external-id scheme is backend-owned, so a JS/TS backend can encode richer coordinates. The lean id-scheme fixes (`#N` disambiguation, single id-assignment authority, no neutral id-parsing) are in; the structured module/member id redesign stays deferred past P2.
- **P2 (in progress):** the value-typed resolver stack — the `possible`-tier edges that make "report, don't rule" earn its keep. **Landed:** `self.m()` → the enclosing class's member (+ cross-file inherited); annotation-typed receivers (`u: User` → `User.save`); local **construction inference** (`x = User(); x.save()`); **relative-import resolution** (`from ..model import X`); and **package re-export resolution** (`from pkg import X` follows `pkg/__init__` to the real defining module). All value-typed edges are `MEDIUM`/possible. a **builtins** pass (`len` / `Exception` → `definite` external edges); and a capped name-match **heuristic** (`LOW`) for untyped receivers — completing the confidence ladder (definite → medium → low). Plus a **call-site type-observation reporter** — a *definite* `RECEIVES_ARG` edge for the concrete type seen flowing into a parameter (`g(User())` → `g` receives `User`). This reports *what was passed*, not *what a call dispatches to*, so unlike the receiver resolvers it is honestly definite (open-world subclassing can't retract an observation) — the first non-syntactic `definite` edge "report, don't rule" actually permits. Lint-grade and one-hop (constructions only, bare-name functions only, no forward propagation); external-type and method/constructor callees are deferred, so it fires only where in-tree classes flow into in-tree functions. All measured by **resolution metrics**: `claude-ast index` reports coverage + the tier/source split (its own `src/`: **~69% of refs bound**, 365 definite / 33 possible), the loop that drove the builtins win (44% → 68%). **Note:** the originally-planned *confidence merge* (escalate a corroborated edge to `definite`) was dropped as a "report, don't rule" violation — method dispatch is never definite, so the definiteness belongs on the observation, never on the derived dispatch edge. Plus **stdlib stub resolution** — a receiver typed by an *external* stdlib type (`p: Path; p.exists()`) resolves to a `MEDIUM` STUB edge on an external member node, behind a `StubProvider` seam. The member table is **generated-then-frozen**: `tools/python/gen_stubs.py` introspects the *intersection* of callable members across the supported Python range (3.12–3.14) into a committed literal, so the index-time lookup is pure and hermetic (no site-packages, no interpreter drift) — all impurity quarantined in the offline generator. The seam is shaped (`member(type, attr) -> bool`) so an environment-aware provider for third-party stubs (`django-stubs`) can slot in later without touching the resolver; it needs no cache fingerprint because resolution is assembly-time and self-corrects. Two guards detect a stale table (a spec fingerprint + a per-interpreter soundness check; `tools/python/gen_stubs.py check` is the authoritative matrix gate — the generator is one self-orchestrating command, no Make/shell glue). Real recall: `src` 69→**73.8%**, Django +274 edges. **Next:** the env-aware third-party-stub provider (bounded ROI — `django-stubs`' hard types are mypy-plugin-computed, not in `.pyi`); and extending call-site observations to external/method callees.
- **P3:** the MCP server (stdio, per-project) + the live filesystem watcher. **Landed early:** the **stderr logging seam** (`log.configure()`) — diagnostics (unreadable / unparseable files, with the reason) route to stderr, keeping stdout a clean data/protocol channel; a skipped file is now visible on *every* command instead of being silently dropped by all but `index`.
