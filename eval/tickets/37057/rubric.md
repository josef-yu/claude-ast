# Rubric — #37057 (GRADING KEY — not shown to the agents)

Ground truth: commit `61a62be313` ("Fixed #37057 -- Adjusted UniqueConstraint handling of UNKNOWN
condition"). Parent (agent's checkout): `63c56cda13`.

## Real touch-set (source only)

- `django/db/models/query_utils.py` :: `Q.check` — **modify (remove)**: strip the
  `Coalesce(condition, True, output_field=BooleanField())` UNKNOWN-wrapping out of the shared
  method (it was over-applied to every caller).
- `django/db/models/constraints.py` :: `CheckConstraint.validate` — **modify (add)**: apply the
  `Coalesce(condition, True, output_field=BooleanField())` here instead, gated on
  `connections[using].features.supports_comparing_boolean_expr`, so only CHECK constraints get
  the UNKNOWN-as-satisfied behavior.

## The crux

`Q.check()` is a **shared** method used by multiple constraint types (both `CheckConstraint` and
`UniqueConstraint` reach it during Python-side validation). The `Coalesce`/UNKNOWN-as-satisfied
wrapping was added to `Q.check` for `CheckConstraint`, but it silently changes `UniqueConstraint`
too. The correct fix **moves the wrapping out of the shared `Q.check` and into the specific
caller `CheckConstraint.validate`** — "how UNKNOWN should be treated depends on the caller's
context." `UniqueConstraint` then simply does not apply it.

- **Full credit:** plan recognizes `Q.check` is shared, that the fix belongs in the *caller*
  (`CheckConstraint.validate`), and that `UniqueConstraint` must not apply the Coalesce.
- **Partial:** patches `Q.check` to special-case the constraint type (works but wrong layering),
  or fixes only `UniqueConstraint` without moving the Coalesce out of the shared method.
- **Miss:** doesn't identify `Q.check` as the shared culprit, or proposes changing behavior in a
  place that would regress `CheckConstraint`.

## Scoring anchors

- Touch-set recall: named `Q.check` (remove) **and** `CheckConstraint.validate` (add)?
- Faithfulness: did it verify (not guess) that both constraint types route through `Q.check`?
- Approach: awareness of the `supports_comparing_boolean_expr` feature gate is a bonus, not required.
