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
    assert summary["usd_billed"] == 0
    assert summary["cost_coverage"] == "none"
    assert summary["running"] == 0


def test_summarize_runs_omits_usd_total_when_coverage_is_partial():
    summary = api.summarize_runs([
        {"status": "completed", "tokens": 10, "usd": 0.02},
        {"status": "completed", "tokens": None, "usd": None},
    ])
    assert summary["usd"] is None
    assert summary["usd_billed"] == 1
    assert summary["cost_coverage"] == "partial"
    assert summary["tokens"] == 10
    assert summary["runs"] == 2


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
    assert runs[0]["status"] == "unknown"


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


def test_overnight_window_at_exactly_1800_opens_tonight():
    now = _dt("2026-09-19T18:00:00")
    start, end = api.overnight_window(now, TZ)
    assert start == _dt("2026-09-19T18:00:00")
    assert end == _dt("2026-09-20T08:00:00")


def test_overnight_window_at_1830_includes_tonight():
    now = _dt("2026-09-19T18:30:00")
    start, end = api.overnight_window(now, TZ)
    assert start == _dt("2026-09-19T18:00:00")
    assert end == _dt("2026-09-20T08:00:00")
    runs = [
        {"job_id": "tonight", "started_at": "2026-09-19T18:45:00-07:00", "status": "completed"},
        {"job_id": "last_night", "started_at": "2026-09-18T22:00:00-07:00", "status": "completed"},
    ]
    kept = api.filter_runs_in_window(runs, start, end, default_tz=TZ)
    assert [r["job_id"] for r in kept] == ["tonight"]


def test_overnight_window_at_2300_includes_tonight():
    now = _dt("2026-09-19T23:00:00")
    start, end = api.overnight_window(now, TZ)
    assert start == _dt("2026-09-19T18:00:00")
    assert end == _dt("2026-09-20T08:00:00")
    runs = [{"job_id": "evening", "started_at": "2026-09-19T18:45:00-07:00"}]
    kept = api.filter_runs_in_window(runs, start, end, default_tz=TZ)
    assert [r["job_id"] for r in kept] == ["evening"]


def test_overnight_window_utc_vs_los_angeles_bounds():
    now_utc = datetime(2026, 9, 19, 15, 30, tzinfo=timezone.utc)  # 08:30 PDT
    start_utc, end_utc = api.overnight_window(now_utc, ZoneInfo("UTC"))
    start_la, end_la = api.overnight_window(now_utc, TZ)
    job_2am_pdt = datetime(2026, 9, 19, 2, 0, tzinfo=TZ)
    # 02:00 PDT = 09:00 UTC, outside UTC 18:00→08:00; inside LA overnight.
    assert not api.in_window(job_2am_pdt, start_utc, end_utc)
    assert api.in_window(job_2am_pdt, start_la, end_la)


def test_night_payload_honors_tz_name(tmp_path: Path):
    (tmp_path / "config.yaml").write_text("x: 1\n", encoding="utf-8")
    now = datetime(2026, 9, 19, 15, 30, tzinfo=timezone.utc)
    utc = api.night_payload(root=tmp_path, tz_name="UTC", now=now)
    la = api.night_payload(root=tmp_path, tz_name="America/Los_Angeles", now=now)
    assert utc["window"]["tz"] == "UTC"
    assert la["window"]["tz"] == "America/Los_Angeles"
    assert utc["window"]["start"] != la["window"]["start"]
    job = datetime(2026, 9, 19, 2, 0, tzinfo=TZ)
    start_utc = api.parse_datetime(utc["window"]["start"], default_tz=timezone.utc)
    end_utc = api.parse_datetime(utc["window"]["end"], default_tz=timezone.utc)
    start_la = api.parse_datetime(la["window"]["start"], default_tz=TZ)
    end_la = api.parse_datetime(la["window"]["end"], default_tz=TZ)
    assert start_utc is not None and end_utc is not None
    assert start_la is not None and end_la is not None
    assert not api.in_window(job, start_utc, end_utc)
    assert api.in_window(job, start_la, end_la)


