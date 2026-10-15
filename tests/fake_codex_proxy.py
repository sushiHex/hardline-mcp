"""Stands in for `codex app-server proxy` plus the daemon behind it, on stdio.

Real WebSocket server framing (websockets' ServerProtocol) over the process's
own pipes, so tests drive codex_steer's real subprocess, reader thread,
deadline and cleanup. argv[1]: "accept" answers the steer, "silent" never does.
"""

import json
import sys

from websockets.frames import Opcode
from websockets.http11 import Request
from websockets.server import ServerProtocol

mode = sys.argv[1]
server = ServerProtocol()
out = sys.stdout.buffer
while data := sys.stdin.buffer.read1(65536):
    server.receive_data(data)
    for event in server.events_received():
        if isinstance(event, Request):
            server.send_response(server.accept(event))
        elif event.opcode == Opcode.TEXT:
            message = json.loads(event.data)
            if message.get("method") == "initialize":
                server.send_text(json.dumps({"id": message["id"], "result": {"userAgent": "fake/0"}}).encode())
            elif message.get("method") == "turn/steer" and mode == "accept":
                turn = message["params"]["expectedTurnId"]
                server.send_text(json.dumps({"id": message["id"], "result": {"turnId": turn}}).encode())
    for chunk in server.data_to_send():
        if chunk:
            out.write(chunk)
    out.flush()
