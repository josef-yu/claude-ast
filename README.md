# claude-ast

AST-backed code navigation for Claude Code — precise, structural answers over a
real syntax tree, with **graded confidence** for a dynamic language.

Claude Code navigates code by grepping text. Text can't tell a definition from a
call, follow an import, or name the twelve callers of a function. `claude-ast`
answers those as precise queries over a Python AST index — and, where a dynamic
language makes an answer uncertain, it tells you *how sure it is* rather than
guessing.

## Status

Early build. The design has converged; implementation is engine-first
(index + resolvers + queries, driven via the CLI) with the MCP server exposed
last, once the engine is validated on real repos.

Scope (v1): **Python**, one language deep. Three query families over one shared
index — symbol lookup, structural relationships, and orientation (repo map).

## Architecture at a glance

```
ingest (ast)  →  resolve (registry)  →  store (sqlite + in-mem graph)  →  query (+ ranker)  →  server (mcp)
                                              ↑
                                           watch (watchfiles)  — feeds changed files back in
```

- **Deterministic & local** — no LLM calls, no external service, no API cost.
- **Own the whole stack** — our own parser (`ast`) and resolver stack; external
  engines (Pyright/Serena/SCIP) can slot in later as optional resolvers.
- **Report, don't rule** — every edge carries a `Resolution(source, confidence)`;
  queries surface `definite` vs `possible` tiers instead of faking precision.

## Development

```sh
uv sync
uv run pytest
uv run ruff check
```