def test_resolve_tz_omitted_or_invalid_falls_back_to_process_local():
    z = api.resolve_tz(None)
    assert z is not None
    invalid = api.resolve_tz("Not/A_Zone")
    assert invalid is not None


def test_corrupt_jobs_json_surfaces_errors_not_quiet_night(tmp_path: Path):
    (tmp_path / "config.yaml").write_text("x: 1\n", encoding="utf-8")
    cron = tmp_path / "cron"
    cron.mkdir()
    (cron / "jobs.json").write_text("{not json", encoding="utf-8")
    payload = api.night_payload(
        root=tmp_path, tz_name="America/Los_Angeles", now=_dt("2026-09-19T10:00:00"),
    )
    assert payload["ok"] is False
    assert payload["read_status"] == "unread"
    assert payload["runs"] == []
    assert payload["errors"]
    assert any(e["kind"] == "jobs.json" for e in payload["errors"])


def test_missing_jobs_json_is_empty_success(tmp_path: Path):
    (tmp_path / "config.yaml").write_text("x: 1\n", encoding="utf-8")
    (tmp_path / "cron").mkdir()
    payload = api.night_payload(
        root=tmp_path, tz_name="America/Los_Angeles", now=_dt("2026-09-19T10:00:00"),
    )
    assert payload["ok"] is True
    assert payload["read_status"] == "ok"
    assert payload["runs"] == []
    assert payload["errors"] == []


def test_corrupt_executions_db_surfaces_error(tmp_path: Path):
    (tmp_path / "config.yaml").write_text("x: 1\n", encoding="utf-8")
    cron = tmp_path / "cron"
    cron.mkdir()
    (cron / "jobs.json").write_text(json.dumps({"jobs": []}), encoding="utf-8")
    (cron / "executions.db").write_bytes(b"not a sqlite database")
    payload = api.night_payload(
        root=tmp_path, tz_name="America/Los_Angeles", now=_dt("2026-09-19T10:00:00"),
    )
    assert payload["ok"] is False
    assert payload["read_status"] == "unread"
    assert any(e["kind"] == "executions.db" for e in payload["errors"])


def _write_cron_session(home: Path, *, end_reason, job_id="abc123abc123"):
    (home / "config.yaml").write_text("x: 1\n", encoding="utf-8")
    cron = home / "cron"
    cron.mkdir()
    (cron / "jobs.json").write_text(
        json.dumps({"jobs": [{"id": job_id, "name": "Ping", "schedule_display": "every 1h"}]}),
        encoding="utf-8",
    )
    state = home / "state.db"
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
            f"cron_{job_id}_2026-09-19_02-00-00", "cron", "Ping",
            started, ended, end_reason, 40, 10, 0.004, 0.01, "estimated",
        ),
    )
    conn.commit()
    conn.close()


def test_session_end_reason_ok_is_completed(tmp_path: Path):
    _write_cron_session(tmp_path, end_reason="ok")
    start, end = api.overnight_window(_dt("2026-09-19T10:00:00"), TZ)
    runs = api.filter_runs_in_window(
        api.collect_home_runs(tmp_path, default_tz=TZ, profile="default"),
        start, end, default_tz=TZ,
    )
    assert len(runs) == 1
    assert runs[0]["status"] == "completed"


def test_session_end_reason_timeout_is_unknown(tmp_path: Path):
    _write_cron_session(tmp_path, end_reason="timeout")
    start, end = api.overnight_window(_dt("2026-09-19T10:00:00"), TZ)
    runs = api.filter_runs_in_window(
        api.collect_home_runs(tmp_path, default_tz=TZ, profile="default"),
        start, end, default_tz=TZ,
    )
    assert len(runs) == 1
    assert runs[0]["status"] == "unknown"


def test_session_end_reason_error_is_failed(tmp_path: Path):
    _write_cron_session(tmp_path, end_reason="error")
    start, end = api.overnight_window(_dt("2026-09-19T10:00:00"), TZ)
    runs = api.filter_runs_in_window(
        api.collect_home_runs(tmp_path, default_tz=TZ, profile="default"),
        start, end, default_tz=TZ,
    )
    assert len(runs) == 1
    assert runs[0]["status"] == "failed"


