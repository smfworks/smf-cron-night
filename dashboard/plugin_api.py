"""SMF Cron Night — overnight cron runs from local Hermes storage.

Reads profile-local cron state under each Hermes home (default ``~/.hermes``
and ``~/.hermes/profiles/<name>``):

* ``cron/jobs.json`` — job name, schedule, last_run_at / last_status / last_error
* ``cron/executions.db`` — durable attempt ledger (claimed → running →
  completed | failed | unknown)
* ``cron/usage_audit.jsonl`` — per-fire token counts (no USD)
* ``cron/output/{job_id}/{timestamp}.md`` (+ optional ``.audit.json``)
* ``state.db`` sessions with ``source='cron'`` — tokens and recorded USD

Never invents a dollar amount. Tokens and USD are nullable.

``GET /night`` query params:

* ``tz`` — IANA name from Desktop (``Intl.DateTimeFormat().resolvedOptions().timeZone``).
  Window bounds are computed in this zone. If omitted or invalid, the serve
  process local timezone is used (``datetime.now().astimezone()``, often UTC
  on a systemd gateway).
* ``start`` / ``end`` — optional explicit ISO-8601 bounds.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, time, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

try:
    from fastapi import APIRouter
    from fastapi.responses import JSONResponse

    router = APIRouter()
except ImportError:  # tests / hosts without FastAPI still import the helpers
    APIRouter = None  # type: ignore[misc, assignment]
    JSONResponse = None  # type: ignore[misc, assignment]
    router = None

PLUGIN = "smf-cron-night"
ERROR_SNIPPET = 240
OUTPUT_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{2}-\d{2}-\d{2})$")
CRON_SESSION_RE = re.compile(r"^cron_([A-Za-z0-9]+)_(.+)$")
OVERNIGHT_START = time(18, 0)
OVERNIGHT_END = time(8, 0)

# Hermes execution ledger + job last_status + audit.json status.
_STATUS_COMPLETED = frozenset({"completed", "ok", "success", "succeeded", "ok_silent"})
_STATUS_FAILED = frozenset({
    "failed", "fail", "error", "delivery_failed", "delivery-failed",
})
_STATUS_RUNNING = frozenset({"running"})
_STATUS_CLAIMED = frozenset({"claimed"})
_STATUS_UNKNOWN = frozenset({"unknown"})
_TERMINAL_FAILED = frozenset({"failed", "unknown"})

_HOME_MARKERS = ("config.yaml", "cron", "state.db", ".env")


# ---------------------------------------------------------------------------
# Time / overnight window
# ---------------------------------------------------------------------------

def resolve_tz(name: Optional[str] = None) -> tzinfo:
    raw = (name or "").strip()
    if raw:
        try:
            return ZoneInfo(raw)
        except Exception:
            pass
    local = datetime.now().astimezone().tzinfo
    return local or timezone.utc


def parse_datetime(value: Any, *, default_tz: tzinfo) -> Optional[datetime]:
    """Parse Hermes timestamps. Naive ISO is treated as *default_tz* (local)."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            return dt.replace(tzinfo=default_tz)
        return dt
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:  # milliseconds
            ts /= 1000.0
        if ts <= 0:
            return None
        return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(default_tz)
    text = str(value).strip()
    if not text:
        return None
    # Output filenames: 2026-09-18_23-15-02
    m = OUTPUT_TS_RE.fullmatch(text)
    if m:
        day, hms = m.group(1), m.group(2).replace("-", ":")
        text = f"{day}T{hms}"
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=default_tz)
    return dt.astimezone(default_tz)


def overnight_window(
    now: Optional[datetime] = None,
    tz: Optional[tzinfo] = None,
) -> Tuple[datetime, datetime]:
    """Local overnight window: 18:00 → following 08:00.

    * After 18:00 (inclusive): *today* 18:00 → *tomorrow* 08:00 (the night
      that just started).
    * Before 18:00: *yesterday* 18:00 → *today* 08:00 (in progress before
      08:00; last closed night from 08:00 until 18:00).
    """
    zone = tz or resolve_tz()
    current = now.astimezone(zone) if now is not None else datetime.now(zone)
    today = current.date()
    evening = datetime.combine(today, OVERNIGHT_START, tzinfo=zone)
    morning = datetime.combine(today, OVERNIGHT_END, tzinfo=zone)
    if current >= evening:
        start = evening
        end = datetime.combine(today + timedelta(days=1), OVERNIGHT_END, tzinfo=zone)
    else:
        start = datetime.combine(today - timedelta(days=1), OVERNIGHT_START, tzinfo=zone)
        end = morning
    return start, end


