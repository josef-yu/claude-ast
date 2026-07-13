# Task — Django ticket #37057

You are planning a change to the Django codebase checked out at the path you are given.
**Produce an implementation plan. Do NOT write the code.**

## The bug

A `UniqueConstraint` with a `condition` that can evaluate to SQL `UNKNOWN` (i.e. the condition
involves `NULL`) is validated incorrectly by Django's Python-side constraint validation.

Background: a while ago, `CheckConstraint` validation was adjusted so that a condition resolving
to `UNKNOWN` is treated as *satisfied* — this matches how databases enforce CHECK constraints
(they do not fail rows whose CHECK evaluates to UNKNOWN). That adjustment was made in the shared
machinery all constraints use to evaluate a condition in Python. But this behavior is **wrong for
`UniqueConstraint`**: a UNIQUE constraint does not ignore UNKNOWN conditions the way a CHECK does.

Fix constraint validation so that `UniqueConstraint` handles `UNKNOWN` conditions per its own
semantics, without regressing `CheckConstraint`.

## Deliverable

1. A concise **implementation plan**: what you would change and why, including *where* the
   UNKNOWN handling should live and why it should not stay where it is.
2. A structured **touch-set**: files + specific symbols you would add / modify / move, one per
   line as `path::symbol — add|modify|move — one-line reason`.

Keep it grounded: only name locations you have actually verified exist in this checkout.