def test_unaudited_markdown_is_unknown_not_completed(tmp_path: Path):
    (tmp_path / "config.yaml").write_text("x: 1\n", encoding="utf-8")
    job_dir = tmp_path / "cron" / "output" / "abc123abc123"
    job_dir.mkdir(parents=True)
    (tmp_path / "cron" / "jobs.json").write_text(
        json.dumps({"jobs": [{"id": "abc123abc123", "name": "Briefing"}]}),
        encoding="utf-8",
    )
    (job_dir / "2026-09-18_22-15-02.md").write_text("# Cron Job: Briefing\n\nstill writing…\n", encoding="utf-8")
    start, end = api.overnight_window(_dt("2026-09-19T10:00:00"), TZ)
    runs = api.filter_runs_in_window(
        api.collect_home_runs(tmp_path, default_tz=TZ, profile="default"),
        start, end, default_tz=TZ,
    )
    assert len(runs) == 1
    assert runs[0]["status"] == "unknown"


def test_failed_markdown_without_audit_is_failed(tmp_path: Path):
    (tmp_path / "config.yaml").write_text("x: 1\n", encoding="utf-8")
    job_dir = tmp_path / "cron" / "output" / "abc123abc123"
    job_dir.mkdir(parents=True)
    (tmp_path / "cron" / "jobs.json").write_text(
        json.dumps({"jobs": [{"id": "abc123abc123", "name": "Briefing"}]}),
        encoding="utf-8",
    )
    (job_dir / "2026-09-18_22-15-02.md").write_text(
        "# Cron Job: Briefing (FAILED)\n\n## Error\nboom\n", encoding="utf-8",
    )
    start, end = api.overnight_window(_dt("2026-09-19T10:00:00"), TZ)
    runs = api.filter_runs_in_window(
        api.collect_home_runs(tmp_path, default_tz=TZ, profile="default"),
        start, end, default_tz=TZ,
    )
    assert len(runs) == 1
    assert runs[0]["status"] == "failed"


def test_summarize_complete_cost_keeps_usd_total():
    summary = api.summarize_runs([
        {"status": "completed", "tokens": 10, "usd": 0.02},
        {"status": "failed", "tokens": 3, "usd": 0.01},
    ])
    assert summary["cost_coverage"] == "complete"
    assert summary["usd"] == pytest.approx(0.03)
    assert summary["usd_billed"] == 2


def test_summarize_running_jobs_are_counted():
    summary = api.summarize_runs([
        {"status": "running", "tokens": None, "usd": None},
        {"status": "claimed", "tokens": None, "usd": None},
        {"status": "completed", "tokens": 1, "usd": None},
    ])
    assert summary["running"] == 2
    assert summary["failed"] == 0
    assert summary["runs"] == 3


def test_overnight_label_switches_after_1800():
    assert api.overnight_label(_dt("2026-09-19T17:59:00"), TZ) == "Last night"
    assert api.overnight_label(_dt("2026-09-19T18:00:00"), TZ) == "Tonight"


def _write_jobs(home: Path, jobs: list) -> None:
    (home / "config.yaml").write_text("x: 1\n", encoding="utf-8")
    cron = home / "cron"
    cron.mkdir(parents=True, exist_ok=True)
    (cron / "jobs.json").write_text(json.dumps({"jobs": jobs}), encoding="utf-8")


