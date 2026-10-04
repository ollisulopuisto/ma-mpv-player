#!/bin/bash
set -euo pipefail

LABEL="com.ollisulopuisto.ma-mpv-player"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
APP_DIR="$HOME/Library/Application Support/MA MPV Player"

launchctl bootout "gui/$(id -u)" "$PLIST" >/dev/null 2>&1 || true
rm -f "$PLIST"
rm -rf "$APP_DIR"
echo "Removed the MA MPV Player LaunchAgent and installed bridge."
echo "Config and Keychain entry were kept. Remove them manually if desired:"
echo "  rm -rf '$HOME/.config/ma-mpv-player'"
echo "  security delete-generic-password -s com.ollisulopuisto.ma-mpv-player -a '$(id -un)'"
