# Rubric — #20024 (GRADING KEY — not shown to the agents)

Ground truth: commit `cec10f992b` ("Fixed #20024 -- Fixed handling of __in lookups with None in
exclude()"). Parent (agent's checkout): `3fb37ef411`.

## Real touch-set (source only)

- `django/db/models/sql/query.py` :: `Query.build_filter` — **modify**: in the block that adds an
  `isnull` companion clause for a nullable column, special-case an `in` lookup whose RHS is a
  container (not `str`/`bytes`) containing `None`: add `isnull(col, True)` with **`OR`** instead
  of `isnull(col, False)` with **`AND`**.

Single source file. (The fix also touches an import — `Iterable` from `collections.abc` — and
adjusts tests, but the substantive change is the `build_filter` branch.)

## The crux

This is a **localization + subtle-semantics** case, not a cross-file one. The challenge is:

1. Find the exact spot in `sql/query.py` (~2,600 lines) — inside `Query.build_filter`, the code
   that appends an `IS [NOT] NULL` companion clause when filtering a nullable column (~line 1638
   in the fixed version; near the `lookup_class(col, False), AND` call in the checkout).
2. Reason correctly about three-valued logic: under `exclude()` (negation) on a nullable column,
   an `__in` list containing `None` needs `... OR col IS NULL` rather than `... AND col IS NOT
   NULL`, so rows are excluded/kept correctly.

- **Full credit:** plan pinpoints `build_filter`'s isnull-companion-clause and states the
  `AND IS NOT NULL` → `OR IS NULL` change for the `in`-with-`None`-on-nullable case.
- **Partial:** identifies `build_filter` / the negation+null interaction but is vague on the exact
  clause, or proposes fixing it at the lookup level rather than the companion-clause construction.
- **Miss:** wrong location (e.g. patches the `In` lookup class, or `exclude()`/`add_q` at a higher
  level where the companion clause isn't decided), or hallucinates a file.

## Scoring anchors

- Touch-set recall: named `sql/query.py :: Query.build_filter` (the companion-clause block)?
- Faithfulness: did it actually locate the code (vs guessing a plausible-sounding method)?
- This ticket's expected signal: the tool helps *localize* in a huge file; less cross-file
  advantage than #37016 / #37057 — a useful contrast for whether the harness discriminates.
