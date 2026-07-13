# Rubric — #37016 (GRADING KEY — not shown to the agents)

Ground truth: commit `3b161e6096` ("Fixed #37016 -- Avoided propagating invalid arguments from
When() to Q()"). Parent (agent's checkout): `123fa3a3f3`.

## Real touch-set (source only)

- `django/db/models/expressions.py` :: `When.__init__` — **modify**: reject reserved lookups —
  `if invalid := PROHIBITED_FILTER_KWARGS.intersection(lookups): raise TypeError(...)`.
- `django/db/models/query_utils.py` :: `PROHIBITED_FILTER_KWARGS` — **add (move here)**: the
  `frozenset({"_connector", "_negated"})` constant is defined here.
- `django/db/models/query.py` :: `PROHIBITED_FILTER_KWARGS` — **move/remove**: delete the local
  definition; import the constant from `query_utils`.

## The crux (this is what separates a good plan from a plausible-but-wrong one)

The reserved-kwarg constant currently lives in **`query.py`**. But `When` lives in
**`expressions.py`**, which **cannot import from `query.py`** without a circular import
(`query` imports from `expressions`, not the reverse). So the constant must be **relocated to
`query_utils.py`** — a lower-level module both `query.py` and `expressions.py` already import —
and imported from there in both places.

- **Full credit:** plan identifies the circular-import constraint and the move to `query_utils`.
- **Partial:** adds the check in `When` but proposes reusing `query.py`'s constant directly
  (would circular-import) or duplicating the constant.
- **Miss:** wrong location for the check, or doesn't recognize the shared-constant issue.

## Scoring anchors

- Touch-set recall: did it name `expressions.py:When.__init__` (the check) **and** the
  `query_utils.py` relocation?
- Faithfulness: are the import relationships it cites real (expressions ↛ query)?
- Approach: consistency with `Q`/`filter` validation (raise `TypeError` naming the bad kwargs).
