#!/usr/bin/env python3
"""Expose the MPD command subset used by Music Assistant and play it with mpv."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import re
import shlex
import subprocess
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

LOGGER = logging.getLogger("mpv_mpd_bridge")
MPD_VERSION = "0.24.0"
# mpv log-file lines look like "[   0.112][e][ao/coreaudio] ..."; group 1 is the level.
MPV_LOG_LEVEL = re.compile(r"\[\s*[\d.]+\]\[([a-z])\]")
OBSERVED_PROPERTIES = (
    "pause",
    "time-pos",
    "duration",
    "idle-active",
    "volume",
    "audio-params",
    "media-title",
    "audio-device-list",
)


class MPVError(RuntimeError):
    """An mpv IPC request failed."""


class MPVClient:
    """Manage one mpv process through its private Unix JSON IPC socket."""

    def __init__(
        self, executable: str, device_uid: str, socket_path: Path, debug: bool = False
    ) -> None:
        self.executable = executable
        self.device_uid = device_uid
        self.socket_path = socket_path
        self.debug = debug
        self.process: asyncio.subprocess.Process | None = None
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.reader_task: asyncio.Task[None] | None = None
        self.stderr_task: asyncio.Task[None] | None = None
        self.pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self.next_request_id = 1
        self.request_lock = asyncio.Lock()
        self.on_change: Any = None
        # Following the pinned output device (see _handle_device_list).
        self.output_present: bool | None = None
        self.paused_for_output = False
        self.last_load: str | None = None
        self.load_retried = False
        self.reload_on_output: str | None = None
        self.background_tasks: set[asyncio.Task[None]] = set()

    async def start(self, on_change: Any) -> None:
        """Start mpv and wait for its IPC endpoint."""
        self.on_change = on_change
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self.socket_path.unlink(missing_ok=True)
        mpv_args = [
            self.executable,
            "--no-config",
            "--idle=yes",
            "--no-terminal",
            "--ytdl=no",
            "--ao=coreaudio",
            "--audio-channels=auto",
            f"--input-ipc-server={self.socket_path}",
            f"--msg-level=all={'debug' if self.debug else 'warn'}",
            "--log-file=/dev/stderr",
        ]
        if self.device_uid != "auto":
            device = self.device_uid
            if not device.startswith("coreaudio/"):
                device = f"coreaudio/{device}"
            mpv_args.insert(6, f"--audio-device={device}")
        self.process = await asyncio.create_subprocess_exec(
            *mpv_args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        deadline = asyncio.get_running_loop().time() + 10
        while asyncio.get_running_loop().time() < deadline:
            if self.process.returncode is not None:
                raise MPVError(f"mpv exited during startup ({self.process.returncode})")
            try:
                self.reader, self.writer = await asyncio.open_unix_connection(self.socket_path)
                break
            except (FileNotFoundError, ConnectionRefusedError):
                await asyncio.sleep(0.1)
        else:
            raise MPVError("Timed out waiting for mpv IPC socket")

        self.reader_task = asyncio.create_task(self._read_events())
        self.stderr_task = asyncio.create_task(self._read_stderr())
        for name in OBSERVED_PROPERTIES:
            await self.command("observe_property", 0, name)

    async def close(self) -> None:
        """Stop mpv and close its private IPC connection."""
        if self.writer is not None:
            self.writer.close()
            await self.writer.wait_closed()
            self.writer = None
        if self.process is not None and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=3)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        if self.reader_task is not None:
            self.reader_task.cancel()
            await asyncio.gather(self.reader_task, return_exceptions=True)
        if self.stderr_task is not None:
            self.stderr_task.cancel()
            await asyncio.gather(self.stderr_task, return_exceptions=True)
        self.socket_path.unlink(missing_ok=True)

    async def command(self, *args: Any) -> Any:
        """Send an mpv command on behalf of the MPD client."""
        if args[0] in {"loadfile", "stop"} or args[:2] == ("set_property", "pause"):
            # The user took over; don't auto-resume when the output returns.
            self.paused_for_output = False
            self.reload_on_output = None
            if args[0] == "loadfile":
                self.last_load = args[1]
                self.load_retried = False
        return await self._send(*args)

    async def _send(self, *args: Any) -> Any:
        """Send an mpv JSON IPC command and return its response data."""
        if self.writer is None or self.reader is None:
            raise MPVError("mpv IPC is not connected")
        request_id = self.next_request_id
        self.next_request_id += 1
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        payload = json.dumps({"command": args, "request_id": request_id}, separators=(",", ":"))
        async with self.request_lock:
            self.writer.write((payload + "\n").encode())
            await self.writer.drain()
        try:
            reply = await asyncio.wait_for(future, timeout=5)
        finally:
            self.pending.pop(request_id, None)
        if reply.get("error") != "success":
            raise MPVError(str(reply.get("error", "unknown mpv error")))
        return reply.get("data")

    async def get(self, name: str, default: Any = None) -> Any:
        """Read a property from mpv."""
        try:
            value = await self._send("get_property", name)
        except MPVError:
            return default
        return default if value is None else value

    async def _handle_device_list(self, devices: list[dict[str, Any]]) -> None:
        """Keep playback on the pinned output device across AVR power changes.

        The AVR's HDMI device vanishes for a moment while the AVR powers on or
        off, and mpv's CoreAudio output then falls back to the Mac's speakers
        and stays there after the device returns. So pause while the pinned
        device is gone, and when it returns reopen the output on it and resume
        if this pause was ours.
        """
        if self.device_uid == "auto":
            return
        target = self.device_uid if self.device_uid.startswith("coreaudio/") else f"coreaudio/{self.device_uid}"
        present = any(device.get("name") == target for device in devices)
        previous, self.output_present = self.output_present, present
        if present == previous:
            return
        if not present:
            playing = not await self.get("idle-active", True) and not await self.get("pause", False)
            LOGGER.warning("Output device %s disappeared%s", target, "; pausing" if playing else "")
            if playing:
                await self._send("set_property", "pause", True)
                self.paused_for_output = True
        elif previous is False:
            LOGGER.warning("Output device %s returned; reopening audio output", target)
            await self._send("ao-reload")
            if self.paused_for_output:
                self.paused_for_output = False
                await self._send("set_property", "pause", False)
            if self.reload_on_output is not None:
                url, self.reload_on_output = self.reload_on_output, None
                await self._send("loadfile", url, "replace")

    async def _handle_load_failure(self, error: str | None) -> None:
        """Retry a track whose audio output could not be opened.

        MA's play often arrives while the AVR is powering on and its HDMI
        device is briefly gone; mpv then gives up on the track at once.
        """
        if error != "audio output initialization failed" or self.last_load is None:
            return
        if self.output_present is False:
            LOGGER.warning("Audio output unavailable; will retry the track when the output device returns")
            self.reload_on_output = self.last_load
        elif not self.load_retried:
            LOGGER.warning("Audio output failed to open; retrying the track once")
            self.load_retried = True
            await self._send("loadfile", self.last_load, "replace")

    def _schedule(self, coroutine: Any) -> None:
        """Run a handler outside the IPC reader, which must keep reading replies."""
        task = asyncio.create_task(coroutine)
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)

    async def _read_events(self) -> None:
        """Resolve command responses and forward mpv playback events."""
        if self.reader is None:
            return
        try:
            while line := await self.reader.readline():
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    LOGGER.warning("Ignoring invalid mpv IPC JSON")
                    continue
                request_id = message.get("request_id")
                if request_id in self.pending:
                    future = self.pending[request_id]
                    if not future.done():
                        future.set_result(message)
                event = message.get("event")
                # MPD clients poll elapsed time themselves. Turning every MPV
                # time-pos tick into an MPD player change floods idle/noidle
                # and makes ordinary commands race with idle responses.
                if event == "property-change" and message.get("name") == "time-pos":
                    continue
                if event == "property-change" and message.get("name") == "audio-device-list":
                    self._schedule(self._handle_device_list(message.get("data") or []))
                    continue
                if event in {
                    "property-change",
                    "file-loaded",
                    "start-file",
                    "end-file",
                    "playback-restart",
                } and self.on_change is not None:
                    self.on_change()
                if event == "end-file" and message.get("reason") == "error":
                    self._schedule(self._handle_load_failure(message.get("file_error")))
                if message.get("event") in {"start-file", "file-loaded", "end-file"}:
                    details = message.get("event")
                    if details == "end-file":
                        details = {
                            "event": details,
                            "reason": message.get("reason"),
                            "error": message.get("file_error"),
                        }
                    LOGGER.info("MPV playback event: %s", details)
        except asyncio.CancelledError:
            raise
        except (ConnectionError, OSError) as err:
            LOGGER.warning("mpv IPC reader stopped: %s", err)
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(MPVError("mpv IPC disconnected"))

    async def _read_stderr(self) -> None:
        """Log MPV diagnostics after redacting any stream URL."""
        if self.process is None or self.process.stderr is None:
            return
        while line := await self.process.stderr.readline():
            message = line.decode("utf-8", errors="replace").strip()
            message = re.sub(r"""https?://[^\s'"<>]+""", "<stream-url-redacted>", message)
            # --log-file always records mpv's verbose output, so drop the
            # chatty levels here unless the bridge itself runs with --verbose.
            level = MPV_LOG_LEVEL.match(message)
            if level and level.group(1) not in "fwe" and not self.debug:
                continue
            if message:
                LOGGER.info("MPV: %s", message)


