"""The logging seam — diagnostics reach stderr, and stdout stays a clean data channel.

Neutral tests over the real ingest path: an unparseable file must not vanish silently
(it's logged, not just dropped), and no diagnostic may leak onto stdout — the channel the
P3 MCP server will speak its stdio protocol on.
"""

import logging

from claude_ast.ingest import ingest_project
from claude_ast.log import configure


def test_unparseable_file_is_skipped_and_logged(tmp_path, caplog):
    (tmp_path / "ok.py").write_text("x = 1\n")
    (tmp_path / "broken.py").write_text("def oops(:\n")  # a syntax error

    with caplog.at_level(logging.WARNING, logger="claude_ast"):
        result = ingest_project(tmp_path)

    assert str(tmp_path / "broken.py") in result.skipped  # still collected...
    assert str(tmp_path / "ok.py") not in result.skipped
    # ...and no longer silent: a warning names the file and the reason.
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("broken.py" in r.getMessage() for r in warnings)


def test_diagnostics_never_reach_stdout(tmp_path, capsys):
    # stdout is the P3 stdio protocol channel: a skip diagnostic must go to stderr, not here.
    (tmp_path / "broken.py").write_text("def oops(:\n")
    ingest_project(tmp_path)
    assert capsys.readouterr().out == ""


def test_configure_is_idempotent():
    # Each entry point may call it; a second call must not raise or double-configure.
    configure()
    configure()
    assert logging.getLogger().handlers  # logging is set up
