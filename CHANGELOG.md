# Changelog

Versions use CalVer: `vYY.MM.DD.N`.

## [v26.10.04.5] - 2026-10-04

- Optional Home Assistant webhook: set `ha_webhook_url` and the bridge POSTs `{"state", "channels", "samplerate"}` whenever playback state or the decoded channel count changes, so HA can switch the Denon's surround mode for real 5.1 from the Mac. Off unless configured.

## [v26.10.04.4] - 2026-10-04

- Playback follows the Denon through power changes. Its HDMI audio device vanishes for a moment when the AVR turns on or off, and mpv used to fall back to the Mac's speakers and stay there. With a pinned `audio_device`, the bridge now pauses while the device is gone (MA shows paused) and, when it returns, reopens the output on it and resumes, unless someone paused, stopped or started a track in the meantime. Logged as "Output device … disappeared" / "returned". `auto` is left to mpv.

## [v26.10.04.3] - 2026-10-04

- Fixes silence after installing: the installer now runs the bridge with Homebrew's Python instead of whichever `python3` is first on PATH. A Python without macOS Local Network permission (such as miniconda's) could not fetch MA's stream ("No route to host"), so MA showed a few seconds of play and then stopped. Override with `PYTHON=... ./scripts/install.sh`.

## [v26.10.04.2] - 2026-10-04

- The log in `~/Library/Logs/ma-mpv-player.log` now keeps only mpv's warnings and errors (such as the Denon output disappearing). Its verbose chatter, roughly 22,000 lines a week, appears only when the bridge runs with `--verbose`.

## [v26.10.04.1] - 2026-10-04

First packaged release of the Music Assistant → MPD → mpv bridge for macOS.

- Music Assistant can play to this Mac through its built-in MPD Players provider; mpv decodes the stream and plays it through CoreAudio.
- Runs as the per-user LaunchAgent `com.ollisulopuisto.ma-mpv-player`, installed under `~/Library/Application Support/MA MPV Player`, logging to `~/Library/Logs/ma-mpv-player.log`.
- Settings live in `~/.config/ma-mpv-player/config.json`: listen address and port, mpv path, `audio_device` (`auto`, a CoreAudio UID, or `coreaudio/<name>`) and the Keychain item.
- The MPD password is read from the macOS Keychain (at least 24 characters). `currentsong` now needs the password, so the stream URL is not shown to unauthenticated clients. Stream URLs are redacted from logs.
- Install, uninstall and status scripts; CI runs strict ruff and shellcheck linting, the protocol tests, and plist validation.
