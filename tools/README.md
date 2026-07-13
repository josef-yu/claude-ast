# tools/

Developer utilities — build-time tooling, **not** part of the shipped package
(they need multiple interpreters / write into the source tree, so they never ship).

Language-specific tooling is partitioned by language under `tools/<language>/`,
mirroring the backend seam in `src/claude_ast/ingest/<language>/`. So the Python
backend's stub-table generator is `tools/python/gen_stubs.py`; a future JS/TS
backend's equivalent (e.g. generating from DefinitelyTyped / `.d.ts`) would live
under `tools/typescript/`. Any language-neutral tool sits at the `tools/` root.

## tools/python/

- **`gen_stubs.py`** — generates the frozen stdlib stub table
  (`src/claude_ast/ingest/python/_stub_table.py`) consumed by `ingest/python/stubs.py`.
  One self-orchestrating command (no Make/shell glue), reading the supported version
  matrix from `stubs.SUPPORTED_VERSIONS`:

  ```sh
  uv run python tools/python/gen_stubs.py          # regenerate the committed table
  uv run python tools/python/gen_stubs.py check    # CI gate: fail if the table is stale
  ```
