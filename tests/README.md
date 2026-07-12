# Test architecture

Tests mirror the engine's language seam, so coverage scales as backends are added.

**Neutral tests** exercise the language-agnostic components (model, graph, query
logic, ingest orchestration) using **model primitives or a fake backend** as
input — never a real language backend. This proves each component works
regardless of *how* the index was populated.

- `test_model.py` — the normalized model + graph adjacency
- `test_query.py` — query logic over a hand-built `Graph`
- `test_ingest_project.py` — discovery + dispatch; a `FakeBackend` proves routing
  is protocol-driven, not hardcoded to Python

**Backend tests** live in `backends/test_<language>.py` and cover how that
language maps to the model, plus one end-to-end pass (parse → assemble → query)
for that backend.

- `backends/python/` — the Python backend, split by concern (see below)

Adding a language backend = add `backends/test_<language>.py` (promote to a
`backends/<language>/` package once it grows). The neutral tests are untouched —
if any of them needs a real backend to pass, it isn't neutral.

## When a backend file gets crowded

Start each backend as a single `backends/test_<language>.py`. When it grows
unwieldy, promote it to a `backends/<language>/` package and split **by concern**
within the backend:

- `backends/python/test_symbols.py` — definition extraction
- `backends/python/test_edges.py` — reference / edge extraction + binding
- `backends/python/test_resolvers.py` — the type-resolver stack + confidence
- `backends/python/test_integration.py` — end-to-end (parse → assemble → query)

The split is by concern *within* a backend; the neutral-vs-backend rule above is
unchanged. Keep test file basenames unique across the tree (pytest's default
import mode requires it).