def overnight_label(now: datetime, tz: tzinfo) -> str:
    """'Tonight' after local 18:00; otherwise 'Last night'."""
    current = now.astimezone(tz)
    evening = datetime.combine(current.date(), OVERNIGHT_START, tzinfo=tz)
    return "Tonight" if current >= evening else "Last night"


def _record_error(
    errors: Optional[List[Dict[str, Any]]],
    *,
    kind: str,
    path: Any,
    error: Any,
    home: Optional[str] = None,
) -> None:
    if errors is None:
        return
    rec: Dict[str, Any] = {
        "kind": kind,
        "path": str(path) if path is not None else None,
        "error": str(error),
    }
    if home:
        rec["home"] = home
    errors.append(rec)


def in_window(dt: Optional[datetime], start: datetime, end: datetime) -> bool:
    if dt is None:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=start.tzinfo)
    else:
        dt = dt.astimezone(start.tzinfo)
    return start <= dt < end


# ---------------------------------------------------------------------------
# Status + cost normalization
# ---------------------------------------------------------------------------

def normalize_status(value: Any) -> Optional[str]:
    if value is None:
        return None
    raw = str(value).strip().lower().replace(" ", "_")
    if not raw:
        return None
    if raw in _STATUS_COMPLETED:
        return "completed"
    if raw in _STATUS_FAILED:
        return "failed"
    if raw in _STATUS_RUNNING:
        return "running"
    if raw in _STATUS_CLAIMED:
        return "claimed"
    if raw in _STATUS_UNKNOWN:
        return "unknown"
    return None


def _finite_number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        num = float(value)
        if num != num or num in (float("inf"), float("-inf")):  # noqa: PLR0124
            return None
        return num
    if isinstance(value, str):
        text = value.strip().replace("$", "").replace(",", "")
        if not text:
            return None
        try:
            num = float(text)
        except ValueError:
            return None
        if num != num or num in (float("inf"), float("-inf")):  # noqa: PLR0124
            return None
        return num
    return None


def _finite_int(value: Any) -> Optional[int]:
    num = _finite_number(value)
    if num is None:
        return None
    return int(num)


def normalize_cost(payload: Any) -> Tuple[Optional[int], Optional[float]]:
    """Return ``(tokens, usd)``. USD is taken only from recorded fields — never priced."""
    if not isinstance(payload, dict):
        return None, None
    tokens = None
    for key in ("total_tokens", "tokens", "total"):
        tokens = _finite_int(payload.get(key))
        if tokens is not None:
            break
    if tokens is None:
        parts = [
            _finite_int(payload.get(k))
            for k in (
                "prompt_tokens",
                "completion_tokens",
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "reasoning_tokens",
            )
        ]
        present = [p for p in parts if p is not None]
        if present:
            # usage_audit: prompt + completion; session row: input + output (+ cache if stored)
            prompt = _finite_int(payload.get("prompt_tokens"))
            completion = _finite_int(payload.get("completion_tokens"))
            if prompt is not None or completion is not None:
                tokens = (prompt or 0) + (completion or 0)
            else:
                inp = _finite_int(payload.get("input_tokens")) or 0
                out = _finite_int(payload.get("output_tokens")) or 0
                cache_r = _finite_int(payload.get("cache_read_tokens")) or 0
                cache_w = _finite_int(payload.get("cache_write_tokens")) or 0
                tokens = inp + out + cache_r + cache_w
                if tokens == 0 and not any(
                    payload.get(k) is not None
                    for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
                ):
                    tokens = None

    usd = None
    cost_status = str(payload.get("cost_status") or payload.get("costStatus") or "").strip().lower()
    # "unknown" / "n/a" means Hermes itself did not know — do not treat missing as $0.
    if cost_status not in {"unknown", "n/a", "na"}:
        for key in (
            "actual_cost_usd",
            "amount_usd",
            "cost_usd",
            "usd",
            "estimated_cost_usd",
        ):
            usd = _finite_number(payload.get(key))
            if usd is not None:
                break
        # "included" subscription routes are not a dollar figure.
        if cost_status == "included":
            usd = None
    return tokens, usd


