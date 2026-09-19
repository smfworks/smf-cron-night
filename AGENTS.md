# SMF Cron Night — agent notes

For Hermes/OpenClaw installing this repo on a user's machine.

## Do

1. Clone to `$HOME/.hermes/plugins/smf-cron-night` (or `hermes plugins install smfworks/smf-cron-night --enable`).
2. Run `bash install.sh` from that tree. It enables the plugin on `$HOME/.hermes` **and** every `profiles/*/plugins` home, then copies `desktop/plugin.js` to `$HOME/.hermes/desktop-plugins/smf-cron-night/`.
3. Tell the user to **quit Hermes Desktop and relaunch from the menu**. The Python API (`plugin_api.py`) mounts only on the next `hermes serve`.

## Do not

- Do not treat ⌘K → Reload desktop plugins as a backend remount. That is JS only. **Backend not reachable** means the serve process predates enable — the page still tries gateway `cron.manage` / `session.list` and degrades (tokens/USD stay null unless those RPCs carry them).
- Do not run `hermes desktop` to relaunch if a packaged Electron binary already exists (`…/linux-unpacked/Hermes --no-sandbox`). `hermes desktop` rewrites the `.desktop` `Exec=` and can prompt for `chrome-sandbox` sudo.
- Do not `hermes serve --stop` (kills every serve on the box). Do not kill this chat's backend from inside the same Desktop window unless the user asked for a relaunch.
- Do not invent dollar amounts. Cost is `actual_cost_usd` / `estimated_cost_usd` from `state.db` when present; `usage_audit.jsonl` has tokens only.
- Do not `git reset --hard` an existing plugin checkout.

## After relaunch

Sidebar **Cron Night**, or ⌘K → Open Cron Night. Optional status-bar chip appears when last night had failures, or when in-window (including spillover hung) jobs are still running. The header lists homes scanned; each row is labeled with its profile. Unreadable cron storage is an error, not a quiet night.
