"""Watcher — watchfiles-driven dirty-set.

Live edits are marked dirty in real time; queries lazily reparse dirty files
before answering, so a query is never stale even in the edit -> query tightloop.
Ambient by design: the model never triggers watching; authority is the launch
context. Scope-filtered (.gitignore, skip .venv/__pycache__/.git, ``.py`` only).  [P1]
"""
