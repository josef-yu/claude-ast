# Task — Django ticket #37016

You are planning a change to the Django codebase checked out at the path you are given.
**Produce an implementation plan. Do NOT write the code.**

## The bug

`When()` (used to build `Case()` expressions) accepts keyword lookups and turns them into a
`Q()` internally. But `Q()` and `filter()` reject a couple of *reserved* keyword arguments —
`_connector` and `_negated` — with a clear `TypeError`, because those are internal to how `Q`
objects are constructed. `When()` does **not**: `When(_connector="OR", then=...)` silently
passes them through to `Q()`, producing confusing behavior instead of a clear error.

Make `When()` reject these reserved kwargs the same way `Q()` / `filter()` already do.

## Deliverable

1. A concise **implementation plan**: what you would change and why, including any constraint
   that dictates *where* the change must live.
2. A structured **touch-set**: the list of files and the specific symbols (functions / classes /
   module-level names) you would add, modify, or move — one per line as
   `path::symbol — add|modify|move — one-line reason`.

Keep it grounded: only name locations you have actually verified exist in this checkout.
