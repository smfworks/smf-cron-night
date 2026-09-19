"""Window filter + status/cost normalization for SMF Cron Night."""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dashboard"))

import plugin_api as api


TZ = ZoneInfo("America/Los_Angeles")


def _dt(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=TZ)


def test_overnight_window_after_morning():
    now = _dt("2026-09-19T15:30:00")
    start, end = api.overnight_window(now, TZ)
    assert start == _dt("2026-09-18T18:00:00")
    assert end == _dt("2026-09-19T08:00:00")


def test_overnight_window_before_morning_is_still_last_night():
    now = _dt("2026-09-19T03:15:00")
    start, end = api.overnight_window(now, TZ)
    assert start == _dt("2026-09-18T18:00:00")
    assert end == _dt("2026-09-19T08:00:00")


def test_overnight_window_at_exactly_0800_is_closed_night():
    now = _dt("2026-09-19T08:00:00")
    start, end = api.overnight_window(now, TZ)
    assert start == _dt("2026-09-18T18:00:00")
    assert end == _dt("2026-09-19T08:00:00")


def test_filter_runs_keeps_only_overnight_attempts():
    start, end = api.overnight_window(_dt("2026-09-19T10:00:00"), TZ)
    runs = [
        {"job_id": "in", "started_at": "2026-09-18T22:05:00-07:00", "status": "completed"},
        {"job_id": "early", "started_at": "2026-09-18T17:59:00-07:00", "status": "completed"},
        {"job_id": "late", "started_at": "2026-09-19T08:00:00-07:00", "status": "failed"},
        {"job_id": "morning", "started_at": "2026-09-19T07:59:00-07:00", "status": "failed"},
    ]
    kept = api.filter_runs_in_window(runs, start, end, default_tz=TZ)
    ids = [r["job_id"] for r in kept]
    assert ids == ["morning", "in"]


def test_normalize_status_maps_hermes_literals():
    assert api.normalize_status("ok") == "completed"
    assert api.normalize_status("completed") == "completed"
    assert api.normalize_status("error") == "failed"
    assert api.normalize_status("delivery_failed") == "failed"
    assert api.normalize_status("claimed") == "claimed"
    assert api.normalize_status("running") == "running"
    assert api.normalize_status("unknown") == "unknown"
    assert api.normalize_status("") is None
    assert api.normalize_status(None) is None


def test_normalize_cost_never_invents_usd():
    tokens, usd = api.normalize_cost({
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "total_tokens": 120,
    })
    assert tokens == 120
    assert usd is None


def test_normalize_cost_uses_recorded_actual_usd_only():
    tokens, usd = api.normalize_cost({
        "input_tokens": 10,
        "output_tokens": 5,
        "actual_cost_usd": 0.0123,
        "estimated_cost_usd": 0.04,
    })
    assert tokens == 15
    assert usd == pytest.approx(0.0123)


def test_normalize_cost_skips_unknown_and_included():
    _, usd_unknown = api.normalize_cost({
        "estimated_cost_usd": 1.5,
        "cost_status": "unknown",
    })
    _, usd_included = api.normalize_cost({
        "estimated_cost_usd": 0.0,
        "cost_status": "included",
    })
    assert usd_unknown is None
    assert usd_included is None


def test_normalize_cost_does_not_coerce_missing_to_zero():
    tokens, usd = api.normalize_cost({"job_id": "x"})
    assert tokens is None
    assert usd is None


def test_summarize_runs_keeps_usd_null_when_none_recorded():
    summary = api.summarize_runs([
        {"status": "completed", "tokens": 10, "usd": None},
        {"status": "failed", "tokens": 3, "usd": None},
    ])
    assert summary["runs"] == 2
    assert summary["failed"] == 1
    assert summary["tokens"] == 13
    assert summary["usd"] is None


def test_summarize_runs_sums_only_recorded_usd():
    summary = api.summarize_runs([
        {"status": "completed", "tokens": 10, "usd": 0.02},
        {"status": "completed", "tokens": None, "usd": None},
    ])
    assert summary["usd"] == pytest.approx(0.02)
    assert summary["tokens"] == 10


def test_load_jobs_accepts_canonical_and_bare_list(tmp_path: Path):
    canonical = tmp_path / "jobs.json"
    canonical.write_text(json.dumps({"jobs": [{"id": "abc", "name": "Briefing"}]}), encoding="utf-8")
    assert api.load_jobs_file(canonical)[0]["id"] == "abc"

    bare = tmp_path / "bare.json"
    bare.write_text(json.dumps([{"id": "def", "name": "Ping"}]), encoding="utf-8")
    assert api.load_jobs_file(bare)[0]["id"] == "def"


