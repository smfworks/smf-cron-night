# Opposition: do not trust SMF Cron Night for overnight ops yet

Adversarial review of `smfworks/smf-cron-night` at `760ca41` (plugin #1). No product
fixes in this PR. Evidence is from `desktop/plugin.js`, `dashboard/plugin_api.py`,
`install.sh`, `tests/test_plugin_api.py`, and Hermes cron storage as it exists
upstream (job id `uuid.uuid4().hex[:12]`, session id `cron_{job_id}_%Y%m%d_%H%M%S`,
`usage_audit.ts` via `_utcnow_iso_ms()`, output files
`{job_id}/%Y-%m-%d_%H-%M-%S.md`).

Pytest: `python` is not on PATH in this environment. `python3 -m pytest tests/ -q`
→ **16 passed** in 0.06s after `pip install pytest`. Those 16 tests do not cover
the trust breakers below.

---

## Addressed in Honest night

Follow-up PR (this tree after `760ca41` / opposition `c9a4add`): window, timezone,
unread vs empty, and the cheap honesty wins. **Not a full cost redesign or
profile ACL.**

| Item | What changed |
|------|----------------|
| **P0.1** unread vs empty | Disk I/O/parse failures go in `errors[]` with `ok: false` and `read_status: unread\|partial`. Empty successful read stays `ok: true`. Desktop `ErrorState` / banner — never “Nothing ran last night.” REST unread empty no longer hides RPC that listed jobs. Fail chip may stay off on unread. |
| **P0.2** 18:00 window | If local now ≥ 18:00, window is *today* 18:00 → *tomorrow* 08:00. Before 18:00: yesterday 18:00 → today 08:00. Same in Python `overnight_window` and JS `overnightWindow`. |
| **P0.3** serve TZ vs Desktop | `fetchNight` always sends `?tz=` from `Intl` / `resolvedOptions().timeZone`. `plugin_api.py` honors it for bounds. Omitted/invalid `tz` → serve process local zone (documented). |
| **P0.4** (cheap) unknown vs ok | Missing / unmapped `end_reason` → `unknown`. Unaudited markdown without `(FAILED)` → `unknown`, not `completed`. |
| **P0.5** (cheap) partial USD | Summary omits `usd` when coverage is incomplete and labels `partial (k/n billed)`. Bare `$` total only when every in-window run has USD. |
| Status-bar running | Chip for in-window `running`/`claimed` when there are no failures. |

Still open: full P0.5 cost merge/estimate redesign, P0.6 profile isolation, dual-aggregator rewrite, CI.

---

## 1. Executive opposition

I would not trust this pane for a Spark morning. It can render **“Nothing ran last
night”** with `ok: true` when it could not read the ledger, when the Python window
is in the wrong timezone, or when tonight’s fires started after 18:00 and you
opened the pane before midnight. Failures with `end_reason=None` or a markdown
file that lacks `(FAILED)` paint **ok**. A status-bar chip that is *absent* is
treated as all-clear; it is also what you get on unread data. Summary `$0.02`
is a sum of whichever rows happened to carry USD, not the night’s bill.
`GET /night` mixes every local profile into one list; the RPC fallback sees only
the connected gateway. Two code paths, two universes, one green empty state.

---

## 2. P0 — trust breakers (must-fix)

### P0.1 Empty list means “quiet night,” not “couldn’t read”

The pane lies by omission. Every disk reader swallows I/O and returns `[]`:

- `load_jobs_file` (`dashboard/plugin_api.py`) — corrupt / unreadable
  `cron/jobs.json` → `[]` (`OSError`, `JSONDecodeError`, `UnicodeDecodeError`).
- `load_executions_db` — missing table, locked DB, URI failure → `[]`.
- `load_cron_sessions` — no `source` column → `[]`.
- `load_usage_audit` — missing file or bad JSONL lines skipped.

`night_payload` then always sets `"ok": True`, `"degraded": False`, `"source":
"disk"` even when every home failed to open. Example: replace `jobs.json` with
`{not json`. `load_jobs_file` returns `[]`. UI (`CronNightPage`) hits
`EmptyState` title **“Nothing ran last night”**.

The JS path is worse. `loadFromHost` catches `cron.manage` and `session.list`
failures and still returns `{ ok: true, runs: [], source: 'rpc', degraded: true }`.
It **cannot throw**. `fetchNight` therefore almost never reaches `ErrorState`.
Sibling `smf-app-launcher` shows **Backend not reachable** on REST failure; Cron
Night shows a quiet night plus an easy-to-miss “RPC fallback” badge.

`fetchNight` prefers REST whenever `rest.ok !== false && Array.isArray(rest.runs)`.
A mounted backend that scanned the wrong/empty home (`{ ok: true, runs: [] }`)
**discards** RPC data that listed real jobs. Disk silence wins.

`GET /night` exception handler returns **HTTP 200** `{ ok: false, error }`
(`plugin_api.py` `night()`). `/health` is unconditionally `{ status: "ok" }`.
Nothing in the UI distinguishes unread from zero fires. The fail chip
(`FailChipHost`) is `null` when `summary.failed` is 0 — including when `data` is
an empty success.

### P0.2 The 18:00→08:00 window is not open between 18:00 and 24:00

`overnight_window` (`plugin_api.py`) and `overnightWindow` (`plugin.js`) both
compute *yesterday 18:00 → today 08:00* on **every** clock time. The
`if current < morning` branch is dead: both arms assign the same `start`/`end`.

Reproduced against the shipped function (`America/Los_Angeles`):

| now                         | window                                      | tonight 18:30 included? |
|-----------------------------|---------------------------------------------|-------------------------|
| 2026-09-19 08:30 (Spark AM) | Sep 18 18:00 → Sep 19 08:00                 | n/a (correct for AM)    |
| 2026-09-19 15:30            | Sep 18 18:00 → Sep 19 08:00                 | n/a                     |
| 2026-09-19 18:30            | Sep 18 18:00 → Sep 19 08:00                 | **no**                  |
| 2026-09-19 23:00            | Sep 18 18:00 → Sep 19 08:00                 | **no**                  |
| 2026-09-19 03:15            | Sep 18 18:00 → Sep 19 08:00                 | in-progress (ok)        |

At 23:00 a job that fired at 18:30 **tonight** is out of window; last calendar
night is still on screen. EmptyState copy (“Jobs still scheduled will show up
after they fire”) is false — they fired, the window did not move. Tests cover
03:15, 08:00, 15:30 only (`test_overnight_window_*`). There is no 18:30 / 22:00
case.

`filter_runs_in_window` / `filterWindow` include a run if **either** `started_at`
**or** `finished_at` is inside the window:

- Job started 17:00, finished 18:01 → **included** (daytime noise).
- Job started 17:00, still `running` at 03:15 with `finished_at=None` →
  **dropped**. Overnight hung work that began before 18:00 is invisible.
- Exclusive end: `start <= dt < end` drops an 08:00:00 tick (`test_filter_runs`
  already asserts `late` at 08:00 is out).

JS duplicates the same dead `if (current < morning)` (`plugin.js`
`overnightWindow`).

### P0.3 Serve timezone ≠ Desktop timezone; `/night` never sends `tz`

`night()` accepts `tz` and `overnight_window` is documented as “last *local*
night.” `fetchNight` calls `ctx.rest('/night')` with **no query string**.
`resolve_tz()` then uses `datetime.now().astimezone().tzinfo` — the **serve
process** TZ, often UTC on a systemd gateway.

Reproduced: serve TZ=UTC at 15:30 UTC (08:30 PDT Spark morning):

- Window = 18:00 UTC yesterday → 08:00 UTC today (11:00 PDT → 01:00 PDT).
- Job at **02:00 PDT** = 09:00 UTC → **out of window** (real overnight dropped).
- Job at **12:00 PDT** = 19:00 UTC → **in window** (daytime included).

The pane still labels the window “Last night” plus whatever `window.tz` the
serve reports. A Spark operator reading Pacific wall-clock jobs will make a bad
call. JS fallback uses `Date#setHours` in the Electron TZ and
`Intl.DateTimeFormat().resolvedOptions().timeZone` only as a **label** — so
backend-on vs RPC-fallback can disagree on which hours count.

Naive ISO (`parse_datetime`: no offset → `default_tz`; JS `new Date("2026-09-18T23:15:02")`
engine-local vs ES5-UTC) plus output filenames that Hermes writes with
`_hermes_now().strftime('%Y-%m-%d_%H-%M-%S')` (local) vs `usage_audit.jsonl`
`ts` from `_utcnow_iso_ms()` (`…Z`) is a second skew if serve TZ is not the
machine the files were written on.

### P0.4 Failures paint completed / ok

**Sessions.** `collect_home_runs` session branch:

```python
status = "failed" if reason in {"error", "failed", "fail"} else "completed"
```

when `ended_at` is set. `end_reason=None` (the test fixture in
`test_session_actual_cost_is_used_when_present`) becomes **`completed`**. Hermes
reasons such as `timeout`, `cancelled`, `killed`, `delivery_failed` also become
ok. JS is slightly different (`end_reason.includes('fail') || === 'error'`) so
`delivery_failed` fails on RPC and succeeds on disk.

**Output markdown.** `load_output_runs`: if audit status is missing, any `.md`
without `(FAILED)` or a parsed `## Error` is **`completed`**. A truncated write,
empty success body, or `# Cron Job: x (failed)` in unexpected casing without the
error section still goes green. Hermes’ real failure template uses `(FAILED)`
and `## Error` — partial files do not.

**usage_audit.** `"status": "failed" if rec.get("error") else "completed"`. A
token line with `"error": null` is a successful run even when the execution
ledger later marks `unknown`.

**jobs.json last_run.** One `last_run_at` / `last_status` row per job. A night
with fail-then-retry shows only the last write. Earlier `failed` attempts exist
in `executions.db` only if that file opened (see P0.1) and survived the 120s
merge (see P0.5).

`statusMeta('completed')` → Badge **ok**, `StatusDot` tone `good`. That is what
the morning glance trusts.

### P0.5 Cost can be understated, estimated, or double-counted

The code does **not** invent a USD *rate*. It still lies about the night’s bill.

- `normalize_cost` copies `estimated_cost_usd` if `actual_cost_usd` is missing
  (`cost_status` not in `{unknown, n/a, na, included}`). UI `fmtUsd` renders
  `$0.12` with no “est.” `cost_status: "estimated"` is not skipped.
  `test_normalize_cost_uses_recorded_actual_usd_only` only checks the case
  where **both** actual and estimated exist.
- `summarize_runs` / `summarize` sum USD only where `usd != null`, then the
  header shows that number as the total (`SummaryLine`: if `summary.usd != null`
  push `fmtUsd`, else tokens, else “cost unknown”). Ten overnight fires, one
  session row with `actual_cost_usd=0.02`, nine with null → **`$0.02`**. Partial
  is indistinguishable from complete. Same for tokens.
- Merge window is **120 seconds** (`MERGE_WINDOW_SECONDS` / `MERGE_WINDOW_MS`).
  `_closest_partner` compares any of `{started_at, finished_at}`. A 5-minute job
  **with** `finished_at` near audit `ts` merges (reproduced: 1 row, tokens=100).
  The same job **without** `finished_at` (crash / `unknown` / RPC session still
  running) vs audit `ts` 180s later → **2 rows**. Reproduced: execution
  `21:00` + audit `21:03` → two runs, summary `runs: 2`, tokens on the ghost
  row. `usage_audit` is written at **end** of the fire (`scheduler.py`
  `_write_usage_audit` after the agent returns); claimed/started is at the
  beginning. Long briefings will miss the merge whenever finished_at is absent.
- JS `normalizeCost` does **not** add `cache_read_tokens` / `cache_write_tokens`.
  Python session path does. Disk vs RPC token totals disagree for the same
  session row.
- Python `normalize_cost` key `"total"` can bind an unrelated total. JS same.
- RPC `loadFromHost` never reads `usage_audit.jsonl` or `executions.db`. Cost
  is whatever `session.list` carried. `degraded: true` is a badge, not a
  “USD omitted on purpose” banner; if a session row has `estimated_cost_usd`,
  the fallback still shows dollars.

### P0.6 Multi-profile: leak on disk, blind on RPC, mixed in one pane

`discover_hermes_homes` always unions `HERMES_HOME`, `~/.hermes`, and
`~/.hermes/profiles/*` (plus siblings if `HERMES_HOME` is already a profile).
`night_payload` concatenates every home’s runs. There is no current-profile
filter, no consent, no redaction.

- **Leak:** a WisdomForge kid profile’s Desktop, or any serve that mounted this
  API, lists **parent/default** cron names, error snippets (240 chars of
  exception text — often prompts, URLs, keys), and session cost. `_profile_name`
  only labels the row.
- **Blind:** RPC `host.request('cron.manage', { action: 'list' })` is the
  connected gateway’s store. Hermes Bot-Mode issue #37 already documents that
  this RPC sees `~/.hermes/cron/jobs.json` and **not**
  `profiles/<bot>/cron/`. `session.list` is the same process. Backend-off → one
  profile. Backend-on → all profiles. Same sidebar item.
- `_HOME_MARKERS` includes `.env`. A `profiles/foo` that is not a Hermes home
  but has an `.env` is scanned for `cron/` and `state.db`.
- `load_output_runs` `iterdir()` follows directory symlinks under
  `cron/output/`. Timestamp-shaped files there become runs (low odds of
  `/etc` leaking; still reads whatever that tree contains).
- Query `start`/`end` on `GET /night` are unauthenticated local API over **all**
  homes. No path traversal via `ctx.rest` (SDK rejects `..`); the damage is
  over-read of sibling profiles, not plugin-namespace escape.

`install.sh` only enables homes that already have `profiles/*/plugins`. A
profile that **runs cron** but has no `plugins/` dir is skipped for enable, yet
still ingested by `discover_hermes_homes` once *any* serve mounts the API.

---

## 3. P1 — gaps / correctness

- **Two aggregators already disagree.** Window, cost, merge, status, and
  session parsing are copied in JS and Python. Known deltas: cache tokens
  (P0.5), `end_reason` matching (P0.4), merge uses started+finished in Python vs
  started-only partner search in JS (`mergeRuns` skips when one side lacks
  `started_at` unless **both** lack it). RPC has no output/audit/ledger.
- **`jobs.json` last_run is not a night’s history.** `cron.manage` list +
  `if (!job.last_run_at) continue` in `loadFromHost` emits at most one synthetic
  row per job. Multiple overnight ticks become one, unless `session.list`
  still has each `cron_{id}_*` row.
- **Truncation.** `load_cron_sessions`: `LIMIT 500` and **no `ORDER BY`**.
  SQLite may return oldest cron sessions; last night drops off a long-lived
  Spark `state.db`. `load_executions_db`: `ORDER BY claimed_at DESC LIMIT 500`
  with mixed ISO/epoch `claimed_at` sorts wrongly. RPC `session.list`
  `{ limit: 200 }` — sort order not specified by this plugin.
- **Session id regex** `^cron_([A-Za-z0-9]+)_(.+)$` matches Hermes
  `cron_{hex12}_{YYYYMMDD_HHMMSS}`. Tests use dashed `2026-09-19_02-00-00`,
  which is **not** the upstream format. Hyphenated or underscored job ids
  would truncate `job_id` at the first underscore (create path uses hex[:12],
  so this is latent).
- **Hung `running` / `claimed`.** Counted in `summary.runs`, not in
  `summary.failed`. Fail chip stays hidden. Filter “Failed” hides them.
  Morning glance: “1 run · 0 failed”.
- **`ok_silent` → completed.** Hermes silent-marker successes look like real
  work completed. Fine if you want scheduler truth; bad if “ok” means “delivered.”
- **Poll 8s** (`POLL_MS`) always fires `loadFromHost` *then* REST. SDK warns
  not to poll `host.request` faster than a few seconds; this does both, even
  when REST is healthy.
- **Enable ≠ mount.** Settings → Plugins is renderer-only (Hermes SDK caution,
  GHSA-mcfc-hp25-cjv7). `plugin_api.py` mounts on next `hermes serve`.
  `defaultEnabled: true` plus `install.sh` copying JS into
  `~/.hermes/desktop-plugins/smf-cron-night/plugin.js` makes the route live
  immediately on ⌘K Reload (JS only). The page then takes the RPC empty-success
  path (P0.1). README says this; the UI does not.
- **`install.sh` stale / last-writer JS.** Loop copies `desktop/plugin.js`
  from whichever home was last in `homes[]`. Divergent checkouts per profile
  → arbitrary JS version against whichever serve mounted Python. `cp -f` to
  `$HOME/.hermes/desktop-plugins/` only — correct per SDK, but a later
  `git pull` in `plugins/smf-cron-night` without re-running install.sh leaves
  **stale JS** talking to new Python (or the reverse). `git merge --ff-only`
  aborts the whole script on a dirty clone (`set -e`).
- **Profiles without `plugins/`.** `for p in "$APP_HOME"/profiles/*/plugins`
  never enables those homes. Desktop can still spawn them.
- **No JS tests, no I/O tests, no TZ tests, no 18:00+ tests, no merge-miss
  tests, no multi-home tests.** The 16 passing tests are the happy path the
  author believed.

---

## 4. P2 — improvements

- Deduplicate window/cost/merge into one fixture tested from Python; JS should
  consume `/night` only (RPC as error, not a second aggregator).
- Surface `homes[]`, per-home read errors, `cost_coverage` (billed_runs /
  runs), and window `tz` in the header — not only in JSON.
- Pass Desktop `Intl` TZ and an explicit `as_of` timestamp into `/night`.
- Treat `unknown` as its own chip (“n unknown”), not a silent increment of
  “failed” (`_TERMINAL_FAILED` includes `unknown`).
- Bound `error_snippet` against secret-shaped tokens; 240 chars of provider
  exceptions is how keys leak across profiles (P0.6).
- Refuse to follow symlinks out of `cron/output/` (`Path.resolve()` prefix
  check).
- Tighten `_HOME_MARKERS` (require `cron/` or `state.db`, not `.env` alone).
- `GET /night` should not return HTTP 200 on unhandled exception; `/health`
  should try opening `executions.db` read-only.
- Add CI: `python -m pytest tests/ -q` (this repo has no workflow). The
  environment here has `python3` only.
- Consider `defaultEnabled: false` until the first successful `/night` with
  `source: "disk"`.

---

## 5. Quick wins (≤ 1 day)

1. **Move the window after 18:00.** If `current >= today 18:00`, start = today
   18:00, end = tomorrow 08:00. Add tests at 18:30 and 23:00 with a job at
   18:45. Kill the dead `if current < morning` in both languages.
2. **Send `tz`.** `ctx.rest('/night?tz=' + encodeURIComponent(Intl.DateTimeFormat().resolvedOptions().timeZone))`.
   Add a test: `overnight_window` in UTC must not keep 02:00 America/Los_Angeles
   when the operator’s tz is LA — unless `tz_name` is LA.
3. **Unread ≠ empty.** If `jobs.json` exists but fails to parse, or
   `executions.db` exists but `sqlite3.Error`, record `errors[]` and set
   `ok: false` or `degraded: true` with `read_status: "partial"|"unread"`.
   UI: `ErrorState` / banner, never “Nothing ran last night.” Do not prefer
   REST `{ runs: [] }` over RPC that listed jobs.
4. **Stop painting unknown as ok.** `end_reason` missing or unmapped →
   `status: "unknown"` (already in the rank table). Output `.md` without audit
   and without `(FAILED)` → `unknown`, not `completed`.
5. **Honest cost line.** If any in-window run has `usd == null` while another
   has a number, render `cost partial · $0.02 (1/10 billed)` or omit USD and
   keep `cost unknown`. Never show a bare `$` total. Skip
   `estimated_cost_usd` unless labelled.
6. **Fail chip for hung work.** Count `running`/`claimed` that started in-window
   and have no finish after 08:00, or show a separate “n still running” chip.
7. **Tests the current file avoids.** Corrupt `jobs.json`; 3-minute job without
   `finished_at` vs audit `ts` (expect 1 run or an explicit unmerged flag);
   `discover_hermes_homes` does not require mixing sibling profiles without a
   flag.

---

## 6. Suggested next ship (one follow-up PR)

**Title:** Honest night — window, timezone, unread vs empty.

**Scope (one PR, no new features):**

1. Fix `overnight_window` / `overnightWindow` so 18:00–24:00 is the night that
   just started; keep 00:00–08:00 as in-progress; keep 08:00–18:00 as last
   closed night.
2. `fetchNight` always passes Desktop TZ; Python must not silently use serve UTC.
3. Disk readers return structured errors; `night_payload` cannot say `ok: true`
   when a present file was unreadable; `CronNightPage` uses `ErrorState` for
   unread and EmptyState only when `read_status === "ok"` and `runs.length === 0`.
4. Tests for (1)–(3) in `tests/test_plugin_api.py` plus the 18:30 / UTC-vs-LA /
   corrupt-JSON cases in §5.

Do **not** expand into a cost-redesign or profile-ACL in that PR. After it
merges, the next opposition item is P0.5 (partial USD) + P0.6 (profile
isolation) as a second PR.

---

## 7. What already holds (do not redo)

- **No token→USD pricing table.** `normalize_cost` / `normalizeCost` copy
  recorded fields only; missing stays `null`, not `0`
  (`test_normalize_cost_never_invents_usd`,
  `test_normalize_cost_does_not_coerce_missing_to_zero`).
- **`cost_status` `unknown` / `n/a` / `included` clears USD** (Python and JS).
- **Real Hermes shapes.** Job ids hex[:12]; sessions `source='cron'` /
  `cron_{id}_…`; `executions.db` status check constraint
  `claimed|running|completed|failed|unknown`; usage audit tokens-only JSONL;
  output `{job_id}/{timestamp}.md` + `.audit.json`. RPC method `cron.manage`
  exists on the gateway.
- **Disk ESM is valid.** `jsx`/`jsxs` from `react/jsx-runtime`; imports only
  `@hermes/plugin-sdk` + that runtime (SDK: only three specifiers resolve).
  Imported kit names (`SegmentedControl`, `StatusDot`, `STATUSBAR_AREAS`,
  `fmtDateTime`, …) are documented SDK exports — this is not a JSX/import
  footgun, unlike a `.jsx` disk file would be.
- **Install script knows the Desktop footguns.** Comments and README forbid
  `hermes desktop` on packaged Electron, warn that Reload is JS-only, copy JS
  to `~/.hermes/desktop-plugins/` (not `profiles/*/desktop-plugins`). Same
  pattern as `smf-app-launcher`.
- **Merge prefers failure.** `_prefer_status` / `preferStatus` ranks
  `failed > unknown > running > claimed > completed`, so a collapsed
  fail+ok pair stays failed (the inverse problem is hiding the retry, P1).
- **Theme vars, 240-char error cap, nullable cost in the type.** Fine.
- **The 16 tests that exist are correct for what they claim** (LA window at
  03:15/08:00/15:30, status literals, “don’t invent USD when no dollar field”).
  They are not a night-ops acceptance suite.

---

## Appendix — pytest

```
python3 -m pytest tests/ -q
................                                                         [100%]
16 passed in 0.06s
```

`python -m pytest tests/ -q` → `python: command not found` on this agent image.
No GitHub Actions workflow in-tree, so merge green does not mean CI green.
