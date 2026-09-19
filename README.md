# SMF Cron Night — Hermes Desktop Plugin

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) desktop plugin that answers **what ran last night, what failed, and what each run cost** in one list. Today that lives in three places (`hermes cron list` / `hermes cron runs`, cron session rows, usage audit). This is one pane.

## What it does

- **Sidebar** — Cron Night, plus ⌘K → Open Cron Night
- **Overnight window** — last local night (18:00 yesterday → 08:00 today in the machine TZ)
- **One run list** — name, schedule, times, status, tokens, USD (nullable), error snippet
- **Summary** — N runs · F failed · total cost (sum of recorded USD only)
- **Filters** — All / Failed
- **Status bar** — a fail chip only when last night had failures
- **Degrades** — if `plugin_api.py` is not mounted, Desktop falls back to gateway `cron.manage` + `session.list`. Missing cost stays `null` — it is never invented.

## Install

One shot (covers profiles + Desktop JS + enable):

```bash
git clone https://github.com/smfworks/smf-cron-night.git ~/.hermes/plugins/smf-cron-night
bash ~/.hermes/plugins/smf-cron-night/install.sh
```

Or, if Hermes is already on PATH:

```bash
hermes plugins install smfworks/smf-cron-night --enable
bash "${HERMES_HOME:-$HOME/.hermes}/plugins/smf-cron-night/install.sh"
```

`install.sh` enables the plugin on `$HOME/.hermes` **and** every `profiles/*/plugins` home Desktop may spawn, copies `desktop/plugin.js` into `$HOME/.hermes/desktop-plugins/smf-cron-night/` (what packaged Electron actually loads), and tells you to **quit and relaunch Desktop**.

**⌘K → Reload desktop plugins is JS only.** It does not mount `plugin_api.py`. If Cron Night says **Backend not reachable** and the RPC fallback is empty, the serve process started before enable — quit Desktop and launch it again.

Do **not** run `hermes desktop` to relaunch if you already have the packaged Linux binary. That command rewrites the `.desktop` `Exec=` and can prompt for `chrome-sandbox` sudo. Use the menu entry / `…/linux-unpacked/Hermes --no-sandbox`.

Then:

1. Settings → Plugins → Cron Night → on
2. Sidebar → **Cron Night** (or ⌘K → Open Cron Night)

### Give this to a Hermes agent

```
Install SMF Cron Night from https://github.com/smfworks/smf-cron-night
Run bash ~/.hermes/plugins/smf-cron-night/install.sh (clone first if missing).
Enable on $HOME/.hermes and every profiles/*/ that already has a plugins dir.
Copy desktop/plugin.js to $HOME/.hermes/desktop-plugins/smf-cron-night/.
Do not run hermes desktop. Do not kill this chat from inside it.
Tell me to quit Hermes Desktop and relaunch from the menu so plugin_api.py mounts.
```

## Architecture

```
smf-cron-night/
├── install.sh
├── AGENTS.md
├── plugin.yaml
├── __init__.py
├── dashboard/
│   ├── manifest.json        # api: plugin_api.py
│   └── plugin_api.py        # GET /night  →  /api/plugins/smf-cron-night/night
├── desktop/
│   └── plugin.js            # copy to ~/.hermes/desktop-plugins/smf-cron-night/
└── tests/
    └── test_plugin_api.py
```

`GET /night` reads local Hermes cron storage (no network):

| Path | What we take |
|------|----------------|
| `cron/jobs.json` | name, `schedule` / `schedule_display`, `last_run_at`, `last_status`, `last_error` |
| `cron/executions.db` | attempt ledger: `claimed` → `running` → `completed` \| `failed` \| `unknown` |
| `cron/usage_audit.jsonl` | `prompt_tokens` / `completion_tokens` / `total_tokens` (no USD) |
| `cron/output/{job_id}/{timestamp}.md` | output + error snippet; `{timestamp}.audit.json` when present |
| `state.db` sessions `source=cron` | `cron_{job_id}_{timestamp}` rows; `actual_cost_usd` / `estimated_cost_usd` only if stored |

USD is copied from recorded session billing columns. Token rows without a stored dollar amount stay `usd: null`.

Desktop also tries gateway JSON-RPC (`host.request('cron.manage', {action:'list'})` and `host.request('session.list', {include_hidden:true})`) when the plugin backend is off.

## Tests

```bash
python -m pytest tests/test_plugin_api.py -q
```

## License

MIT — SMF Works
