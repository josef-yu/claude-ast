# claude-ast

AST-backed code-navigation engine for Claude Code (an MCP server, built
engine-first). Python, one language deep; deterministic and local — no LLM
calls, no external service. See `README.md` and the design brief for scope and
architecture.

## Conventions

### File organization — split by concern (universal)

Keep every file focused on **one** concern. When a file grows crowded, split it
into a **package** (a directory of focused modules), divided by concern. This
applies everywhere — `src/` and `tests/` alike. Prefer several small,
single-purpose files over one large one.

- A module does one thing; a package groups the modules of one area.
- **Source example:** `ingest/python.py` (the Python backend) → when crowded,
  promote to `ingest/python/` split into `symbols.py`, `edges.py`,
  `resolvers.py`.
- **Test example:** `tests/backends/test_python.py` → when crowded, promote to
  `tests/backends/python/` split by concern. See `tests/README.md`.
- Keep test file basenames unique across the tree (pytest's prepend import mode).

### Imports

Top of the module only. No inline / lazy imports except to break a real
circular-import cycle or defer a genuinely heavy/optional dependency — with a
comment saying why.

### The language seam

All language-specific logic lives behind the `Indexer` protocol
(`ingest/base.py`) in a backend module; nothing outside a backend imports `ast`.
The protocol's methods are `@abstractmethod`, so a real backend **subclasses**
`Indexer` and can't be constructed until it implements the whole contract (test
doubles may still conform structurally). `ingest + resolve` are the per-backend
language layer; `model / graph / query / rank / store / watch` are
language-neutral. The neutral core stays policy-free: it enforces invariants
(e.g. `Graph.collisions()` catches a backend that lets duplicate ids reach the
graph) but never language-specific policy (id disambiguation, declaration
merging) — that lives in the backend's `finalize`. No backend registry until a
real second language lands.

### Tests

Neutral tests (model, graph, query logic, orchestration) use model primitives or
a fake backend — never a real language backend. Backend-specific tests live in
`tests/backends/`. Full detail in `tests/README.md`.

## Toolchain

`uv` for everything. Keep all three green before moving on:

```sh
uv run pytest
uv run ruff check
uv run pyright
```
