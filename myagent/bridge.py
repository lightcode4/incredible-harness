"""Bridge for the LightCode4 Chrome extension: the agent can use your own Chrome (your tabs, your sign-ins).

The extension talks to a small server on a fixed local port (127.0.0.1:47561) with a pairing key you paste into it
once. It asks for work (long polling), does it in Chrome, and sends the result back."""
from __future__ import annotations

import json
import os
import queue
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("LC4_EXT_PORT", "47561"))


class ChromeBridge:
    def __init__(self):
        self.jobs: queue.Queue = queue.Queue()
        self.waiting: dict[str, list] = {}
        self.last_seen = 0.0
        self.info = {}

    @property
    def connected(self) -> bool:
        return time.time() - self.last_seen < 45

    def call(self, action: str, args: dict | None = None, timeout: float = 60):
        if not self.connected:
            raise RuntimeError("The LightCode4 Chrome extension isn't connected. Install it and paste the pairing code "
                               "(Settings > Tools > Chrome), and keep Chrome open.")
        jid = secrets.token_hex(6)
        done = threading.Event()
        self.waiting[jid] = [done, None]
        self.jobs.put({"id": jid, "action": action, "args": args or {}})
        if not done.wait(timeout):
            self.waiting.pop(jid, None)
            raise RuntimeError("Chrome didn't answer in time.")
        ok, data = self.waiting.pop(jid)[1]
        if not ok:
            raise RuntimeError(f"Chrome: {data}")
        return data

    def next_job(self, wait: float = 25) -> dict | None:
        self.last_seen = time.time()
        try:
            job = self.jobs.get(timeout=wait)
        except queue.Empty:
            job = None
        self.last_seen = time.time()
        return job

    def finish(self, jid: str, ok: bool, data) -> None:
        slot = self.waiting.get(jid)
        if slot:
            slot[1] = (ok, data)
            slot[0].set()


def start_ext_server(bridge: ChromeBridge, key_fn) -> ThreadingHTTPServer | None:
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _out(self, obj, code=200):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            if self.path == "/ping":
                return self._out({"app": "LightCode4"})
            self._out({"error": "not found"}, 404)

        def do_POST(self):
            if not secrets.compare_digest(self.headers.get("X-LC4-Key", ""), key_fn()):
                return self._out({"error": "Wrong pairing code"}, 401)
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0) or 0)) or b"{}")
            if self.path == "/chrome/next":
                bridge.info = {"browser": str(body.get("browser", ""))[:80]}
                return self._out({"job": bridge.next_job(float(body.get("wait", 25)))})
            if self.path == "/chrome/result":
                bridge.finish(str(body.get("id", "")), bool(body.get("ok")), body.get("data"))
                return self._out({})
            self._out({"error": "not found"}, 404)

    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    except OSError:
        return None  # another LightCode4 is already running and owns the port
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
