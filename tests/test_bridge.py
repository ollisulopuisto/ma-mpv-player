import asyncio
import http.server
import json
import plistlib
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bridge import (
    OBSERVED_PROPERTIES,
    Bridge,
    ClientState,
    MPVClient,
    MPVError,
    StatePublisher,
    bool_arg,
    load_config,
    load_password,
    parse_command,
    post_json,
    serve_until_mpv_exits,
)


class FakeMPV:
    def __init__(self):
        self.properties = {
            "idle-active": True,
            "pause": False,
            "time-pos": 0,
            "duration": 0,
            "volume": 72,
            "audio-params": {"samplerate": 96000, "format": "s24", "channel-count": 6},
        }
        self.commands = []
        self.reload_on_output = None

    async def get(self, name, default=None):
        return self.properties.get(name, default)

    async def command(self, *args):
        self.commands.append(args)
        if args[0] == "stop":
            self.properties["idle-active"] = True
        elif args[0] == "loadfile":
            self.properties["idle-active"] = False
        elif args[0] == "set_property":
            self.properties[args[1]] = args[2]


MPV_LOG_LINES = [
    b"[   0.112][d][cplayer] Run command: disable-section\n",
    b"[   0.112][v][ao/coreaudio] Handling potential hotplug event...\n",
    b"[   0.113][e][ao/coreaudio] failed to select device (jbo[33]/560947818)\n",
    b"[   0.114][w][ffmpeg] http: reading http://ma.local:8097/stream?token=secret failed\n",
]


class MPVLogTests(unittest.IsolatedAsyncioTestCase):
    async def read_mpv_log(self, debug):
        client = MPVClient("mpv", "auto", Path("/nonexistent.sock"), debug=debug)
        stderr = asyncio.StreamReader()
        for line in MPV_LOG_LINES:
            stderr.feed_data(line)
        stderr.feed_eof()
        client.process = SimpleNamespace(stderr=stderr)
        with self.assertLogs("mpv_mpd_bridge", level="INFO") as logs:
            await client._read_stderr()
        return [record.getMessage() for record in logs.records]

    async def test_quiet_mode_logs_only_mpv_warnings_and_errors(self):
        messages = await self.read_mpv_log(debug=False)

        self.assertEqual(len(messages), 2)
        self.assertIn("failed to select device", messages[0])
        self.assertIn("<stream-url-redacted>", messages[1])
        self.assertNotIn("token=secret", messages[1])

    async def test_debug_mode_logs_every_mpv_line(self):
        messages = await self.read_mpv_log(debug=True)

        self.assertEqual(len(messages), 4)


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.mpv = FakeMPV()
        self.bridge = Bridge(self.mpv, "a" * 24)
        self.state = ClientState()

    async def test_mpd_commands_control_mpv(self):
        await self.bridge._dispatch("password", ["a" * 24], self.state)
        await self.bridge._dispatch("clear", [], self.state)
        await self.bridge._dispatch("add", ["http://ma.local:8097/stream?token=secret"], self.state)
        await self.bridge._dispatch("play", [], self.state)
        await self.bridge._dispatch("seekcur", ["+42.5"], self.state)
        await self.bridge._dispatch("pause", ["1"], self.state)
        await self.bridge._dispatch("setvol", ["150"], self.state)

        self.assertEqual(self.mpv.commands[0], ("stop",))
        self.assertEqual(self.mpv.commands[1][0], "loadfile")
        self.assertEqual(self.mpv.commands[2], ("seek", 42.5, "absolute"))
        self.assertEqual(self.mpv.properties["pause"], True)
        self.assertEqual(self.mpv.properties["volume"], 100)

    async def test_password_required_for_mutation_and_currentsong(self):
        self.bridge.current_url = "http://ma.local:8097/stream?token=secret"
        with self.assertRaisesRegex(ValueError, "permission denied"):
            await self.bridge._dispatch("play", [], self.state)
        with self.assertRaisesRegex(ValueError, "permission denied"):
            await self.bridge._dispatch("currentsong", [], self.state)
        with self.assertRaisesRegex(ValueError, "incorrect password"):
            await self.bridge._dispatch("password", ["wrong"], self.state)

    async def test_status_reports_state_position_and_decoded_format(self):
        self.mpv.properties.update(
            {"idle-active": False, "time-pos": 3.25, "duration": 180.0}
        )
        status = await self.bridge._status()
        self.assertIn("state: play", status)
        self.assertIn("elapsed: 3.250", status)
        self.assertIn("audio: 96000:24:6", status)

    async def test_track_waiting_for_its_output_reports_play_not_stop(self):
        # MA gives up on a track it sees stop, and never notices the reload.
        self.bridge.current_url = "http://ma.local/x.wav"
        self.mpv.reload_on_output = "http://ma.local/x.wav"
        self.mpv.properties.update({"idle-active": True})

        self.assertIn("state: play", await self.bridge._status())

    async def test_idle_command_waits_until_notification(self):
        reader = asyncio.StreamReader()
        writer = FakeWriter()
        self.bridge.client_states[0] = self.state
        task = asyncio.create_task(
            self.bridge._idle(reader, writer, self.state, ["player"])
        )
        await asyncio.sleep(0)
        self.bridge.notify("player")
        await asyncio.wait_for(task, timeout=1)
        self.assertEqual(writer.data, b"changed: player\nOK\n")
        if self.state.pending_line_task is not None:
            self.state.pending_line_task.cancel()
            await asyncio.gather(self.state.pending_line_task, return_exceptions=True)
            self.state.pending_line_task = None


