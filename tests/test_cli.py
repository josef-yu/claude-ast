"""CLI tests — the display paths that aren't covered by the query/backend suites.

Uses a tiny tmp project and an isolated cache dir, so `main()` (which persists a snapshot)
never writes into the repo.
"""

from claude_ast.cli import main


def test_callers_source_flag_shows_the_call_line(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("CLAUDE_AST_CACHE_DIR", str(tmp_path / "cache"))
    (tmp_path / "m.py").write_text("def hub():\n    return 1\n\n\ndef start():\n    return hub()\n")

    rc = main(["callers", "m.hub", str(tmp_path), "--source"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "m.start" in out            # the resolved caller
    assert "return hub()" in out       # ...with its source line inline


def test_callers_without_source_is_terse(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("CLAUDE_AST_CACHE_DIR", str(tmp_path / "cache"))
    (tmp_path / "m.py").write_text("def hub():\n    return 1\n\n\ndef start():\n    return hub()\n")

    main(["callers", "m.hub", str(tmp_path)])
    assert "return hub()" not in capsys.readouterr().out
