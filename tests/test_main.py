"""Tests for timestamped curse word counting."""

import json
import sqlite3
from pathlib import Path

import pytest

from src import main
from src.main import (
    SessionLog,
    build_daily_chart,
    build_matcher,
    cached_count,
    count_curses,
    count_daily_curses,
    discover_logs,
    format_duration,
)


def test_format_duration() -> None:
    """Render ETAs with a fixed-width clock format."""
    assert format_duration(3661.4) == "01:01:01"


def test_build_daily_chart_draws_a_color_coded_line_per_source() -> None:
    """Render one color-coded source series across recorded interaction days."""
    chart = build_daily_chart(
        {
            "claude": {"2026-11-01": 4, "2026-11-02": 0, "2026-11-03": 6},
            "gemini": {"2026-11-01": 2, "2026-11-03": 1},
        }
    )
    assert "Curse words per day" in chart
    assert "3 interaction days" in chart
    assert "100.0,300.0 730.0,620.0 1360.0,140.0" in chart
    assert "Claude" in chart
    assert "Gemini" in chart
    assert "#5144d8" in chart
    assert "#d88317" in chart


def test_count_curses(tmp_path: Path) -> None:
    """Count repeated whole words across lines without regard to case."""
    log = tmp_path / "session.jsonl"
    log.write_text("Fuck fuck!\nSHIT, shitake.\n", encoding="utf-8")
    assert count_curses(log, ["fuck", "shit"]) == 3


def test_count_curses_handles_punctuation_variants(tmp_path: Path) -> None:
    """Count a configured punctuation variant only at whole-word boundaries."""
    log = tmp_path / "session.jsonl"
    log.write_text("fuck ass-hat ass-hatx\n", encoding="utf-8")
    assert count_curses(log, ["fuck", "ass-hat"]) == 2


def test_count_curses_prefers_a_longer_overlapping_term(tmp_path: Path) -> None:
    """Count one curse when a phrase contains a shorter configured curse term."""
    log = tmp_path / "session.jsonl"
    log.write_text("ass-hat\n", encoding="utf-8")
    assert count_curses(log, ["ass", "ass-hat"]) == 1


