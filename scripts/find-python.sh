#!/bin/bash
# Print the Python interpreter the LaunchAgent should run.
#
# macOS Local Network privacy is granted per binary. A launchd job running a
# Python that never got the grant (e.g. miniconda's) cannot open LAN
# connections, so mpv fails to fetch MA's stream with "No route to host".
# Prefer Homebrew's Python over whatever comes first on PATH; set PYTHON to
# override.
set -euo pipefail

if [[ -n "${PYTHON:-}" ]]; then
  echo "$PYTHON"
  exit 0
fi
for candidate in "${HOMEBREW_PREFIX:-/opt/homebrew}/bin/python3" /usr/local/bin/python3; do
  if [[ -x "$candidate" ]]; then
    echo "$candidate"
    exit 0
  fi
done
command -v python3 || true
