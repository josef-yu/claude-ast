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
claude-ast callers <symbol> [path]   # who calls a symbol
claude-ast deps <symbol> [path]      # what a symbol uses
claude-ast repo-map [path] [--focus <id>] [--budget N]
```

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

- **Next:** id-scheme redesign (disambiguating symbol ids — module/member boundary + same-qualname defs). Folds in the **external-reference boundary**: an `external` symbol kind for library/stdlib targets, internal-only ranking, and binding the from-import / external-base cases that today drop silently (deterministic — import text only).
- **P2:** the resolver stack — attribute/method calls, heuristics, call-site tracing → the `possible`-tier edges that make "report, don't rule" earn its keep on dynamic code. Extends external resolution to attribute calls and to `.pyi`/typeshed **stubs** (opt-in, scoped), and adds **resolution metrics** — coverage + confidence/source distribution, derived in-process and asserted in the golden eval.
- **P3:** the MCP server (stdio, per-project) + the live filesystem watcher. Needs a **stderr logging seam** (stdout is the protocol channel), introduced earlier so skipped files stop being silently invisible.