class StatePublisher:
    """Push the player state and decoded channel count to Home Assistant.

    The Mac's HDMI output is always a multichannel PCM container, so the AVR
    cannot tell stereo from 5.1; HA uses this to pick the surround mode.
    """

    def __init__(self, mpv: Any, send: Any) -> None:
        self.mpv = mpv
        self.send = send
        self.last_sent: dict[str, Any] | None = None
        self.task: asyncio.Task[None] | None = None

    def schedule(self) -> None:
        """Check soon; mpv emits bursts of changes, so run one check at a time."""
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.check())

    async def check(self) -> None:
        """Send the current state if it differs from the last one HA received."""
        idle = bool(await self.mpv.get("idle-active", True))
        paused = bool(await self.mpv.get("pause", False))
        params = await self.mpv.get("audio-params", {}) if not idle else {}
        if not isinstance(params, dict):
            params = {}
        payload = {
            "state": "stop" if idle else ("pause" if paused else "play"),
            "channels": int(params.get("channel-count", 0) or 0),
            "samplerate": int(params.get("samplerate", 0) or 0),
        }
        if payload == self.last_sent:
            return
        try:
            await self.send(payload)
        except (OSError, ValueError) as err:
            LOGGER.warning("Could not send player state to Home Assistant: %s", err)
            return
        self.last_sent = payload