def error_snippet(value: Any, limit: int = ERROR_SNIPPET) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, dict):
        for key in ("message", "error", "detail", "reason"):
            inner = value.get(key)
            if inner:
                return error_snippet(inner, limit)
        try:
            text = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(value)
    else:
        text = str(value)
    text = " ".join(text.strip().split())
    if not text:
        return None
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _error_from_markdown(text: str) -> Optional[str]:
    if not text:
        return None
    lower = text.lower()
    if "## error" in lower:
        idx = lower.index("## error")
        body = text[idx + len("## error") :].strip()
        if body.startswith("```"):
            body = body.split("```", 2)
            body = body[1] if len(body) > 1 else body[0]
        return error_snippet(body)
    if "(failed)" in lower:
        return error_snippet(text.split("## Error")[-1] if "## Error" in text else text)
    return None


def schedule_display(job: Dict[str, Any]) -> str:
    display = job.get("schedule_display")
    if isinstance(display, str) and display.strip():
        return display.strip()
    sched = job.get("schedule")
    if isinstance(sched, str) and sched.strip():
        return sched.strip()
    if isinstance(sched, dict):
        for key in ("display", "expr", "kind"):
            val = sched.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return ""


# ---------------------------------------------------------------------------
# Homes + file IO
# ---------------------------------------------------------------------------

def _is_hermes_home(path: Path) -> bool:
    try:
        if not path.is_dir():
            return False
    except OSError:
        return False
    return any((path / marker).exists() for marker in _HOME_MARKERS)


def discover_hermes_homes(root: Optional[Path] = None) -> List[Path]:
    """Default home + named profiles. Does not invent remote URLs."""
    found: List[Path] = []
    seen: set[str] = set()

    def add(path: Path) -> None:
        try:
            resolved = path.expanduser()
            if resolved.exists():
                resolved = resolved.resolve()
        except OSError:
            return
        key = str(resolved)
        if key in seen:
            return
        if not _is_hermes_home(resolved):
            return
        seen.add(key)
        found.append(resolved)

    env = os.environ.get("HERMES_HOME")
    if env:
        add(Path(env))
    default = Path(root) if root is not None else Path.home() / ".hermes"
    add(default)
    for base in list(found):
        profiles = base / "profiles"
        if profiles.is_dir():
            try:
                kids = list(profiles.iterdir())
            except OSError:
                kids = []
            for child in kids:
                if child.name.startswith("."):
                    continue
                add(child)
        # HERMES_HOME may already be a profile dir; still scan siblings.
        if base.parent.name == "profiles":
            try:
                siblings = list(base.parent.iterdir())
            except OSError:
                siblings = []
            for child in siblings:
                if child.name.startswith("."):
                    continue
                add(child)
            add(base.parent.parent)
    return found


