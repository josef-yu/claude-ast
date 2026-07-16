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


def test_unknown_symbol_is_distinct_from_no_results(tmp_path, capsys, monkeypatch):
    # A mistyped id and a real-but-uncalled symbol must NOT read the same: unknown -> exit 2 with a
    # 'no such symbol' + near-miss; a known symbol with no callers -> exit 1 with 'no callers'.
    monkeypatch.setenv("CLAUDE_AST_CACHE_DIR", str(tmp_path / "cache"))
    (tmp_path / "m.py").write_text("def hub():\n    return 1\n")  # defined, never called

    rc_unknown = main(["callers", "m.hubb", str(tmp_path)])  # typo
    err = capsys.readouterr().err
    assert rc_unknown == 2
    assert "no such symbol" in err and "m.hub" in err  # near-miss points at the real id

    rc_empty = main(["callers", "m.hub", str(tmp_path)])  # real symbol, genuinely no callers
    err = capsys.readouterr().err
    assert rc_empty == 1
    assert "no callers of" in err and "no such symbol" not in err


def test_unknown_module_outline_reports_a_near_miss(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("CLAUDE_AST_CACHE_DIR", str(tmp_path / "cache"))
    (tmp_path / "widgets.py").write_text("def build():\n    return 1\n")

    rc = main(["outline", "widget", str(tmp_path)])  # typo: widget vs widgets
    err = capsys.readouterr().err
    assert rc == 2 and "no such module" in err and "widgets" in err