def post_json(url: str, payload: dict[str, Any]) -> None:
    """POST a JSON body; raises OSError on network or HTTP failure."""
    if urlsplit(url).scheme not in {"http", "https"}:
        raise ValueError("ha_webhook_url must be an http(s) URL")
    request = urllib.request.Request(  # noqa: S310 - scheme checked above
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=5):  # noqa: S310 - scheme checked above
        pass


@dataclass
class ClientState:
    authenticated: bool = False
    idle_event: asyncio.Event = field(default_factory=asyncio.Event)
    changed: set[str] = field(default_factory=set)
    pending_line_task: asyncio.Task[bytes] | None = None


class Bridge:
    """Serve the MPD subset needed by Music Assistant."""

    def __init__(self, mpv: MPVClient, password: str) -> None:
        self.mpv = mpv
        self.password = password
        self.current_url: str | None = None
        self.playlist_version = 1
        self.client_states: dict[int, ClientState] = {}
        self.next_client_id = 0

    def notify(self, *subsystems: str) -> None:
        """Wake MPD clients waiting in idle."""
        for state in self.client_states.values():
            state.changed.update(subsystems)
            state.idle_event.set()

    async def handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle one MPD text-protocol connection."""
        client_id = self.next_client_id
        self.next_client_id += 1
        state = ClientState()
        self.client_states[client_id] = state
        peer = writer.get_extra_info("peername")
        LOGGER.info("MPD client connected: %s", peer)
        writer.write(f"OK MPD {MPD_VERSION}\n".encode())
        await writer.drain()
        try:
            while True:
                if state.pending_line_task is None:
                    line = await reader.readline()
                else:
                    pending_line_task = state.pending_line_task
                    state.pending_line_task = None
                    line = await pending_line_task
                if not line:
                    break
                try:
                    command, args = parse_command(line.decode("utf-8").strip())
                    LOGGER.debug("MPD command received: %s", command)
                    if command == "idle":
                        await self._idle(reader, writer, state, args)
                        continue
                    if command == "noidle":
                        # The MPD async client writes noidle to cancel its
                        # outstanding idle request, but the idle notification
                        # may already have been returned by the time this line
                        # reaches us. In that race, noidle is still only a
                        # cancellation signal; replying with OK would become
                        # an unsolicited response and shift every later reply
                        # (for example, status would be parsed as idle data).
                        continue
                    response, close = await self._dispatch(command, args, state)
                    for item in response:
                        writer.write((item + "\n").encode("utf-8"))
                    if not response or response[-1] != "OK":
                        writer.write(b"OK\n")
                    await writer.drain()
                    if close:
                        break
                except (ValueError, MPVError) as err:
                    writer.write(f"ACK [5@0] {{command}} {err}\n".encode())
                    await writer.drain()
                except Exception:
                    LOGGER.exception("Failed to handle MPD command")
                    writer.write(b"ACK [5@0] {command} internal error\n")
                    await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            if state.pending_line_task is not None:
                state.pending_line_task.cancel()
                await asyncio.gather(state.pending_line_task, return_exceptions=True)
            self.client_states.pop(client_id, None)
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()
            LOGGER.info("MPD client disconnected: %s", peer)

    async def _idle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        state: ClientState,
        args: list[str],
    ) -> None:
        """Wait until an observed player state change or a noidle command."""
        # ``args`` names the subsystems the client wants to observe; it does
        # not mean those subsystems have changed. Actual changes are queued by
        # notify(), otherwise every idle request would complete immediately.
        # Keep notifications that arrived just before this idle call. Clearing
        # the event first can otherwise lose a change that raced with noidle.
        if state.changed:
            await self._write_idle_changes(writer, state)
            return
        state.idle_event.clear()
        change_task = asyncio.create_task(state.idle_event.wait())
        line_task = asyncio.create_task(reader.readline())
        done, _ = await asyncio.wait(
            {change_task, line_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if line_task in done:
            if not change_task.done():
                change_task.cancel()
                await asyncio.gather(change_task, return_exceptions=True)
            line = line_task.result()
            if not line:
                return
            command, _ = parse_command(line.decode("utf-8").strip())
            if command != "noidle":
                writer.write(b"ACK [5@0] {idle} expected noidle\n")
                await writer.drain()
                return
            if change_task.done() and state.changed:
                await self._write_idle_changes(writer, state)
            else:
                writer.write(b"OK\n")
                await writer.drain()
            return
        # Do not cancel readline when a notification wins. Cancellation can
        # consume a simultaneous noidle command without delivering it, shifting
        # the response stream and making a later status reply look like idle.
        state.pending_line_task = line_task
        if change_task in done:
            await self._write_idle_changes(writer, state)

    @staticmethod
    async def _write_idle_changes(writer: asyncio.StreamWriter, state: ClientState) -> None:
        """Write and clear the pending MPD idle notifications."""
        for subsystem in sorted(state.changed):
            writer.write(f"changed: {subsystem}\n".encode())
        state.changed.clear()
        state.idle_event.clear()
        writer.write(b"OK\n")
        await writer.drain()

    async def _dispatch(
        self, command: str, args: list[str], state: ClientState
    ) -> tuple[list[str], bool]:
        """Run one supported MPD command."""
        if command == "password":
            if len(args) != 1 or args[0] != self.password:
                raise ValueError("incorrect password")
            state.authenticated = True
            return [], False
        if command in {"close", "ping", "status", "noidle"}:
            if command == "close":
                return [], True
            if command == "ping":
                return [], False
            if command == "status":
                return await self._status(), False
            return [], False
        if not state.authenticated:
            raise ValueError("permission denied")
        if command == "currentsong":
            return ([f"file: {self.current_url}"] if self.current_url else []), False
        if command == "clear":
            await self.mpv.command("stop")
            self.current_url = None
            self.playlist_version += 1
            self.notify("player", "playlist")
            return [], False
        if command == "add":
            if len(args) != 1:
                raise ValueError("add requires one URI")
            self.current_url = args[0]
            parsed_url = urlsplit(self.current_url)
            LOGGER.info(
                "MPD stream target: %s://%s:%s",
                parsed_url.scheme,
                parsed_url.hostname or "<no-host>",
                parsed_url.port or (443 if parsed_url.scheme == "https" else 80),
            )
            self.playlist_version += 1
            self.notify("playlist")
            return [], False
        if command == "play":
            if self.current_url is None:
                raise ValueError("no item in queue")
            if await self.mpv.get("idle-active", True):
                await self.mpv.command("loadfile", self.current_url, "replace")
            else:
                await self.mpv.command("set_property", "pause", False)
            self.notify("player")
            return [], False
        if command == "pause":
            pause = bool_arg(args[0]) if args else not bool(await self.mpv.get("pause", False))
            await self.mpv.command("set_property", "pause", pause)
            self.notify("player")
            return [], False
        if command == "stop":
            await self.mpv.command("stop")
            self.notify("player")
            return [], False
        if command == "seekcur":
            if len(args) != 1:
                raise ValueError("seekcur requires a position")
            position = float(args[0].lstrip("+"))
            await self.mpv.command("seek", position, "absolute")
            self.notify("player")
            return [], False
        if command == "setvol":
            if len(args) != 1:
                raise ValueError("setvol requires a volume")
            volume = max(0.0, min(100.0, float(args[0])))
            await self.mpv.command("set_property", "volume", volume)
            self.notify("mixer")
            return [], False
        raise ValueError(f"unknown command {command}")

    async def _status(self) -> list[str]:
        """Return the state fields read by the Music Assistant MPD provider."""
        idle = bool(await self.mpv.get("idle-active", True))
        paused = bool(await self.mpv.get("pause", False))
        position = await self.mpv.get("time-pos", 0.0)
        duration = await self.mpv.get("duration", 0.0)
        volume = await self.mpv.get("volume", 100.0)
        params = await self.mpv.get("audio-params", {})
        # A track waiting for its output device (see MPVClient._handle_load_failure)
        # is still playing as far as MA is concerned; MA abandons a track it sees stop.
        if idle and getattr(self.mpv, "reload_on_output", None) is not None:
            idle, paused = False, False
        state = "stop" if idle else ("pause" if paused else "play")
        result = [
            f"volume: {round(float(volume))}",
            f"state: {state}",
            "song: 0" if self.current_url else "song: -1",
            "playlist: 1" if self.current_url else "playlist: 0",
            f"playlistlength: {1 if self.current_url else 0}",
            f"playlistversion: {self.playlist_version}",
            f"elapsed: {max(0.0, float(position or 0.0)):.3f}",
            f"duration: {max(0.0, float(duration or 0.0)):.3f}",
        ]
        if isinstance(params, dict) and params:
            rate = params.get("samplerate", 0)
            bits = sample_format_bits(params.get("format", ""))
            channels = params.get("channel-count", 0)
            if rate and channels:
                result.append(f"audio: {rate}:{bits}:{channels}")
        return result


def sample_format_bits(value: str) -> int:
    """Convert mpv's sample format label to an integer bit depth."""
    match = re.search(r"(\d+)", value)
    return int(match.group(1)) if match else 0