def load_jobs_file(
    path: Path,
    errors: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        _record_error(errors, kind="jobs.json", path=path, error=exc)
        return []
    if isinstance(raw, list):
        jobs = raw
    elif isinstance(raw, dict):
        jobs = raw.get("jobs", [])
        if isinstance(jobs, dict):
            jobs = [{**v, "id": v.get("id") or k} for k, v in jobs.items() if isinstance(v, dict)]
    else:
        _record_error(errors, kind="jobs.json", path=path, error="jobs.json is not a list or object")
        return []
    return [j for j in jobs if isinstance(j, dict)]


def load_usage_audit(
    path: Path,
    errors: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    if not path.is_file():
        return []
    rows: List[Dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict):
                    rows.append(rec)
    except OSError as exc:
        _record_error(errors, kind="usage_audit.jsonl", path=path, error=exc)
        return []
    return rows


def load_executions_db(
    path: Path,
    errors: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        _record_error(errors, kind="executions.db", path=path, error=exc)
        return []
    conn.row_factory = sqlite3.Row
    try:
        try:
            rows = conn.execute(
                "SELECT * FROM executions ORDER BY claimed_at DESC, id DESC LIMIT 500"
            ).fetchall()
        except sqlite3.Error as exc:
            _record_error(errors, kind="executions.db", path=path, error=exc)
            return []
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _session_columns(conn: sqlite3.Connection) -> set[str]:
    try:
        info = conn.execute("PRAGMA table_info(sessions)").fetchall()
    except sqlite3.Error:
        return set()
    names: set[str] = set()
    for row in info:
        try:
            names.add(str(row[1]))
        except Exception:
            continue
    return names


def load_cron_sessions(
    path: Path,
    errors: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        _record_error(errors, kind="state.db", path=path, error=exc)
        return []
    conn.row_factory = sqlite3.Row
    try:
        cols = _session_columns(conn)
        if "source" not in cols or "id" not in cols:
            _record_error(
                errors,
                kind="state.db",
                path=path,
                error="sessions table missing source/id columns",
            )
            return []
        want = [
            c for c in (
                "id", "source", "title", "model", "started_at", "ended_at",
                "end_reason", "input_tokens", "output_tokens",
                "cache_read_tokens", "cache_write_tokens", "reasoning_tokens",
                "estimated_cost_usd", "actual_cost_usd", "cost_status",
                "cost_source", "preview",
            )
            if c in cols
        ]
        sql = f"SELECT {', '.join(want)} FROM sessions WHERE source = ? LIMIT 500"
        try:
            rows = conn.execute(sql, ("cron",)).fetchall()
        except sqlite3.Error as exc:
            _record_error(errors, kind="state.db", path=path, error=exc)
            return []
        return [dict(row) for row in rows]
    finally:
        conn.close()


def load_output_runs(
    output_dir: Path,
    default_tz: tzinfo,
    errors: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    if not output_dir.is_dir():
        return []
    runs: List[Dict[str, Any]] = []
    try:
        job_dirs = list(output_dir.iterdir())
    except OSError as exc:
        _record_error(errors, kind="output", path=output_dir, error=exc)
        return []
    for job_dir in job_dirs:
        if not job_dir.is_dir() or job_dir.name.startswith("."):
            continue
        try:
            files = list(job_dir.iterdir())
        except OSError as exc:
            _record_error(errors, kind="output", path=job_dir, error=exc)
            continue
        for path in files:
            stem = path.stem
            if path.suffix == ".json" and stem.endswith(".audit"):
                # 2026-09-18_23-15-02.audit.json → handled with matching .md
                continue
            if path.suffix not in {".md", ".json"}:
                continue
            ts_key = stem
            if ts_key.endswith(".audit"):
                continue
            if not OUTPUT_TS_RE.fullmatch(ts_key):
                continue
            started = parse_datetime(ts_key, default_tz=default_tz)
            rec: Dict[str, Any] = {
                "job_id": job_dir.name,
                "started_at": started.isoformat() if started else None,
                "output_path": str(path),
            }
            audit_path = job_dir / f"{ts_key}.audit.json"
            if audit_path.is_file():
                try:
                    audit = json.loads(audit_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                    _record_error(errors, kind="output", path=audit_path, error=exc)
                    audit = None
                if isinstance(audit, dict):
                    rec["audit"] = audit
                    rec["status"] = audit.get("status")
                    rec["name"] = audit.get("job_name")
                    rec["schedule"] = audit.get("schedule")
                    rec["started_at"] = audit.get("start_time") or rec["started_at"]
                    rec["finished_at"] = audit.get("end_time")
                    rec["error"] = audit.get("error")
                    agent = audit.get("agent") if isinstance(audit.get("agent"), dict) else {}
                    rec.update({k: agent.get(k) for k in ("prompt_tokens", "completion_tokens", "total_tokens") if k in agent})
                    for k in ("input_tokens", "output_tokens", "actual_cost_usd", "estimated_cost_usd", "cost_usd"):
                        if k in audit:
                            rec[k] = audit[k]
            if path.suffix == ".md":
                try:
                    md = path.read_text(encoding="utf-8", errors="replace")
                except OSError as exc:
                    _record_error(errors, kind="output", path=path, error=exc)
                    md = ""
                if rec.get("error") is None:
                    rec["error"] = _error_from_markdown(md)
                if rec.get("status") is None:
                    # Unaudited markdown is not proof of success.
                    failed = "(failed)" in md.lower() or bool(rec.get("error"))
                    rec["status"] = "failed" if failed else "unknown"
            runs.append(rec)
    return runs


# ---------------------------------------------------------------------------
# Merge + filter
# ---------------------------------------------------------------------------

def _profile_name(home: Path, default_home: Path) -> str:
    if home.parent.name == "profiles":
        return home.name
    if home == default_home:
        return "default"
    return home.name or "default"


MERGE_WINDOW_SECONDS = 120


def _run_times(run: Dict[str, Any], default_tz: tzinfo) -> List[datetime]:
    times: List[datetime] = []
    for key in ("started_at", "finished_at"):
        dt = parse_datetime(run.get(key), default_tz=default_tz)
        if dt is not None:
            times.append(dt)
    return times


def _closest_partner(
    merged: Sequence[Dict[str, Any]],
    run: Dict[str, Any],
    *,
    default_tz: tzinfo,
) -> Optional[Dict[str, Any]]:
    job_id = str(run.get("job_id") or "")
    times = _run_times(run, default_tz)
    partner: Optional[Dict[str, Any]] = None
    partner_delta = MERGE_WINDOW_SECONDS + 1
    for other in merged:
        if str(other.get("job_id") or "") != job_id:
            continue
        other_times = _run_times(other, default_tz)
        if not times and not other_times:
            return other
        for left in times:
            for right in other_times:
                delta = abs((left - right).total_seconds())
                if delta <= MERGE_WINDOW_SECONDS and delta < partner_delta:
                    partner = other
                    partner_delta = delta
    return partner


def _prefer_status(current: Optional[str], incoming: Optional[str]) -> Optional[str]:
    rank = {
        "failed": 50,
        "unknown": 40,
        "running": 30,
        "claimed": 20,
        "completed": 10,
    }
    if incoming is None:
        return current
    if current is None:
        return incoming
    return incoming if rank.get(incoming, 0) >= rank.get(current, 0) else current


def _merge_run(target: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    for key in ("id", "job_id", "name", "schedule", "started_at", "finished_at",
                "session_id", "profile", "home"):
        if not target.get(key) and extra.get(key):
            target[key] = extra[key]
    target["status"] = _prefer_status(target.get("status"), extra.get("status"))
    if extra.get("error") and not target.get("error"):
        target["error"] = extra["error"]
    tok_a, usd_a = target.get("tokens"), target.get("usd")
    tok_b, usd_b = extra.get("tokens"), extra.get("usd")
    if tok_a is None and tok_b is not None:
        target["tokens"] = tok_b
    if usd_a is None and usd_b is not None:
        target["usd"] = usd_b
    return target


def empty_run() -> Dict[str, Any]:
    return {
        "id": "",
        "job_id": "",
        "name": "",
        "schedule": "",
        "started_at": None,
        "finished_at": None,
        "status": None,
        "tokens": None,
        "usd": None,
        "error": None,
        "session_id": None,
        "profile": "",
    }


def normalize_run(raw: Dict[str, Any], *, default_tz: tzinfo) -> Dict[str, Any]:
    run = empty_run()
    job_id = str(raw.get("job_id") or raw.get("id") or "").strip()
    run["job_id"] = job_id
    run["name"] = str(raw.get("name") or raw.get("job_name") or job_id)
    run["schedule"] = str(raw.get("schedule") or raw.get("schedule_display") or "")
    started = parse_datetime(
        raw.get("started_at") or raw.get("claimed_at") or raw.get("start_time") or raw.get("ts") or raw.get("last_run_at"),
        default_tz=default_tz,
    )
    finished = parse_datetime(
        raw.get("finished_at") or raw.get("end_time") or raw.get("ended_at"),
        default_tz=default_tz,
    )
    run["started_at"] = started.isoformat() if started else None
    run["finished_at"] = finished.isoformat() if finished else None
    status = normalize_status(raw.get("status") or raw.get("last_status"))
    if status is None and raw.get("error"):
        status = "failed"
    run["status"] = status
    tokens, usd = normalize_cost(raw)
    run["tokens"] = tokens
    run["usd"] = usd
    err = raw.get("error") or raw.get("last_error") or raw.get("last_delivery_error")
    run["error"] = error_snippet(err)
    run["session_id"] = raw.get("session_id") or raw.get("sessionId")
    run["profile"] = str(raw.get("profile") or "")
    run["id"] = str(
        raw.get("execution_id")
        or raw.get("id")
        or raw.get("fire_id")
        or raw.get("session_id")
        or (f"{job_id}:{started.isoformat()}" if started else job_id)
    )
    return run


def collect_home_runs(
    home: Path,
    *,
    default_tz: tzinfo,
    profile: str,
    errors: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    cron_dir = home / "cron"
    jobs = {
        str(j.get("id") or ""): j
        for j in load_jobs_file(cron_dir / "jobs.json", errors=errors)
        if j.get("id")
    }
    raw_rows: List[Dict[str, Any]] = []
    home_label = str(home)

    for exe in load_executions_db(cron_dir / "executions.db", errors=errors):
        job = jobs.get(str(exe.get("job_id") or ""), {})
        raw_rows.append({
            **exe,
            "execution_id": exe.get("id"),
            "name": job.get("name") or exe.get("job_id"),
            "schedule": schedule_display(job),
            "profile": profile,
            "home": home_label,
        })

    for rec in load_output_runs(cron_dir / "output", default_tz, errors=errors):
        job = jobs.get(str(rec.get("job_id") or ""), {})
        merged = {
            **rec,
            "name": rec.get("name") or job.get("name") or rec.get("job_id"),
            "schedule": rec.get("schedule") or schedule_display(job),
            "profile": profile,
        }
        raw_rows.append(merged)

    for rec in load_usage_audit(cron_dir / "usage_audit.jsonl", errors=errors):
        job = jobs.get(str(rec.get("job_id") or ""), {})
        raw_rows.append({
            **rec,
            "started_at": rec.get("ts"),
            "status": "failed" if rec.get("error") else "completed",
            "name": job.get("name") or rec.get("job_id"),
            "schedule": schedule_display(job),
            "profile": profile,
            "id": rec.get("fire_id"),
        })

    for sess in load_cron_sessions(home / "state.db", errors=errors):
        sid = str(sess.get("id") or "")
        m = CRON_SESSION_RE.match(sid)
        job_id = m.group(1) if m else ""
        job = jobs.get(job_id, {})
        started = sess.get("started_at")
        ended = sess.get("ended_at")
        status = None
        if ended:
            reason = str(sess.get("end_reason") or "").strip()
            mapped = normalize_status(reason) if reason else None
            if mapped == "failed":
                status = "failed"
            elif mapped == "completed":
                status = "completed"
            else:
                # Missing or unmapped end_reason (timeout, cancelled, killed, …).
                status = "unknown"
        elif started:
            status = "running"
        raw_rows.append({
            **sess,
            "job_id": job_id or sid,
            "session_id": sid,
            "name": job.get("name") or sess.get("title") or job_id or sid,
            "schedule": schedule_display(job),
            "started_at": started,
            "finished_at": ended,
            "status": status,
            "error": sess.get("preview") if status == "failed" else None,
            "profile": profile,
        })

    # Last-run fallback when a job ran in-window but left no ledger/output/session.
    for job in jobs.values():
        if not job.get("last_run_at"):
            continue
        raw_rows.append({
            "id": f"last:{job.get('id')}:{job.get('last_run_at')}",
            "job_id": job.get("id"),
            "name": job.get("name") or job.get("id"),
            "schedule": schedule_display(job),
            "last_run_at": job.get("last_run_at"),
            "started_at": job.get("last_run_at"),
            "status": job.get("last_status"),
            "error": job.get("last_error") or job.get("last_delivery_error"),
            "profile": profile,
        })

    normalized = [normalize_run(r, default_tz=default_tz) for r in raw_rows]
    merged: List[Dict[str, Any]] = []
    for run in normalized:
        partner = _closest_partner(merged, run, default_tz=default_tz)
        if partner is not None:
            _merge_run(partner, run)
        else:
            merged.append(run)
    return merged


def filter_runs_in_window(
    runs: Sequence[Dict[str, Any]],
    start: datetime,
    end: datetime,
    *,
    default_tz: tzinfo,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for run in runs:
        started = parse_datetime(run.get("started_at"), default_tz=default_tz)
        finished = parse_datetime(run.get("finished_at"), default_tz=default_tz)
        if in_window(started, start, end) or in_window(finished, start, end):
            out.append(run)
    out.sort(
        key=lambda r: parse_datetime(r.get("started_at"), default_tz=default_tz) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return out


def summarize_runs(runs: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    failed = 0
    running = 0
    token_sum = 0
    token_any = False
    usd_sum = 0.0
    usd_billed = 0
    for run in runs:
        status = run.get("status")
        if status in _TERMINAL_FAILED or status == "failed":
            failed += 1
        elif status is None and run.get("error"):
            failed += 1
        if status in ("running", "claimed"):
            running += 1
        tokens = run.get("tokens")
        if isinstance(tokens, int):
            token_sum += tokens
            token_any = True
        usd = run.get("usd")
        if isinstance(usd, (int, float)) and usd == usd:  # noqa: PLR0124
            usd_sum += float(usd)
            usd_billed += 1
    n = len(runs)
    if usd_billed == 0:
        coverage = "none"
        usd_total: Optional[float] = None
    elif usd_billed == n:
        coverage = "complete"
        usd_total = round(usd_sum, 6)
    else:
        # Partial sum is not the night's bill — omit usd so consumers cannot
        # present it as a total.
        coverage = "partial"
        usd_total = None
    return {
        "runs": n,
        "failed": failed,
        "running": running,
        "tokens": token_sum if token_any else None,
        "usd": usd_total,
        "usd_billed": usd_billed,
        "cost_coverage": coverage,
    }


def night_payload(
    *,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    tz_name: Optional[str] = None,
    root: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    zone = resolve_tz(tz_name)
    as_of = now.astimezone(zone) if now is not None else datetime.now(zone)
    if start is None or end is None:
        win_start, win_end = overnight_window(now=as_of, tz=zone)
        start = start or win_start
        end = end or win_end
    default_home = Path(root) if root is not None else Path.home() / ".hermes"
    homes = discover_hermes_homes(default_home if root is not None else None)
    if root is not None and not homes:
        homes = discover_hermes_homes(root)

    all_runs: List[Dict[str, Any]] = []
    home_labels: List[str] = []
    errors: List[Dict[str, Any]] = []
    for home in homes:
        profile = _profile_name(home, default_home)
        home_labels.append(profile)
        before = len(errors)
        all_runs.extend(
            collect_home_runs(home, default_tz=zone, profile=profile, errors=errors)
        )
        for rec in errors[before:]:
            rec.setdefault("home", profile)

    windowed = filter_runs_in_window(all_runs, start, end, default_tz=zone)
    summary = summarize_runs(windowed)
    if errors:
        read_status = "partial" if windowed else "unread"
        ok = False
    else:
        read_status = "ok"
        ok = True
    tz_label = getattr(zone, "key", None) or str(zone)
    return {
        "ok": ok,
        "plugin": PLUGIN,
        "read_status": read_status,
        "window": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "tz": tz_label,
            "label": overnight_label(as_of, zone),
        },
        "summary": summary,
        "runs": windowed,
        "homes": home_labels,
        "errors": errors,
        "source": "disk",
        "degraded": False,
    }


def _json(payload: Dict[str, Any], status: int = 200):
    if JSONResponse is None:
        return payload
    return JSONResponse(payload, status_code=status)


if router is not None:

    @router.get("/night")
    def night(
        start: Optional[str] = None,
        end: Optional[str] = None,
        tz: Optional[str] = None,
    ):
        """Overnight cron runs for the Desktop pane.

        ``tz`` should be the Desktop IANA zone (``Intl`` resolvedOptions).
        If omitted or invalid, window bounds use the serve process local
        timezone (often UTC).
        """
        zone = resolve_tz(tz)
        start_dt = parse_datetime(start, default_tz=zone) if start else None
        end_dt = parse_datetime(end, default_tz=zone) if end else None
        try:
            payload = night_payload(start=start_dt, end=end_dt, tz_name=tz)
        except Exception as exc:  # pragma: no cover - defensive mount
            return _json({
                "ok": False,
                "error": str(exc),
                "plugin": PLUGIN,
                "read_status": "unread",
                "errors": [{"kind": "night", "path": None, "error": str(exc)}],
                "runs": [],
                "summary": {
                    "runs": 0, "failed": 0, "running": 0,
                    "tokens": None, "usd": None, "usd_billed": 0,
                    "cost_coverage": "none",
                },
            }, status=200)
        return _json(payload)

    @router.get("/health")
    def health() -> dict:
        return {"status": "ok", "plugin": PLUGIN}
