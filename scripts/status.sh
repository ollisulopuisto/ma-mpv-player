#!/bin/bash
set -euo pipefail

LABEL="com.ollisulopuisto.ma-mpv-player"
PORT="$(python3 -c 'import json, pathlib; p=pathlib.Path.home()/".config/ma-mpv-player/config.json"; print(json.loads(p.read_text())["port"])')"
launchctl print "gui/$(id -u)/$LABEL"
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null; then
  echo "MPD protocol listener is available on TCP $PORT."
else
  echo "No listener found on TCP $PORT." >&2
  exit 1
fi
