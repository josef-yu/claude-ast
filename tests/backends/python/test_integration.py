"""Python backend — end-to-end (parse -> assemble -> query) + warm start.

Whole-pipeline mechanics: real source flows through the backend into a queryable
Index, byte-level parse handling (BOM), and the snapshot warm-start round-trip.
Per-mechanism semantics live in test_symbols / test_edges / test_resolvers.
"""

from claude_ast.index import Index

_SRC = '''\
CONST = 3


def authenticate(email, pw):
    ...


class User:
    def save(self):
        ...
'''


def test_end_to_end_python_source_is_queryable(tmp_path):
    # Real Python source flows through parse -> assemble -> query. The query logic
    # itself is proved neutrally in tests/test_query.py.
    (tmp_path / "auth.py").write_text(_SRC)
    index = Index.build(tmp_path)

    assert [d.id for d in index.find_definition("auth.User")] == ["auth.User"]
    outline_ids = {e.id for e in index.outline("auth")}
    assert {"auth", "auth.authenticate", "auth.User", "auth.User.save"} <= outline_ids


def test_bom_prefixed_source_is_still_parsed(tmp_path):
    (tmp_path / "m.py").write_bytes(b"\xef\xbb\xbfdef f():\n    ...\n")  # UTF-8 BOM
    index = Index.build(tmp_path)
    assert [d.id for d in index.find_definition("m.f")] == ["m.f"]


def test_warm_start_preserves_results_and_writes_a_snapshot(tmp_path):
    (tmp_path / "m.py").write_text("def helper():\n    ...\n\n\ndef use():\n    helper()\n")

    first = Index.build(tmp_path)  # cold — parses + writes snapshot
    assert (tmp_path / ".claude-ast" / "index.db").exists()

    second = Index.build(tmp_path)  # warm — reuses the snapshot
    cold_callers = {r.id for r in first.find_callers("m.helper")}
    warm_callers = {r.id for r in second.find_callers("m.helper")}
    assert cold_callers == warm_callers == {"m.use"}  # identical across cold/warm
