#!/bin/bash
# One-shot SMF Cron Night install for Hermes Desktop.
#
# Desktop can spawn more than one serve (`--profile default` and the
# active profile). The JS half loads from $HOME/.hermes/desktop-plugins,
# not profiles/*/desktop-plugins. plugin_api.py only mounts on the next
# hermes serve — quit and relaunch Desktop. Do not run `hermes desktop`
# if you already have the packaged Electron binary; that path rewrites
# the .desktop Exec= and can prompt for chrome-sandbox sudo.
set -euo pipefail

REPO="${REPO:-https://github.com/smfworks/smf-cron-night.git}"
NAME=smf-cron-night
APP_HOME="${HOME}/.hermes"
HERMES_BIN="${HERMES_BIN:-hermes}"

if ! command -v "$HERMES_BIN" >/dev/null 2>&1; then
  echo "hermes not on PATH" >&2
  exit 1
fi

homes=("$APP_HOME")
if [[ -n ${HERMES_HOME:-} && $HERMES_HOME != "$APP_HOME" ]]; then
  homes+=("$HERMES_HOME")
fi
shopt -s nullglob
for p in "$APP_HOME"/profiles/*/plugins; do
  homes+=("$(dirname "$(dirname "$p")")")
done
shopt -u nullglob
mapfile -t homes < <(printf '%s\n' "${homes[@]}" | awk 'NF && !seen[$0]++')

install_into() {
  local home=$1
  mkdir -p "$home/plugins"
  echo "==> HERMES_HOME=$home"
  if [[ -L $home/plugins/$NAME ]]; then
    echo "    symlink $home/plugins/$NAME -> $(readlink "$home/plugins/$NAME")"
  elif [[ -d $home/plugins/$NAME/.git ]]; then
    git -C "$home/plugins/$NAME" fetch --depth 1 origin main
    git -C "$home/plugins/$NAME" merge --ff-only FETCH_HEAD
  else
    env -u HERMES_PROFILE HERMES_HOME="$home" "$HERMES_BIN" plugins install "$REPO" --enable --no-deps || \
      git clone --depth 1 "$REPO" "$home/plugins/$NAME"
  fi
  env -u HERMES_PROFILE HERMES_HOME="$home" "$HERMES_BIN" plugins enable "$NAME" --no-allow-tool-override
}

src=""
for home in "${homes[@]}"; do
  install_into "$home"
  if [[ -f $home/plugins/$NAME/desktop/plugin.js ]]; then
    src="$home/plugins/$NAME/desktop/plugin.js"
  elif [[ -L $home/plugins/$NAME ]]; then
    candidate="$(readlink -f "$home/plugins/$NAME")/desktop/plugin.js"
    [[ -f $candidate ]] && src=$candidate
  fi
done

if [[ -z $src ]]; then
  echo "desktop/plugin.js not found" >&2
  exit 1
fi

mkdir -p "$APP_HOME/desktop-plugins/$NAME"
cp -f "$src" "$APP_HOME/desktop-plugins/$NAME/plugin.js"
echo "==> copied JS -> $APP_HOME/desktop-plugins/$NAME/plugin.js"

echo
echo "DONE. The Python API is not live in this Desktop process."
echo "Quit Hermes Desktop and launch it again from the menu / packaged Electron --no-sandbox."
echo "Do not run: hermes desktop"
echo "Then: Ctrl+K → Open Cron Night."