def test_collect_and_filter_merges_execution_ledger_with_audit(tmp_path: Path):
    cron = tmp_path / "cron"
    cron.mkdir()
    (tmp_path / "config.yaml").write_text("profile: test\n", encoding="utf-8")
    (cron / "jobs.json").write_text(
        json.dumps({
            "jobs": [{
                "id": "a1b2c3d4e5f6",
                "name": "Daily briefing",
                "schedule": {"kind": "cron", "expr": "0 2 * * *", "display": "0 2 * * *"},
                "schedule_display": "0 2 * * *",
                "last_run_at": "2026-09-18T21:00:00-07:00",
                "last_status": "ok",
            }]
        }),
        encoding="utf-8",
    )
    db = cron / "executions.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE executions (
            id TEXT PRIMARY KEY, job_id TEXT, source TEXT, process_id TEXT,
            pid INTEGER, status TEXT, claimed_at TEXT, started_at TEXT,
            finished_at TEXT, error TEXT
        )"""
    )
    conn.execute(
        """INSERT INTO executions VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            "exe1", "a1b2c3d4e5f6", "builtin", "p", 1, "completed",
            "2026-09-18T21:00:00-07:00", "2026-09-18T21:00:01-07:00",
            "2026-09-18T21:00:40-07:00", None,
        ),
    )
    conn.execute(
        """INSERT INTO executions VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            "exe2", "a1b2c3d4e5f6", "builtin", "p", 1, "failed",
            "2026-09-18T23:10:00-07:00", "2026-09-18T23:10:01-07:00",
            "2026-09-18T23:10:05-07:00", "TimeoutError: provider hung",
        ),
    )
    conn.commit()
    conn.close()
    (cron / "usage_audit.jsonl").write_text(
        json.dumps({
            "ts": "2026-09-19T04:00:40.100Z",
            "job_id": "a1b2c3d4e5f6",
            "fire_id": "fire-ok",
            "prompt_tokens": 80,
            "completion_tokens": 20,
            "total_tokens": 100,
            "error": None,
        })
        + "\n"
        + json.dumps({
            "ts": "2026-09-19T06:10:05.000Z",
            "job_id": "a1b2c3d4e5f6",
            "fire_id": "fire-bad",
            "prompt_tokens": None,
            "completion_tokens": None,
            "total_tokens": None,
            "error": "TimeoutError: provider hung",
        })
        + "\n",
        encoding="utf-8",
    )

    start, end = api.overnight_window(_dt("2026-09-19T10:00:00"), TZ)
    runs = api.collect_home_runs(tmp_path, default_tz=TZ, profile="default")
    windowed = api.filter_runs_in_window(runs, start, end, default_tz=TZ)
    assert len(windowed) == 2
    failed = next(r for r in windowed if r["status"] == "failed")
    ok = next(r for r in windowed if r["status"] == "completed")
    assert failed["error"] and "TimeoutError" in failed["error"]
    assert failed["usd"] is None
    assert ok["tokens"] == 100
    assert ok["usd"] is None
    assert ok["name"] == "Daily briefing"
    assert ok["schedule"] == "0 2 * * *"
    summary = api.summarize_runs(windowed)
    assert summary["runs"] == 2
    assert summary["failed"] == 1
    assert summary["usd"] is None


def test_session_actual_cost_is_used_when_present(tmp_path: Path):
    (tmp_path / "config.yaml").write_text("x: 1\n", encoding="utf-8")
    cron = tmp_path / "cron"
    cron.mkdir()
    (cron / "jobs.json").write_text(
        json.dumps({"jobs": [{"id": "abc123abc123", "name": "Ping", "schedule_display": "every 1h"}]}),
        encoding="utf-8",
    )
    state = tmp_path / "state.db"
    conn = sqlite3.connect(state)
    conn.execute(
        """CREATE TABLE sessions (
            id TEXT PRIMARY KEY, source TEXT, title TEXT,
            started_at REAL, ended_at REAL, end_reason TEXT,
            input_tokens INTEGER, output_tokens INTEGER,
            actual_cost_usd REAL, estimated_cost_usd REAL, cost_status TEXT
        )"""
    )
    started = datetime(2026, 9, 19, 2, 0, tzinfo=TZ).timestamp()
    ended = datetime(2026, 9, 19, 2, 1, tzinfo=TZ).timestamp()
    conn.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "cron_abc123abc123_2026-09-19_02-00-00", "cron", "Ping",
            started, ended, None, 40, 10, 0.004, 0.01, "estimated",
        ),
    )
    conn.commit()
    conn.close()
    start, end = api.overnight_window(_dt("2026-09-19T10:00:00"), TZ)
    runs = api.filter_runs_in_window(
        api.collect_home_runs(tmp_path, default_tz=TZ, profile="default"),
        start, end, default_tz=TZ,
    )
    assert len(runs) == 1
    assert runs[0]["tokens"] == 50
    assert runs[0]["usd"] == pytest.approx(0.004)
    assert runs[0]["status"] == "completed"


def test_error_snippet_truncates():
    snippet = api.error_snippet("x" * 500)
    assert snippet is not None
    assert len(snippet) == 240
    assert snippet.endswith("…")


def test_parse_output_filename_timestamp():
    dt = api.parse_datetime("2026-09-18_23-15-02", default_tz=TZ)
    assert dt is not None
    assert dt.hour == 23
    assert dt.minute == 15
    assert dt.tzinfo == TZ
