"""Stands in for `codex app-server proxy` plus the daemon behind it, on stdio.

Real WebSocket server framing (websockets' ServerProtocol) over the process's
own pipes, so tests drive codex_steer's real subprocess, reader thread,
deadline and cleanup. argv[1]: "accept" answers the steer, "silent" never
does, "flood" never does but sends notifications for 15 s.
"""

import json
import sys
import time

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
            elif message.get("method") == "turn/steer" and mode == "flood":
                until = time.monotonic() + 15
                note = json.dumps({"method": "thread/status/changed", "params": {}}).encode()
                try:
                    while time.monotonic() < until:
                        server.send_text(note)
                        for chunk in server.data_to_send():
                            out.write(chunk)
                        out.flush()  # no pause: the reader always has a backlog
                except OSError:
                    sys.exit(0)  # the client hung up
    for chunk in server.data_to_send():
        if chunk:
            out.write(chunk)
    out.flush()
