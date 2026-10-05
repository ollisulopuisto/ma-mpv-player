#!/bin/bash
# List mpv's audio devices for one output as ready-to-paste config lines.
# Usage: scripts/list-devices.sh [ao]   (default: coreaudio on macOS, pipewire elsewhere)
set -euo pipefail

if [[ "$(uname -s)" == "Darwin" ]]; then default_ao=coreaudio; else default_ao=pipewire; fi
AO="${1:-$default_ao}"
MPV="${MPV:-$(command -v mpv || true)}"
if [[ -z "$MPV" ]]; then
  echo "mpv not found; install it first." >&2
  exit 1
fi

echo "Devices for --ao=$AO. Put one of these in config.json (or keep \"auto\"):"
"$MPV" --ao="$AO" --audio-device=help 2>/dev/null |
  sed -n "s|^ *'$AO/\\([^']*\\)' (\\(.*\\))\$|  \"audio_device\": \"\\1\"   # \\2|p"
