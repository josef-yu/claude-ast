# Task — Django ticket #20024

You are planning a change to the Django codebase checked out at the path you are given.
**Produce an implementation plan. Do NOT write the code.**

## The bug

`exclude()` with an `__in` lookup whose value list contains `None`, against a **nullable**
column, returns the wrong rows. For example:

```python
Model.objects.exclude(nullable_field__in=[1, 2, None])
```

does not exclude the rows you'd expect — the generated SQL mishandles the `None` inside the
`IN` list when the whole condition is negated by `exclude()` and the column is nullable.
`filter(...__in=[1, 2, None])` and the `exclude` case need consistent, correct NULL handling.

Fix the handling of `__in` lookups that contain `None` inside `exclude()`.

## Deliverable

1. A concise **implementation plan**: what you would change and why — including *where* in the
   query-building machinery the NULL-companion logic for a nullable column is decided.
2. A structured **touch-set**: files + specific symbols you would add / modify, one per line as
   `path::symbol — add|modify — one-line reason`.

Keep it grounded: only name locations you have actually verified exist in this checkout.
