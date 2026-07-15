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


# A small project spanning every resolver pass: imports + inheritance (syntactic), an annotated
# receiver and a `self` call (annotation / inference), a constructed local and a call-return chain
# (inference), a call-site construction (observed/definite), and an external + builtin call.
_PKG = {
    "pkg/__init__.py": "",
    "pkg/models.py": (
        "class Base:\n"
        "    def save(self):\n        ...\n\n"
        "class User(Base):\n"
        "    def greet(self) -> str:\n        return _mk()\n\n"
        "def _mk() -> str:\n    return 'hi'\n"
    ),
    "pkg/service.py": (
        "import os\n"
        "from pkg.models import Base, User\n\n"
        "def make_user() -> User:\n    return User()\n\n"
        "def run(u: User):\n"
        "    u.greet()\n"
        "    u.save()\n"
        "    x = make_user()\n"
        "    x.save()\n"
        "    make_user().greet()\n"
        "    os.path.join('a', 'b')\n"
        "    len(u.greet())\n\n"
        "class Service(Base):\n"
        "    def handle(self, user: User):\n"
        "        user.save()\n"
        "        self.save()\n"
    ),
}


def _canonical(index) -> str:
    """A deterministic, insertion-order dump of the whole assembled graph + coverage metrics —
    the load-bearing warm==cold invariant, compared byte for byte."""
    g = index.graph
    lines = []
    for s in g.symbols():
        sp = s.span
        lines.append(f"S {s.id} {s.name} {s.kind.value} {sp.file}:{sp.line}:{sp.col} "
                     f"{s.signature!r} {s.parent} {s.return_type} {int(s.return_type_inferred)}")
    for s in g.symbols():
        for e in g.out_edges(s.id):
            at = f"{e.at.file}:{e.at.line}:{e.at.col}" if e.at else "-"
            lines.append(f"E {e.src} -> {e.dst} {e.kind.value} "
                         f"{e.resolution.source.value}/{e.resolution.confidence.value} {at}")
    lines += [f"X {x.id}" for x in g.externals()]
    m = index.metrics
    lines.append(f"M {m.total_refs} {m.bound_refs} "
                 f"{sorted(m.by_confidence.items())} {sorted(m.by_source.items())}")
    return "\n".join(lines)


def test_warm_rebuild_is_byte_identical_to_cold(tmp_path):
    # The whole pipeline (parse -> assemble -> every resolver pass -> metrics) must reproduce a
    # byte-identical graph from the snapshot as from a cold parse. Guards the shared-context /
    # single-walk refactors: any divergence in symbols, edges, externals, ordering, confidence,
    # or coverage fails here.
    for rel, src in _PKG.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(src)

    cold = _canonical(Index.build(tmp_path))  # parses + writes snapshot
    assert (tmp_path / ".claude-ast" / "index.db").exists()
    warm = _canonical(Index.build(tmp_path))  # reuses the snapshot
    assert warm == cold
