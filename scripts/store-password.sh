#!/bin/bash
set -euo pipefail

SERVICE="${1:-com.ollisulopuisto.ma-mpv-player}"
ACCOUNT="${2:-$(id -un)}"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This command requires macOS Keychain." >&2
  exit 1
fi

echo "Enter the MPD password configured for this player in Music Assistant."
echo "Keychain will prompt securely; use at least 24 characters."
exec /usr/bin/security add-generic-password \
  -U \
  -s "$SERVICE" \
  -a "$ACCOUNT" \
  -w
