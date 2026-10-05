#!/usr/bin/env python3
"""Stand-in for mpv: serves its JSON IPC socket and records its pid."""

import json
import os
import socket
import sys
import threading
from pathlib import Path

socket_path = next(arg.split("=", 1)[1] for arg in sys.argv if arg.startswith("--input-ipc-server="))
Path(os.environ["FAKE_MPV_PID_FILE"]).write_text(str(os.getpid()), encoding="utf-8")


def serve(connection: socket.socket) -> None:
    buffer = b""
    while chunk := connection.recv(4096):
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            request = json.loads(line)
            reply = {"request_id": request.get("request_id"), "error": "success", "data": None}
            connection.sendall((json.dumps(reply) + "\n").encode())


server = socket.socket(socket.AF_UNIX)
server.bind(socket_path)
server.listen()
while True:
    client, _ = server.accept()
    threading.Thread(target=serve, args=(client,), daemon=True).start()
