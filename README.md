# MA MPV Player

A small macOS audio player endpoint for Music Assistant. It speaks the MPD
command subset used by MA's built-in **MPD Players** provider and controls a
local `mpv` process through a private Unix socket. MPV fetches MA's stream over
the network and sends decoded audio directly to CoreAudio.

```text
Navidrome ──source──▶ Music Assistant ──MPD commands──▶ this bridge ──IPC──▶ mpv
                                      MA stream URL ───────────────────────▶ mpv
mpv ──decoded PCM──▶ CoreAudio / Mac HDMI ──▶ AVR
```

This project does not replace MA's player provider, host Navidrome, or expose
MPV's unauthenticated IPC socket to the network. It does not require Docker.
The bridge is intentionally a single-player endpoint, not a general MPD music
server.

## Requirements

- macOS with a logged-in user session (the service runs as a LaunchAgent)
- Python 3.10 or newer
- MPV with FFmpeg audio decoding support (`brew install mpv`)
- A CoreAudio output, such as the Mac's HDMI connection to the AVR
- Network access from the Mac to MA's advertised audio stream URL and port

## Install

Clone this repository on the Mac, then run:

```sh
./scripts/install.sh
```

The installer checks for MPV and Python, asks macOS Keychain to store the MPD
password, installs the bridge under `~/Library/Application Support/MA MPV Player`,
and starts a per-user LaunchAgent. Enter the same password in the MA
MPD Player provider. Use at least 24 characters.

Find CoreAudio output device names and UIDs with:

```sh
mpv --audio-device=help
```

Edit `~/.config/ma-mpv-player/config.json` and set `audio_device` to `auto`, a
CoreAudio device UID, or `coreaudio/<device-name>`. Set `listen` to the Mac's
LAN address to bind only that interface. The default `0.0.0.0` listens on all
interfaces; allow inbound TCP port `6601` only from the MA host in your network
firewall. The MPD password is required for all playback controls and current
track URLs.

After changing configuration, restart the service:

```sh
launchctl kickstart -k "gui/$(id -u)/com.ollisulopuisto.ma-mpv-player"
```

To update the installed bridge, pull the repository changes and rerun
`./scripts/install.sh`; it replaces the installed script and restarts the agent.

Check its process and listener with:

```sh
./scripts/status.sh
tail -f "$HOME/Library/Logs/ma-mpv-player.log"
```

## Add it in Music Assistant

In MA, add a player under **Settings → Providers → MPD Players**. Enter the
Mac's LAN address and port `6601`, then enter the Keychain password you stored
during installation. Choose this player as MA's output.

MPV runs on the Mac, so it fetches the stream URL that MA sends it. The Mac
must be able to reach that exact advertised host and port. If MA advertises a
container-only address such as `172.x.x.x`, configure MA's advertised stream
address/port to use the HA host's reachable LAN address before playback.

The bridge uses MPV's `--audio-channels=auto` and CoreAudio output. It does not
force a sample rate or use DTS passthrough: MPV/FFmpeg decodes DTS to PCM, and
CoreAudio negotiates the output. Use Audio Format Guard separately if HDMI
needs to be reset after the AVR turns off or changes modes.

## Home Assistant and Navidrome companions

- Navidrome scans and serves the library. Its DTS metadata patch is maintained
  separately; it makes DTS-in-WAV files report their true channel count.
- The HA blueprint can switch the Denon input when this MA player starts. Set
  the blueprint's MA player and input-select entity in Home Assistant.
- Audio Format Guard manages Mac HDMI device state. It is a separate app and
  is not installed or controlled by this bridge.

Keeping those projects separate lets each follow its own upstream release and
update cycle while MA remains the shared control plane.

## Supported commands

The bridge implements the MPD commands MA uses: `password`, `status`, `idle`,
`clear`, `add`, `play`, `pause`, `stop`, `seekcur`, `setvol`, `currentsong`,
`close`, and `ping`. It reports MPV's play/pause/stop state, position, duration,
volume, and decoded audio sample rate, bit depth, and channel count.

## Home Assistant state webhook

Optional. Set `ha_webhook_url` in the config (e.g.
`http://192.168.1.202:8123/api/webhook/<id>`) and the bridge POSTs JSON to it
whenever the player state or decoded format changes:

```json
{"state": "play", "channels": 6, "samplerate": 48000}
```

`state` is `play`, `pause` or `stop`; with no track loaded it sends
`{"state": "stop", "channels": 0, "samplerate": 0}`. `channels` is what mpv
decoded from MA's stream, which the AVR cannot see because the Mac's HDMI
output is always a multichannel PCM container. Bursts of changes (e.g. while
the output reopens after the AVR powers on) are sent as they happen, so
debounce in HA. Failed POSTs are logged and retried on the next change.

## Troubleshooting

- **MA cannot connect:** check the Mac LAN address, TCP port `6601`, the
  Keychain password, and the macOS/network firewall.
- **MA says play failed or MPV cannot load the track:** test that the Mac can
  reach MA's advertised stream URL and port. A successful connection to the
  bridge alone does not prove the stream URL is reachable.
- **Log shows `No route to host` to MA, but the same address works from
  Terminal:** macOS Local Network privacy is blocking the Python the
  LaunchAgent runs. The installer prefers Homebrew's Python; to pick another,
  rerun it as `PYTHON=/path/to/python3 ./scripts/install.sh`.
- **Stereo instead of 5.1:** check MA's source channel count, the MPD `audio`
  status (sample rate, bit depth, channels), and the AVR's input mode. For DTS tracks, refresh
  the Navidrome provider in MA after Navidrome reindexes them.
- **Crackle from the Mac's speakers when the AVR turns off:** macOS moves
  mpv to the built-in speakers for ~40 ms before the bridge can pause it.
  Mute the built-in output once in macOS (mute is remembered per device):
  `SwitchAudioSource -t output -s "<built-in speakers>" && osascript -e 'set volume output muted true' && SwitchAudioSource -t output -s "<AVR>"`
  (`brew install switchaudio-osx`).
- **No sound after the AVR reconnects:** check Audio MIDI Setup and Audio
  Format Guard; the bridge does not change system-wide HDMI formats.

## Development

Run the protocol and helper tests without MPV or an audio device:

```sh
python3 -m unittest discover -s tests -v
for script in scripts/*.sh; do bash -n "$script"; done
plutil -lint launchd/com.ollisulopuisto.ma-mpv-player.plist.example
```

The tests use a fake MPV endpoint. Verify real CoreAudio multichannel output on
the target Mac/AVR before changing the MPV or macOS audio configuration.

## License

MIT — see [LICENSE](LICENSE).

## Uninstall

```sh
./scripts/uninstall.sh
```

Uninstall stops and removes this LaunchAgent and installed bridge. It leaves
the JSON config and Keychain item in place so reinstalling does not lose them.