def _write_execution(home: Path, *, exe_id: str, job_id: str, status: str,
                     claimed_at: str, started_at: str, finished_at, error=None) -> None:
    db = home / "cron" / "executions.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS executions (
            id TEXT PRIMARY KEY, job_id TEXT, source TEXT, process_id TEXT,
            pid INTEGER, status TEXT, claimed_at TEXT, started_at TEXT,
            finished_at TEXT, error TEXT
        )"""
    )
    conn.execute(
        "INSERT INTO executions VALUES (?,?,?,?,?,?,?,?,?,?)",
        (exe_id, job_id, "builtin", "p", 1, status, claimed_at, started_at, finished_at, error),
    )
    conn.commit()
    conn.close()


def test_normalize_cost_includes_cache_tokens():
    tokens, usd = api.normalize_cost({
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_read_tokens": 100,
        "cache_write_tokens": 20,
    })
    assert tokens == 135
    assert usd is None


def test_normalize_cost_cache_only_counts():
    tokens, usd = api.normalize_cost({"cache_read_tokens": 40, "cache_write_tokens": 2})
    assert tokens == 42
    assert usd is None


def test_hung_running_started_before_window_is_kept():
    start, end = api.overnight_window(_dt("2026-09-19T10:00:00"), TZ)
    runs = [
        {
            "job_id": "hung",
            "started_at": "2026-09-18T17:00:00-07:00",
            "finished_at": None,
            "status": "running",
        },
        {
            "job_id": "claimed",
            "started_at": "2026-09-18T16:45:00-07:00",
            "finished_at": None,
            "status": "claimed",
        },
        {
            "job_id": "daytime_done",
            "started_at": "2026-09-18T17:00:00-07:00",
            "finished_at": "2026-09-18T17:30:00-07:00",
            "status": "completed",
        },
        {
            "job_id": "after_window",
            "started_at": "2026-09-19T09:00:00-07:00",
            "finished_at": None,
            "status": "running",
        },
    ]
    kept = api.filter_runs_in_window(runs, start, end, default_tz=TZ)
    assert {r["job_id"] for r in kept} == {"hung", "claimed"}


def test_in_window_running_is_kept_and_summarized():
    start, end = api.overnight_window(_dt("2026-09-19T10:00:00"), TZ)
    runs = [
        {
            "job_id": "overnight_hung",
            "started_at": "2026-09-18T21:00:00-07:00",
            "finished_at": None,
            "status": "running",
            "usd": None,
        }
    ]
    kept = api.filter_runs_in_window(runs, start, end, default_tz=TZ)
    assert [r["job_id"] for r in kept] == ["overnight_hung"]
    summary = api.summarize_runs(kept)
    assert summary["running"] == 1
    assert summary["failed"] == 0
    assert summary["runs"] == 1


def test_last_run_at_skipped_when_executions_exist(tmp_path: Path):
    job_id = "a1b2c3d4e5f6"
    _write_jobs(tmp_path, [{
        "id": job_id,
        "name": "Briefing",
        "schedule_display": "0 2 * * *",
        "last_run_at": "2026-09-18T23:00:00-07:00",
        "last_status": "ok",
    }])
    _write_execution(
        tmp_path,
        exe_id="exe-fail",
        job_id=job_id,
        status="failed",
        claimed_at="2026-09-18T21:00:00-07:00",
        started_at="2026-09-18T21:00:01-07:00",
        finished_at="2026-09-18T21:00:40-07:00",
        error="boom",
    )
    start, end = api.overnight_window(_dt("2026-09-19T10:00:00"), TZ)
    runs = api.filter_runs_in_window(
        api.collect_home_runs(tmp_path, default_tz=TZ, profile="default"),
        start, end, default_tz=TZ,
    )
    assert len(runs) == 1
    assert runs[0]["status"] == "failed"
    assert runs[0]["error"] and "boom" in runs[0]["error"]
    assert not any(str(r.get("id") or "").startswith("last:") for r in runs)


def test_last_run_at_used_when_no_other_history(tmp_path: Path):
    job_id = "a1b2c3d4e5f6"
    _write_jobs(tmp_path, [{
        "id": job_id,
        "name": "Briefing",
        "schedule_display": "0 2 * * *",
        "last_run_at": "2026-09-18T22:00:00-07:00",
        "last_status": "failed",
        "last_error": "delivery failed",
    }])
    start, end = api.overnight_window(_dt("2026-09-19T10:00:00"), TZ)
    runs = api.filter_runs_in_window(
        api.collect_home_runs(tmp_path, default_tz=TZ, profile="default"),
        start, end, default_tz=TZ,
    )
    assert len(runs) == 1
    assert runs[0]["status"] == "failed"
    assert runs[0]["name"] == "Briefing"


def test_usage_audit_without_error_does_not_paint_completed(tmp_path: Path):
    job_id = "a1b2c3d4e5f6"
    _write_jobs(tmp_path, [{"id": job_id, "name": "Briefing"}])
    (tmp_path / "cron" / "usage_audit.jsonl").write_text(
        json.dumps({
            "ts": "2026-09-19T05:00:00.000Z",
            "job_id": job_id,
            "fire_id": "fire-tokens",
            "prompt_tokens": 80,
            "completion_tokens": 20,
            "total_tokens": 100,
            "error": None,
        }) + "\n",
        encoding="utf-8",
    )
    start, end = api.overnight_window(_dt("2026-09-19T10:00:00"), TZ)
    runs = api.filter_runs_in_window(
        api.collect_home_runs(tmp_path, default_tz=TZ, profile="default"),
        start, end, default_tz=TZ,
    )
    assert len(runs) == 1
    assert runs[0]["status"] is None
    assert runs[0]["tokens"] == 100
    assert runs[0]["usd"] is None


def test_usage_audit_merges_tokens_without_overwriting_completed(tmp_path: Path):
    job_id = "a1b2c3d4e5f6"
    _write_jobs(tmp_path, [{"id": job_id, "name": "Briefing"}])
    _write_execution(
        tmp_path,
        exe_id="exe-ok",
        job_id=job_id,
        status="completed",
        claimed_at="2026-09-18T21:00:00-07:00",
        started_at="2026-09-18T21:00:00-07:00",
        finished_at="2026-09-18T21:00:40-07:00",
    )
    (tmp_path / "cron" / "usage_audit.jsonl").write_text(
        json.dumps({
            "ts": "2026-09-19T04:00:40.100Z",
            "job_id": job_id,
            "fire_id": "fire-ok",
            "total_tokens": 100,
            "error": None,
        }) + "\n",
        encoding="utf-8",
    )
    start, end = api.overnight_window(_dt("2026-09-19T10:00:00"), TZ)
    runs = api.filter_runs_in_window(
        api.collect_home_runs(tmp_path, default_tz=TZ, profile="default"),
        start, end, default_tz=TZ,
    )
    assert len(runs) == 1
    assert runs[0]["status"] == "completed"
    assert runs[0]["tokens"] == 100


def test_merge_matches_audit_to_finished_at_not_only_started(tmp_path: Path):
    job_id = "a1b2c3d4e5f6"
    _write_jobs(tmp_path, [{"id": job_id, "name": "Long briefing"}])
    _write_execution(
        tmp_path,
        exe_id="exe-long",
        job_id=job_id,
        status="completed",
        claimed_at="2026-09-18T21:00:00-07:00",
        started_at="2026-09-18T21:00:00-07:00",
        finished_at="2026-09-18T21:04:00-07:00",
    )
    (tmp_path / "cron" / "usage_audit.jsonl").write_text(
        json.dumps({
            "ts": "2026-09-19T04:04:00.000Z",
            "job_id": job_id,
            "fire_id": "fire-long",
            "total_tokens": 250,
            "error": None,
        }) + "\n",
        encoding="utf-8",
    )
    start, end = api.overnight_window(_dt("2026-09-19T10:00:00"), TZ)
    runs = api.filter_runs_in_window(
        api.collect_home_runs(tmp_path, default_tz=TZ, profile="default"),
        start, end, default_tz=TZ,
    )
    assert len(runs) == 1
    assert runs[0]["tokens"] == 250
    assert runs[0]["status"] == "completed"


def test_night_payload_labels_profiles_when_unioning_homes(tmp_path: Path):
    default = tmp_path
    kid = tmp_path / "profiles" / "kid"
    kid.mkdir(parents=True)
    _write_jobs(default, [{
        "id": "aaaaaaaaaaaa",
        "name": "Parent briefing",
        "last_run_at": "2026-09-18T22:00:00-07:00",
        "last_status": "ok",
    }])
    _write_jobs(kid, [{
        "id": "bbbbbbbbbbbb",
        "name": "Kid ping",
        "last_run_at": "2026-09-18T22:30:00-07:00",
        "last_status": "failed",
        "last_error": "no",
    }])
    payload = api.night_payload(
        root=default, tz_name="America/Los_Angeles", now=_dt("2026-09-19T10:00:00"),
    )
    assert "default" in payload["homes"]
    assert "kid" in payload["homes"]
    by_name = {r["name"]: r for r in payload["runs"]}
    assert by_name["Parent briefing"]["profile"] == "default"
    assert by_name["Kid ping"]["profile"] == "kid"
    assert by_name["Parent briefing"]["home"]
    assert by_name["Kid ping"]["home"]


def test_runs_carry_profile_on_execution_rows(tmp_path: Path):
    _write_jobs(tmp_path, [{"id": "a1b2c3d4e5f6", "name": "Briefing"}])
    _write_execution(
        tmp_path,
        exe_id="exe1",
        job_id="a1b2c3d4e5f6",
        status="completed",
        claimed_at="2026-09-18T21:00:00-07:00",
        started_at="2026-09-18T21:00:00-07:00",
        finished_at="2026-09-18T21:00:10-07:00",
    )
    runs = api.collect_home_runs(tmp_path, default_tz=TZ, profile="forge")
    assert runs[0]["profile"] == "forge"
    assert runs[0]["home"] == str(tmp_path)


def test_load_cron_sessions_orders_newest_first(tmp_path: Path):
    (tmp_path / "config.yaml").write_text("x: 1\n", encoding="utf-8")
    state = tmp_path / "state.db"
    conn = sqlite3.connect(state)
    conn.execute(
        """CREATE TABLE sessions (
            id TEXT PRIMARY KEY, source TEXT, title TEXT,
            started_at REAL, ended_at REAL, end_reason TEXT
        )"""
    )
    overnight = datetime(2026, 9, 19, 2, 0, tzinfo=TZ).timestamp()
    old = datetime(2026, 1, 1, 2, 0, tzinfo=TZ).timestamp()
    for i in range(500):
        conn.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?)",
            (f"cron_old{i:04d}oldx_{i}", "cron", "Old", old - i, old - i + 1, "ok"),
        )
    conn.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?)",
        ("cron_abc123abc123_2026-09-19_02-00-00", "cron", "Night", overnight, overnight + 60, "ok"),
    )
    conn.commit()
    conn.close()
    rows = api.load_cron_sessions(state)
    assert len(rows) == 500
    ids = [r["id"] for r in rows]
    assert "cron_abc123abc123_2026-09-19_02-00-00" in ids
    assert ids[0] == "cron_abc123abc123_2026-09-19_02-00-00"


def test_session_limit_keeps_overnight_run_in_payload(tmp_path: Path):
    (tmp_path / "config.yaml").write_text("x: 1\n", encoding="utf-8")
    cron = tmp_path / "cron"
    cron.mkdir()
    (cron / "jobs.json").write_text(
        json.dumps({"jobs": [{"id": "abc123abc123", "name": "Night"}]}),
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
    overnight = datetime(2026, 9, 19, 2, 0, tzinfo=TZ).timestamp()
    old = datetime(2026, 1, 1, 2, 0, tzinfo=TZ).timestamp()
    for i in range(500):
        conn.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (f"cron_old{i:04d}oldx_{i}", "cron", "Old", old - i, old - i + 1, "ok",
             1, 1, None, None, None),
        )
    conn.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("cron_abc123abc123_2026-09-19_02-00-00", "cron", "Night",
         overnight, overnight + 60, "ok", 4, 1, 0.001, None, None),
    )
    conn.commit()
    conn.close()
    payload = api.night_payload(
        root=tmp_path, tz_name="America/Los_Angeles", now=_dt("2026-09-19T10:00:00"),
    )
    names = [r["name"] for r in payload["runs"]]
    assert "Night" in names