def bool_arg(value: str) -> bool:
    """Parse an MPD boolean argument."""
    if value in {"1", "true", "yes"}:
        return True
    if value in {"0", "false", "no"}:
        return False
    raise ValueError(f"invalid boolean {value}")


def parse_command(line: str) -> tuple[str, list[str]]:
    """Parse the quoted command syntax used by MPD clients."""
    parts = shlex.split(line, posix=True)
    if not parts:
        raise ValueError("empty command")
    return parts[0], parts[1:]


def load_password(service: str, account: str) -> str:
    """Read the MPD password from macOS Keychain without logging the secret."""
    try:
        result = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", service, "-a", account, "-w"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as err:
        raise RuntimeError(
            f"Could not read Keychain item {service!r} for account {account!r}; "
            "run scripts/store-password.sh first"
        ) from err
    password = result.stdout.rstrip("\r\n")
    if len(password) < 24:
        raise RuntimeError("The MPD password must contain at least 24 characters")
    return password


def load_config(path: Path) -> dict[str, Any]:
    """Read bridge settings from a JSON file and fill safe defaults."""
    config: dict[str, Any] = {
        "listen": "0.0.0.0",
        "port": 6601,
        "mpv": "mpv",
        "audio_device": "auto",
        "ipc_socket": str(Path.home() / "Library/Application Support/MA MPV Player/mpv.sock"),
        "keychain_service": "com.ollisulopuisto.ma-mpv-player",
        "keychain_account": os.environ.get("USER", "music-assistant"),
        "ha_webhook_url": None,
    }
    if path.exists():
        with path.open(encoding="utf-8") as file:
            loaded = json.load(file)
        if not isinstance(loaded, dict):
            raise ValueError("Bridge configuration must be a JSON object")
        config.update(loaded)
    return config


