"""Graphical app: a local web page (opens in your browser) driving the same agent engine.

Only listens on 127.0.0.1 and every API call must carry a secret token from the page URL,
so other websites cannot send commands to it.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__
from .cli import DEFAULT_MODELS, LOCAL_URL, system_prompt
from .tools import NEEDS_APPROVAL, ToolError, Workspace, describe, validate

PAGE = (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
CONFIG_FILE = Path.home() / ".incredible-herness" / "settings.json"


class Cancelled(Exception):
    pass


class Session:
    """One agent conversation plus the event stream the page listens to."""

    def __init__(self):
        self.events: "queue.Queue[dict]" = queue.Queue()
        self.lock = threading.Lock()
        self.busy = False
        self.cancel = threading.Event()
        self.pending: dict[str, tuple[threading.Event, dict]] = {}
        self.config = {"provider": os.environ.get("MYAGENT_PROVIDER", "claude"), "model": "", "effort": "high",
                       "base_url": "", "api_key": "", "cwd": os.getcwd(), "auto": False}
        self.config["model"] = DEFAULT_MODELS[self.config["provider"]]
        self.provider = None
        self.ws = None

    # --- events to the page --------------------------------------------------------
    def emit(self, kind: str, **data):
        self.events.put({"kind": kind, **data})

    # UI protocol used by providers (raising Cancelled aborts the model stream)
    def text(self, chunk):
        self._check()
        self.emit("text", text=chunk)

    def thinking(self, chunk):
        self._check()
        self.emit("thinking", text=chunk)

    def end_block(self):
        self.emit("end_block")

    def notice(self, msg):
        self.emit("notice", text=msg)

    def _check(self):
        if self.cancel.is_set():
            raise Cancelled()

    # --- configuration ---------------------------------------------------------------
    def apply_config(self, new: dict) -> None:
        from .providers import ClaudeProvider, OpenAIProvider

        cfg = {**self.config, **{k: v for k, v in new.items() if k in self.config}}
        if cfg["provider"] not in DEFAULT_MODELS:
            raise ValueError(f"Unknown provider {cfg['provider']}")
        if not cfg["model"] or (new.get("provider") not in (None, self.config["provider"]) and not new.get("model")):
            cfg["model"] = DEFAULT_MODELS[cfg["provider"]]
        cwd = Path(os.path.expanduser(cfg["cwd"] or os.getcwd()))
        if not cwd.is_dir():
            raise ValueError(f"Folder not found: {cwd}")
        cfg["cwd"] = str(cwd.resolve())
        sp = system_prompt(cfg["cwd"])
        if cfg["provider"] == "claude":
            prov = ClaudeProvider(cfg["model"], sp, effort=cfg["effort"], api_key=cfg["api_key"] or None)
        elif cfg["provider"] == "openai":
            prov = OpenAIProvider(cfg["model"], sp, base_url=cfg["base_url"] or os.environ.get("OPENAI_BASE_URL"),
                                  api_key=cfg["api_key"] or None, label_prefix="openai")
        else:
            prov = OpenAIProvider(cfg["model"], sp, base_url=cfg["base_url"] or os.environ.get("LOCAL_BASE_URL", LOCAL_URL),
                                  api_key=cfg["api_key"] or "local", label_prefix="local")
        self.config, self.provider, self.ws = cfg, prov, Workspace(cfg["cwd"])
        self.save()

    def load(self) -> None:
        try:
            saved = json.loads(CONFIG_FILE.read_text())
            self.config.update({k: v for k, v in saved.items() if k in self.config and k != "auto"})
        except (OSError, ValueError):
            pass

    def save(self) -> None:
        try:
            CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            data = {k: v for k, v in self.config.items() if k != "auto"}  # auto-approve is never remembered
            CONFIG_FILE.write_text(json.dumps(data, indent=2))
            os.chmod(CONFIG_FILE, 0o600)  # holds the API key: readable only by you
        except OSError:
            pass

    def public_config(self) -> dict:
        c = dict(self.config)
        c["api_key"] = "set" if c["api_key"] else ""  # never send the key back to the page
        return c

    # --- tools with approval -----------------------------------------------------------
    def run_tool(self, name: str, args: dict) -> tuple[str, bool]:
        self._check()
        problem = validate(name, args)
        if problem:
            self.emit("tool", summary=f"bad tool call: {problem}", error=True)
            return f"Invalid input: {problem} Fix the call and try again.", True
        call_id = secrets.token_hex(6)
        info = {"id": call_id, "name": name, "summary": describe(name, args)}
        if name == "edit_file":
            info["old"], info["new"] = args["old_text"][:4000], args["new_text"][:4000]
        if name == "write_file":
            info["new"] = args["content"][:4000]
        if name in NEEDS_APPROVAL and not self.config["auto"]:
            done = threading.Event()
            answer: dict = {}
            self.pending[call_id] = (done, answer)
            self.emit("approve", **info)
            while not done.wait(0.25):
                self._check()
            self.pending.pop(call_id, None)
            if answer.get("decision") == "always":
                self.config["auto"] = True
                self.emit("config", config=self.public_config())
            elif answer.get("decision") != "allow":
                fb = answer.get("feedback") or "The user declined."
                self.emit("tool_result", id=call_id, output=f"Declined. {fb}", error=True)
                return f"The user did not allow this. Their reply: {fb}", True
        else:
            self.emit("tool", **info)
        try:
            out, err = self.ws.run(name, args), False
        except ToolError as e:
            out, err = str(e), True
        except Exception as e:
            out, err = f"{type(e).__name__}: {e}", True
        self.emit("tool_result", id=call_id, output=out[:6000], error=err)
        return out, err

    # --- running a message ---------------------------------------------------------------
    def send(self, text: str) -> None:
        with self.lock:
            if self.busy:
                raise RuntimeError("Still working on the last message. Press Stop first.")
            self.busy = True
            self.cancel.clear()
        threading.Thread(target=self._turn, args=(text,), daemon=True).start()

    def _turn(self, text: str) -> None:
        self.emit("status", busy=True)
        try:
            self.provider.turn(text, self.run_tool, self)
        except Cancelled:
            self.notice("Stopped.")
            self.provider.close_interrupted()
        except Exception as e:
            self.notice(f"{type(e).__name__}: {e}")
            self.provider.close_interrupted()
        finally:
            self.end_block()
            self.busy = False
            self.emit("status", busy=False)


def make_handler(session: Session, token: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _authed(self) -> bool:
            q = parse_qs(urlparse(self.path).query)
            if self.headers.get("X-Token") == token or q.get("token", [""])[0] == token:
                return True
            self.send_error(403)
            return False

        def _json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/":
                if not self._authed():
                    return
                body = PAGE.replace("__TOKEN__", token).replace("__VERSION__", __version__).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/events":
                if not self._authed():
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                try:
                    self.wfile.write(f"data: {json.dumps({'kind': 'config', 'config': session.public_config()})}\n\n".encode())
                    self.wfile.flush()
                    while True:
                        try:
                            ev = session.events.get(timeout=15)
                            self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
                        except queue.Empty:
                            self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
            else:
                self.send_error(404)

        def do_POST(self):
            if not self._authed():
                return
            try:
                data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                path = urlparse(self.path).path
                if path == "/api/send":
                    session.send(str(data.get("text", "")).strip())
                elif path == "/api/approve":
                    item = session.pending.get(data.get("id"))
                    if item:
                        item[1].update(decision=data.get("decision"), feedback=data.get("feedback", ""))
                        item[0].set()
                elif path == "/api/stop":
                    session.cancel.set()
                elif path == "/api/clear":
                    session.provider.clear()
                elif path == "/api/config":
                    if session.busy:
                        raise RuntimeError("Stop the current task before changing settings.")
                    session.apply_config(data)
                    session.emit("config", config=session.public_config())
                else:
                    return self._json({"error": "not found"}, 404)
                self._json({"ok": True})
            except Exception as e:
                self._json({"error": str(e)}, 400)

    return Handler


def start_server(cwd: str | None = None, port: int = 0):
    """Start the engine on 127.0.0.1. Returns (server, url). port=0 picks a free port."""
    session = Session()
    session.load()
    try:
        session.apply_config({"cwd": cwd} if cwd else {})
    except Exception as e:  # e.g. no API key yet: the window lets the user fix settings
        session.config["cwd"] = cwd or session.config["cwd"] or str(Path.home())
        session.emit("notice", text=f"Setup needed: {e}. Enter your settings above and press Apply.")
    token = secrets.token_urlsafe(16)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(session, token))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}/?token={token}"


def main(argv=None):
    p = argparse.ArgumentParser(prog="myagent-gui", description="Your own AI agent, in a browser window.")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--cwd", default=os.getcwd(), help="Working folder (can be changed in the app).")
    p.add_argument("--no-browser", action="store_true")
    args = p.parse_args(argv)

    server, url = start_server(args.cwd, args.port)
    print(f"myagent {__version__} running at {url}\nPress Ctrl-C here to quit.")
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
