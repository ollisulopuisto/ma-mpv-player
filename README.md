# MA MPV Player

Play [Music Assistant](https://music-assistant.io) to a Mac's own audio
output, typically its HDMI connection to an AV receiver, in full multichannel
(5.1, 7.1) at the source's sample rate.

The bridge looks like an MPD server to Music Assistant's built-in **MPD
Players** provider and drives a local [mpv](https://mpv.io) process. mpv
fetches MA's stream and decodes it straight to CoreAudio.

```text
Music Assistant ──MPD commands──▶ this bridge ──private IPC──▶ mpv
                MA stream URL ─────────────────────────────▶ mpv ──PCM──▶ CoreAudio / HDMI ──▶ AVR
```

## Why

The problem this solves: **reliable multichannel playback of every format
from Music Assistant on a Mac, over HDMI to an AV receiver.** FLAC, WAV, DTS
and hi-res 5.1 sources should arrive at the receiver as real 5.1 PCM at
their own sample rate, every time, with nothing downmixing them to stereo
along the way.

The usual ways to make a Mac a Music Assistant player didn't get there
reliably for us. mpv, which decodes almost anything and opens CoreAudio
multichannel outputs properly, does. Most of the remaining work was making
it dependable around an HDMI receiver, whose audio device vanishes whenever
it powers on, off or changes input.

## Features

- **Multichannel passthrough:** 5.1 sources reach the receiver as 5.1 PCM; no
  downmix, no resampling forced by the bridge.
- **Survives the receiver powering on and off.** An HDMI receiver's audio
  device disappears for a moment whenever it turns on, off or changes input.
  Left alone, mpv falls back to the Mac's speakers or gives up on the track.
  With the receiver's device pinned, the bridge instead:
  - pauses while the device is gone and resumes on it when it returns,
  - retries a track that failed to start because the receiver was still
    waking up, and keeps reporting "playing" to MA meanwhile, so one press of
    play is enough.
- **Home Assistant webhook (optional):** pushes the playback state and the
  decoded channel count, so HA can pick the receiver's sound mode per track.
  (The receiver can't tell: the Mac's HDMI output is always a multichannel
  container, even for stereo.)
- **Runs as a per-user service** (LaunchAgent) and restarts itself if mpv
  crashes.
- **Small and dependency-free:** one Python file, standard library only.

It is deliberately a single-player endpoint, not a general MPD music server,
and it never exposes mpv's unauthenticated IPC socket to the network.

## Requirements

- macOS, with the user logged in (the service is a LaunchAgent)
- [Homebrew](https://brew.sh) with `mpv` and Python 3.10+:
  `brew install mpv python`
- Music Assistant on your network, with its stream URL reachable from the Mac
- An audio output on the Mac, such as HDMI to an AV receiver

Linux: see [Platforms](#platforms).

## Quick start

### 1. Install

```sh
git clone https://github.com/ollisulopuisto/ma-mpv-player.git
cd ma-mpv-player
./scripts/install.sh
```

The installer asks for the MPD password (at least 24 characters) and stores it
in the macOS Keychain, installs the bridge under
`~/Library/Application Support/MA MPV Player`, writes a config to
`~/.config/ma-mpv-player/config.json`, and starts the service.

### 2. Pick the audio device

```sh
./scripts/list-devices.sh
```

Copy the receiver's line into `~/.config/ma-mpv-player/config.json` as
`audio_device`. **Pin the receiver's device** rather than using `auto`: that
is what lets the bridge follow the receiver through power changes instead of
falling back to the Mac's speakers. Also set `listen` to the Mac's LAN address.
Then restart:

```sh
launchctl kickstart -k "gui/$(id -u)/com.ollisulopuisto.ma-mpv-player"
```

### 3. Add the player in Music Assistant

**Settings → Providers → MPD Players:** add a player with the Mac's LAN
address, port `6601` and the password from step 1.

Then, in that player's settings, the recommended setup for a receiver that
Home Assistant knows about:

- **Power Control:** the receiver's HA `media_player`. Pressing play in MA
  turns the receiver on; MA stops playback ~15 s after it turns off (see
  [Home Assistant](#home-assistant) for an immediate stop).
- **Volume Control:** the same entity, so MA's volume slider is the
  receiver's master volume (mpv stays at 100 %). Pick the live entity: a
  disabled or duplicate integration's entity (e.g. `..._2`) has no state and
  reads as 0 %. While the receiver is off, MA shows no volume.
- **Output channels:** multichannel.

### 4. Optional: mute the Mac's built-in speakers

When the receiver turns off, macOS moves the audio to the built-in speakers
for ~40 ms before the bridge can pause it, which can be heard as a crackle.
Mute the built-in output once; macOS remembers mute per device:

```sh
brew install switchaudio-osx
SwitchAudioSource -t output -s "<built-in speakers>" && osascript -e 'set volume output muted true' && SwitchAudioSource -t output -s "<receiver>"
```

## Configuration

`~/.config/ma-mpv-player/config.json`; restart the service after changes.

| Key | Default | Meaning |
| --- | --- | --- |
| `listen` | `0.0.0.0` | Address to serve MPD on. Set the Mac's LAN address to bind only that interface. |
| `port` | `6601` | MPD port. |
| `audio_device` | `auto` | Output device id from `list-devices.sh`, or a full mpv name like `coreaudio/<id>`. `auto` follows the macOS default output and disables device following. |
| `mpv` | `mpv` | Path to mpv. |
| `ao` | `coreaudio` (macOS) | mpv audio output. |
| `ipc_socket` | in `~/Library/Application Support/MA MPV Player/` | mpv's private control socket. |
| `keychain_service`, `keychain_account` | `com.ollisulopuisto.ma-mpv-player`, your user | Keychain item holding the MPD password. |
| `password_file` | none | Alternative to the Keychain: a private (0600) file containing the password. `MA_MPV_PLAYER_PASSWORD` in the environment overrides both. |
| `ha_webhook_url` | none | Home Assistant webhook for state updates; see below. |

The MPD password is required for every playback command and for the current
stream URL. Allow inbound TCP `6601` only from the MA host.

## Home Assistant

[`examples/home-assistant.yaml`](examples/home-assistant.yaml) has example
automations, with placeholders for your entities:

- wake the receiver and select the Mac's input when this player starts,
- stop this player as soon as the receiver turns off,
- receive the bridge's webhook into a helper,
- pick the receiver's sound mode from the decoded channel count.

They were developed against a Denon AVR-X3600H; sound mode names and input
names vary by receiver.

### State webhook

Set `ha_webhook_url` (e.g. `http://homeassistant.local:8123/api/webhook/<id>`)
and the bridge POSTs JSON whenever the state or decoded format changes:

```json
{"state": "play", "channels": 6, "samplerate": 96000}
```

`state` is `play`, `pause` or `stop`; with nothing loaded it sends
`{"state": "stop", "channels": 0, "samplerate": 0}`. Music Assistant passes
the source's channel count through, so stereo reads 2 and 5.1 reads 6.
Changes come in bursts while the output reopens after the receiver wakes, so
debounce in HA (the example waits 3 s). Failed POSTs are logged and retried
on the next change.

## Operating

```sh
./scripts/status.sh                              # process and listener
tail -f ~/Library/Logs/ma-mpv-player.log         # warnings, errors, device changes
```

The log keeps mpv's warnings and errors and the bridge's own events, such as
"Output device … disappeared; pausing". Run the bridge with `--verbose` for
everything.

**Update:** `git pull && ./scripts/install.sh`. After upgrading mpv
(`brew upgrade mpv`), restart the service so it starts the new mpv.

**Uninstall:** `./scripts/uninstall.sh` stops and removes the service and the
installed bridge, and leaves the config and Keychain item for a later
reinstall.

## Troubleshooting

- **MA cannot connect:** check the Mac's LAN address, TCP port `6601`, the
  password, and the macOS and network firewalls.
- **Play fails, or the log shows mpv can't open the stream:** the Mac must
  reach the exact host and port in MA's stream URL. If MA advertises a
  container-only address such as `172.x.x.x`, set MA's advertised stream
  address to its LAN address. A working connection to the bridge doesn't prove
  the stream URL is reachable.
- **Log shows `No route to host` to MA, but the address works from
  Terminal:** macOS Local Network privacy is blocking the Python the service
  runs. The installer prefers Homebrew's Python; pick another with
  `PYTHON=/path/to/python3 ./scripts/install.sh`.
- **Plays from the Mac's speakers, or stays silent, after the receiver turns
  on:** pin `audio_device` to the receiver (not `auto`) so the bridge follows it.
- **Stereo instead of 5.1:** check the MA player's output channels, MA's view
  of the source's channel count, the `audio:` line in MPD `status`
  (rate:bits:channels), and the receiver's input mode.
- **Crackle from the Mac when the receiver turns off:** mute the built-in
  speakers (Quick start, step 4).
- **No sound after the receiver reconnects in some HDMI mode:** check Audio MIDI
  Setup. The bridge doesn't change system-wide HDMI formats;
  [Audio Format Guard](https://github.com/ollisulopuisto/audio-format-guard)
  can.

## Platforms

macOS is the supported, tested platform. The bridge core isn't macOS-specific:
with `ao` set to `pipewire` or `alsa` and the password from
`MA_MPV_PLAYER_PASSWORD` or `password_file`, it should run on Linux under a
systemd user unit. That is **untested** and there is no Linux installer;
reports and contributions are welcome.

## Supported MPD commands

`password`, `status`, `idle`, `noidle`, `clear`, `add`, `play`, `pause`,
`stop`, `seekcur`, `setvol`, `currentsong`, `close` and `ping`: the subset
Music Assistant uses. `status` reports state, position, duration, volume and
the decoded `audio:` format.

## Development

```sh
python3 -m unittest discover -s tests -v
ruff check .
shellcheck -S style scripts/*.sh
plutil -lint launchd/com.ollisulopuisto.ma-mpv-player.plist.example
```

The tests need neither mpv nor an audio device; they use a fake mpv. CI runs
the same on macOS. Changes to device handling still need a check with real
hardware: a receiver turning on and off during playback.

## License

MIT; see [LICENSE](LICENSE).