async def run(args: argparse.Namespace) -> None:
    """Start mpv and serve MPD clients until shutdown."""
    config = load_config(args.config)
    overrides = {
        "listen": args.listen,
        "port": args.port,
        "mpv": args.mpv,
        "audio_device": args.audio_device,
        "ipc_socket": args.ipc_socket,
        "keychain_service": args.keychain_service,
        "keychain_account": args.keychain_account,
    }
    config.update({key: value for key, value in overrides.items() if value is not None})
    if not 1 <= int(config["port"]) <= 65535:
        raise ValueError("MPD listen port must be between 1 and 65535")
    if not all(config.get(key) for key in ("listen", "mpv", "ipc_socket", "keychain_service", "keychain_account")):
        raise ValueError("Bridge config is missing a required setting")
    password = load_password(config["keychain_service"], config["keychain_account"])
    mpv = MPVClient(config["mpv"], config["audio_device"], Path(config["ipc_socket"]), debug=args.verbose)
    bridge = Bridge(mpv, password)
    webhook = config["ha_webhook_url"]
    publisher = StatePublisher(mpv, lambda payload: asyncio.to_thread(post_json, webhook, payload)) if webhook else None

    def on_change() -> None:
        bridge.notify("player")
        if publisher is not None:
            publisher.schedule()

    await mpv.start(on_change)
    server = await asyncio.start_server(bridge.handle_client, config["listen"], int(config["port"]))
    LOGGER.info("MPV MPD bridge listening on %s:%s", config["listen"], config["port"])
    try:
        async with server:
            await server.serve_forever()
    finally:
        server.close()
        await server.wait_closed()
        await mpv.close()


def main() -> None:
    """Parse options and run the bridge."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path.home() / ".config/ma-mpv-player/config.json")
    parser.add_argument("--listen")
    parser.add_argument("--port", type=int)
    parser.add_argument("--mpv")
    parser.add_argument("--audio-device")
    parser.add_argument("--ipc-socket")
    parser.add_argument("--keychain-service")
    parser.add_argument("--keychain-account")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass
    except (MPVError, RuntimeError, ValueError, OSError) as err:
        LOGGER.error("MPV bridge could not start: %s", err)  # noqa: TRY400 - config errors, no traceback
        raise SystemExit(1) from err


if __name__ == "__main__":
    main()