def test_count_daily_curses_uses_capture_timestamps(tmp_path: Path) -> None:
    """Group JSONL curse counts by the UTC date of every capture."""
    log = tmp_path / "session.jsonl"
    log.write_text(
        "\n".join(
            [
                json.dumps({"timestamp": "2026-11-01T23:30:00-02:00", "type": "user", "message": {"role": "user", "content": "fuck"}}),
                json.dumps({"timestamp": "2026-11-02T10:00:00Z", "type": "user", "message": {"role": "user", "content": "shit shitake"}}),
                json.dumps({"type": "session-metadata", "message": "metadata only"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    assert count_daily_curses(SessionLog("claude", log, "jsonl"), build_matcher(["fuck", "shit"])) == {"2026-11-02": 2}


def test_count_daily_curses_records_zero_for_interaction_days_without_curses(tmp_path: Path) -> None:
    """Preserve zero buckets only when the source recorded interactions that day."""
    log = tmp_path / "session.jsonl"
    log.write_text(
        "\n".join(
            [
                json.dumps({"timestamp": "2026-11-01T10:00:00Z", "type": "user", "message": {"role": "user", "content": "hello"}}),
                json.dumps({"timestamp": "2026-11-03T10:00:00Z", "type": "user", "message": {"role": "user", "content": "fuck"}}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    assert count_daily_curses(SessionLog("claude", log, "jsonl"), build_matcher(["fuck"])) == {
        "2026-11-01": 0,
        "2026-11-03": 1,
    }


def test_count_daily_curses_ignores_non_user_captures(tmp_path: Path) -> None:
    """Exclude assistant, tool, and system payloads from the user's curse metric."""
    log = tmp_path / "session.jsonl"
    log.write_text(
        "\n".join(
            [
                json.dumps({"timestamp": "2026-11-01T10:00:00Z", "type": "assistant", "message": "fuck fuck"}),
                json.dumps({"timestamp": "2026-11-01T10:01:00Z", "type": "user", "message": {"role": "user", "content": "fuck"}}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    assert count_daily_curses(SessionLog("claude", log, "jsonl"), build_matcher(["fuck"])) == {"2026-11-01": 1}


def test_codex_daily_counts_use_capture_timestamps(tmp_path: Path) -> None:
    """Read Codex captures from its timestamped SQLite transcript table."""
    log = tmp_path / "thread_history_1.sqlite"
    with sqlite3.connect(log) as connection:
        connection.execute("CREATE TABLE thread_realtime_items (created_at_ms INTEGER NOT NULL, item_json TEXT NOT NULL)")
        connection.execute("INSERT INTO thread_realtime_items VALUES (?, ?)", (1762041600000, '{"message": "fuck"}'))
    assert count_daily_curses(SessionLog("codex", log, "sqlite"), build_matcher(["fuck"])) == {"2025-11-02": 1}


def test_cache_uses_a_file_named_after_the_analyzed_log(tmp_path: Path) -> None:
    """Persist one finished log's buckets in its own named JSON cache file."""
    path = tmp_path / "session.jsonl"
    cache = tmp_path / "cache"
    path.write_text(
        '{"timestamp": "2026-11-02T10:00:00Z", "type": "user", "message": {"role": "user", "content": "fuck"}}\n', encoding="utf-8"
    )
    log = SessionLog("claude", path, "jsonl")
    assert cached_count(log, ["fuck"], cache) == {"2026-11-02": 1}
    entry = main.cache_entry(log, cache)
    assert entry.name == "session.jsonl.json"
    assert json.loads(entry.read_text(encoding="utf-8")) == {
        "daily_counts": {"2026-11-02": 1},
        "format_version": main.Settings.CACHE_FORMAT_VERSION,
        "source_size": path.stat().st_size,
    }


def test_cache_reprocesses_logs_when_their_size_changes(tmp_path: Path) -> None:
    """Reuse unchanged files and replace buckets after a source-size change."""
    path = tmp_path / "session.jsonl"
    cache = tmp_path / "cache"
    path.write_text(
        '{"timestamp": "2026-11-02T10:00:00Z", "type": "user", "message": {"role": "user", "content": "fuck"}}\n', encoding="utf-8"
    )
    log = SessionLog("claude", path, "jsonl")
    assert cached_count(log, ["fuck"], cache) == {"2026-11-02": 1}
    path.write_text(
        '{"timestamp": "2026-11-02T10:00:00Z", "type": "user", "message": {"role": "user", "content": "darn"}}\n', encoding="utf-8"
    )
    assert cached_count(log, ["fuck"], cache) == {"2026-11-02": 1}
    path.write_text(
        '{"timestamp": "2026-11-02T10:00:00Z", "type": "user", "message": {"role": "user", "content": "fuck fuck"}}\n', encoding="utf-8"
    )
    assert cached_count(log, ["fuck"], cache) == {"2026-11-02": 2}


def test_discover_logs_supports_all_clients(tmp_path: Path) -> None:
    """Discover one primary transcript format for each supported client."""
    (tmp_path / ".claude" / "projects" / "project").mkdir(parents=True)
    (tmp_path / ".claude" / "projects" / "project" / "session.jsonl").touch()
    (tmp_path / ".grok" / "sessions" / "project" / "session").mkdir(parents=True)
    (tmp_path / ".grok" / "sessions" / "project" / "session" / "chat_history.jsonl").touch()
    gemini_logs = tmp_path / ".gemini" / "antigravity" / "brain" / "session" / ".system_generated" / "logs"
    gemini_logs.mkdir(parents=True)
    (gemini_logs / "transcript.jsonl").touch()
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "thread_history_1.sqlite").touch()
    assert [log.source for log in discover_logs(tmp_path)] == ["claude", "codex", "gemini", "grok"]


def test_main_writes_daily_counts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Write daily aggregate output while rendering one in-place provider progress line."""
    projects = tmp_path / ".claude" / "projects" / "project"
    projects.mkdir(parents=True)
    log = projects / "session.jsonl"
    log.write_text(
        '{"timestamp": "2026-11-02T10:00:00Z", "type": "user", "message": {"role": "user", "content": "fuck"}}\n', encoding="utf-8"
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(main, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(main, "OUTPUT", tmp_path / "output" / "daily_counts.json")
    monkeypatch.setattr(main, "CHART", tmp_path / "output" / "daily_counts.svg")
    monkeypatch.setattr(main, "worker_count", lambda: 1)
    main.main()
    captured = capsys.readouterr()
    assert "Total curse words: 1" in captured.out
    assert "[claude] 1 logs, 1 curse words" in captured.out
    assert "Processing 0.0% (0/1) ETA: calculating... | [claude] session.jsonl" in captured.err
    assert captured.err.count("\n") == 1
    assert json.loads(main.OUTPUT.read_text(encoding="utf-8")) == {"2026-11-02": 1}
    assert main.CHART.exists()


def test_main_exits_nonzero_when_no_curses_are_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Keep the command's zero-match failure status after daily aggregation."""
    projects = tmp_path / ".claude" / "projects" / "project"
    projects.mkdir(parents=True)
    (projects / "session.jsonl").write_text(
        '{"timestamp": "2026-11-02T10:00:00Z", "type": "user", "message": {"role": "user", "content": "hello"}}\n', encoding="utf-8"
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(main, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(main, "OUTPUT", tmp_path / "output" / "daily_counts.json")
    monkeypatch.setattr(main, "CHART", tmp_path / "output" / "daily_counts.svg")
    monkeypatch.setattr(main, "worker_count", lambda: 1)
    with pytest.raises(SystemExit) as error:
        main.main()
    assert error.value.code == 1
    assert "Total curse words: 0" in capsys.readouterr().out