class UtilityTests(unittest.TestCase):
    def test_parse_command_handles_quoted_arguments(self):
        self.assertEqual(
            parse_command('add "http://ma.local:8097/a path?token=secret"'),
            ("add", ["http://ma.local:8097/a path?token=secret"]),
        )

    def test_bool_arg_accepts_mpd_values_only(self):
        self.assertTrue(bool_arg("yes"))
        self.assertFalse(bool_arg("0"))
        with self.assertRaises(ValueError):
            bool_arg("maybe")

    def test_config_overrides_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"port": 6602, "audio_device": "coreaudio/device-uid"}))
            config = load_config(path)
        self.assertEqual(config["port"], 6602)
        self.assertEqual(config["audio_device"], "coreaudio/device-uid")
        self.assertEqual(config["listen"], "0.0.0.0")

    @patch("bridge.subprocess.run")
    def test_password_is_read_from_keychain_without_putting_secret_in_args(self, run):
        secret = "safe-password-with-more-than-24-chars"
        run.return_value = SimpleNamespace(stdout=secret + "\n")
        self.assertEqual(load_password("service", "account"), secret)
        args = run.call_args.args[0]
        self.assertIn("find-generic-password", args)
        self.assertNotIn(secret, args)

    @patch("bridge.subprocess.run")
    def test_short_keychain_password_is_rejected(self, run):
        run.return_value = SimpleNamespace(stdout="short\n")
        with self.assertRaisesRegex(RuntimeError, "24 characters"):
            load_password("service", "account")

    def test_launch_agent_generator_renders_install_paths(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "agent.plist"
            subprocess.run(
                [
                    sys.executable,
                    str(root / "scripts/generate-launch-agent.py"),
                    str(root / "launchd/com.ollisulopuisto.ma-mpv-player.plist.example"),
                    str(output),
                    "/usr/bin/python3",
                    "/Users/test/Library/Application Support/MA MPV Player/bridge.py",
                    "/Users/test/.config/ma-mpv-player/config.json",
                    "/Users/test/Library/Application Support/MA MPV Player",
                    "/Users/test/Library/Logs/ma-mpv-player.log",
                ],
                check=True,
            )
            plist = plistlib.loads(output.read_bytes())
        self.assertEqual(
            plist["ProgramArguments"],
            [
                "/usr/bin/python3",
                "/Users/test/Library/Application Support/MA MPV Player/bridge.py",
                "--config",
                "/Users/test/.config/ma-mpv-player/config.json",
            ],
        )
        self.assertTrue(plist["KeepAlive"])
        self.assertTrue(plist["RunAtLoad"])


class FakeWriter:
    def __init__(self):
        self.data = bytearray()

    def write(self, data):
        self.data.extend(data)

    async def drain(self):
        pass


if __name__ == "__main__":
    unittest.main()


class FindPythonTests(unittest.TestCase):
    """launchd jobs need a Python that macOS lets reach the LAN (Local Network privacy)."""

    root = Path(__file__).resolve().parents[1]

    def find_python(self, directory, **env):
        conda_bin = Path(directory) / "miniconda3/bin"
        brew_bin = Path(directory) / "homebrew/bin"
        for folder in (conda_bin, brew_bin):
            folder.mkdir(parents=True)
            fake = folder / "python3"
            fake.write_text("#!/bin/sh\n")
            fake.chmod(0o755)
        result = subprocess.run(
            ["/bin/bash", str(self.root / "scripts/find-python.sh")],
            check=True,
            capture_output=True,
            text=True,
            env={"PATH": f"{conda_bin}:/usr/bin:/bin", "HOMEBREW_PREFIX": str(Path(directory) / "homebrew"), **env},
        )
        return result.stdout.strip(), brew_bin / "python3"

    def test_prefers_homebrew_python_over_first_python_on_path(self):
        with tempfile.TemporaryDirectory() as directory:
            chosen, brew_python = self.find_python(directory)
            self.assertEqual(chosen, str(brew_python))

    def test_explicit_python_overrides_detection(self):
        with tempfile.TemporaryDirectory() as directory:
            chosen, _ = self.find_python(directory, PYTHON="/usr/bin/python3")
            self.assertEqual(chosen, "/usr/bin/python3")


AO_FAILED = "audio output initialization failed"
DENON = "coreaudio/11EE6600-0000-0000-001D-010380502D78"


def device_list(*names):
    return [{"name": "auto"}, *({"name": name} for name in names), {"name": "coreaudio/BuiltInSpeakerDevice"}]


class OutputDeviceTests(unittest.IsolatedAsyncioTestCase):
    """The AVR's HDMI device vanishes while it powers on or off; mpv then falls
    back to the Mac's speakers and stays there after the device returns."""

    def make_client(self, device_uid="11EE6600-0000-0000-001D-010380502D78", idle=False, pause=False):
        client = MPVClient("mpv", device_uid, Path("/nonexistent.sock"))
        properties = {"idle-active": idle, "pause": pause}
        client.sent = []

        async def send(*args):
            if args[0] == "get_property":
                return properties[args[1]]
            client.sent.append(args)
            if args[:2] == ("set_property", "pause"):
                properties["pause"] = args[2]
            return None

        client._send = send
        return client

    async def test_playback_pauses_while_output_is_gone_and_resumes_on_it(self):
        client = self.make_client()
        await client._handle_device_list(device_list(DENON))
        await client._handle_device_list(device_list())
        self.assertEqual(client.sent, [("set_property", "pause", True)])

        await client._handle_device_list(device_list(DENON))
        self.assertEqual(
            client.sent,
            [("set_property", "pause", True), ("ao-reload",), ("set_property", "pause", False)],
        )

    async def test_returning_output_is_reopened_without_resuming_a_user_pause(self):
        client = self.make_client(pause=True)
        await client._handle_device_list(device_list(DENON))
        await client._handle_device_list(device_list())
        await client._handle_device_list(device_list(DENON))

        self.assertEqual(client.sent, [("ao-reload",)])

    async def test_mpd_command_while_output_is_gone_cancels_the_auto_resume(self):
        client = self.make_client()
        await client._handle_device_list(device_list(DENON))
        await client._handle_device_list(device_list())
        await client.command("stop")
        await client._handle_device_list(device_list(DENON))

        self.assertEqual(client.sent, [("set_property", "pause", True), ("stop",), ("ao-reload",)])

    async def test_track_that_failed_while_output_was_gone_is_reloaded_on_return(self):
        # The AVR powering on removes its HDMI device just as MA sends play.
        client = self.make_client(idle=True)
        url = "http://ma.local:8097/flow/x.wav"
        await client._handle_device_list(device_list(DENON))
        await client._handle_device_list(device_list())
        await client.command("loadfile", url, "replace")
        await client._handle_load_failure(AO_FAILED)
        await client._handle_device_list(device_list(DENON))

        self.assertEqual(client.sent, [("loadfile", url, "replace"), ("ao-reload",), ("loadfile", url, "replace")])

    async def test_mpd_command_cancels_the_pending_reload(self):
        client = self.make_client(idle=True)
        await client._handle_device_list(device_list(DENON))
        await client._handle_device_list(device_list())
        await client.command("loadfile", "http://ma.local/x.wav", "replace")
        await client._handle_load_failure(AO_FAILED)
        await client.command("stop")
        await client._handle_device_list(device_list(DENON))

        self.assertNotIn(("loadfile", "http://ma.local/x.wav", "replace"), client.sent[1:])

    async def test_failure_with_output_present_is_retried_once(self):
        client = self.make_client(idle=True)
        url = "http://ma.local/x.wav"
        await client._handle_device_list(device_list(DENON))
        await client.command("loadfile", url, "replace")
        await client._handle_load_failure(AO_FAILED)
        await client._handle_load_failure(AO_FAILED)

        self.assertEqual(client.sent, [("loadfile", url, "replace"), ("loadfile", url, "replace")])

    async def test_other_load_errors_are_not_retried(self):
        client = self.make_client(idle=True)
        await client._handle_device_list(device_list(DENON))
        await client.command("loadfile", "http://ma.local/x.wav", "replace")
        await client._handle_load_failure("loading failed")

        self.assertEqual(len(client.sent), 1)

    async def test_auto_device_is_left_to_mpv(self):
        client = self.make_client(device_uid="auto")
        await client._handle_device_list(device_list(DENON))
        await client._handle_device_list(device_list())
        await client._handle_device_list(device_list(DENON))

        self.assertEqual(client.sent, [])

    def test_device_list_is_observed(self):
        self.assertIn("audio-device-list", OBSERVED_PROPERTIES)


class StatePublisherTests(unittest.IsolatedAsyncioTestCase):
    """Home Assistant restores the Denon's surrounds from the decoded channel count."""

    def setUp(self):
        self.mpv = FakeMPV()
        self.sent = []

        async def send(payload):
            self.sent.append(payload)

        self.publisher = StatePublisher(self.mpv, send)

    async def test_publishes_decoded_channels_once_per_change(self):
        self.mpv.properties.update({"idle-active": False, "pause": False})
        await self.publisher.check()
        await self.publisher.check()

        self.assertEqual(self.sent, [{"state": "play", "channels": 6, "samplerate": 96000}])

    async def test_stopped_player_publishes_zero_channels(self):
        self.mpv.properties.update({"idle-active": False})
        await self.publisher.check()
        self.mpv.properties.update({"idle-active": True})
        await self.publisher.check()

        self.assertEqual(self.sent[-1], {"state": "stop", "channels": 0, "samplerate": 0})

    async def test_failed_send_is_logged_and_retried_on_next_change(self):
        async def failing_send(payload):
            raise OSError("unreachable")

        publisher = StatePublisher(self.mpv, failing_send)
        with self.assertLogs("mpv_mpd_bridge", level="WARNING"):
            await publisher.check()
        publisher.send = self.publisher.send
        await publisher.check()

        self.assertEqual(len(self.sent), 1)

    def test_webhook_is_off_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(load_config(Path(directory) / "missing.json")["ha_webhook_url"])

    def test_post_json_sends_payload_to_webhook(self):
        received = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.handle_request)
        thread.start()
        post_json(f"http://127.0.0.1:{server.server_port}/api/webhook/x", {"channels": 2})
        thread.join()
        server.server_close()

        self.assertEqual(received, [{"channels": 2}])

    def test_post_json_rejects_non_http_urls(self):
        with self.assertRaises(ValueError):
            post_json("file:///etc/passwd", {})


class SupervisionTests(unittest.IsolatedAsyncioTestCase):
    """libmpv 0.41 can crash on CoreAudio device changes; launchd restarts the
    bridge only if the bridge itself exits."""

    async def test_mpv_exit_stops_the_bridge(self):
        class Server:
            async def serve_forever(self):
                await asyncio.Event().wait()

        class Process:
            returncode = None

            async def wait(self):
                self.returncode = -11
                return -11

        with self.assertRaisesRegex(MPVError, "-11"):
            await asyncio.wait_for(serve_until_mpv_exits(Server(), Process()), timeout=1)
