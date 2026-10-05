#!/bin/bash
set -euo pipefail
umask 077

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.ollisulopuisto.ma-mpv-player"
APP_DIR="$HOME/Library/Application Support/MA MPV Player"
CONFIG_DIR="$HOME/.config/ma-mpv-player"
CONFIG="$CONFIG_DIR/config.json"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/ma-mpv-player.log"
PYTHON="$("$ROOT/scripts/find-python.sh")"
MPV="$(command -v mpv || true)"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "The MPV CoreAudio player requires macOS." >&2
  exit 1
fi
if [[ -z "$PYTHON" ]]; then
  echo "Install Python 3, then rerun this script." >&2
  exit 1
fi
if [[ -z "$MPV" ]]; then
  echo 'Install MPV with Homebrew (brew install mpv), then rerun this script.' >&2
  exit 1
fi

mkdir -p "$APP_DIR" "$CONFIG_DIR" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cp "$ROOT/bridge.py" "$APP_DIR/bridge.py"
chmod 700 "$APP_DIR" "$CONFIG_DIR"
chmod 700 "$APP_DIR/bridge.py"

if [[ ! -e "$CONFIG" ]]; then
  "$PYTHON" - "$CONFIG" "$MPV" "$HOME" "$(id -un)" <<'PY'
import json
import sys
from pathlib import Path

config_path, mpv, home, account = sys.argv[1:]
Path(config_path).write_text(json.dumps({
    "listen": "0.0.0.0",
    "port": 6601,
    "mpv": mpv,
    "audio_device": "auto",
    "ipc_socket": f"{home}/Library/Application Support/MA MPV Player/mpv.sock",
    "keychain_service": "com.ollisulopuisto.ma-mpv-player",
    "keychain_account": account,
}, indent=2) + "\n")
PY
  chmod 600 "$CONFIG"
fi

SERVICE="$("$PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1]))["keychain_service"])' "$CONFIG")"
ACCOUNT="$("$PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1]))["keychain_account"])' "$CONFIG")"
if ! /usr/bin/security find-generic-password -s "$SERVICE" -a "$ACCOUNT" >/dev/null 2>&1; then
  "$ROOT/scripts/store-password.sh" "$SERVICE" "$ACCOUNT"
fi

"$PYTHON" "$ROOT/scripts/generate-launch-agent.py" \
  "$ROOT/launchd/$LABEL.plist.example" "$PLIST" "$PYTHON" \
  "$APP_DIR/bridge.py" "$CONFIG" "$APP_DIR" "$LOG"
chmod 600 "$PLIST"
plutil -lint "$PLIST"

launchctl bootout "gui/$(id -u)" "$PLIST" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl kickstart -k "gui/$(id -u)/$LABEL"

echo "Installed and started $LABEL."
echo "Edit $CONFIG to pin the audio device (list them with ./scripts/list-devices.sh) and set the LAN bind address."
echo "MA connects to this Mac at <mac-lan-ip>:6601. Use a LAN-only firewall rule."
