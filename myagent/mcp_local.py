"""Local MCP servers: programs on this computer (filesystem, databases, Slack, GitHub, ...) that offer tools over
the Model Context Protocol (JSON-RPC 2.0 over stdin/stdout). Their tools are offered to every provider as
mcp_<server>_<tool>, and each call goes through the normal approval rules (rule kind: mcp).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
from concurrent.futures import Future

PROTOCOL = "2025-06-18"


class McpError(Exception):
    pass


def tool_name(server: str, tool: str) -> str:
    return ("mcp_" + re.sub(r"[^A-Za-z0-9_]", "_", f"{server}_{tool}"))[:64]


class McpServer:
    def __init__(self, name: str, command: str, args: list[str] | None = None, env: dict | None = None):
        self.name, self.command, self.args, self.env = name, command, args or [], env or {}
        self.proc: subprocess.Popen | None = None
        self.pending: dict[int, Future] = {}
        self.next_id = 1
        self.lock = threading.Lock()
        self.tools: list[dict] = []
        self.error = ""
        self.stderr_tail: list[str] = []

    def start(self):
        exe = shutil.which(self.command) or self.command
        self.proc = subprocess.Popen([exe, *self.args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, env={**os.environ, **self.env}, text=True, encoding="utf-8",
                                     bufsize=1, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()
        self.request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                    "clientInfo": {"name": "incredible-herness", "version": "1"}}, timeout=60)
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        tools, cursor = [], None
        while True:
            res = self.request("tools/list", {"cursor": cursor} if cursor else {}, timeout=60)
            tools += res.get("tools", [])
            cursor = res.get("nextCursor")
            if not cursor:
                break
        self.tools = tools

    def _send(self, msg: dict):
        if not self.proc or self.proc.poll() is not None:
            raise McpError(f"MCP server '{self.name}' is not running. {' '.join(self.stderr_tail[-3:])}")
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: dict, timeout: float = 120) -> dict:
        with self.lock:
            rid = self.next_id
            self.next_id += 1
            fut = Future()
            self.pending[rid] = fut
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        try:
            msg = fut.result(timeout=timeout)
        except TimeoutError:
            raise McpError(f"MCP server '{self.name}' did not answer {method} in {timeout:.0f}s.")
        finally:
            self.pending.pop(rid, None)
        if "error" in msg:
            raise McpError(f"{self.name}: {msg['error'].get('message', msg['error'])}")
        return msg.get("result", {})

    def _read(self):
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if "id" in msg and msg["id"] in self.pending and ("result" in msg or "error" in msg):
                self.pending[msg["id"]].set_result(msg)
            elif "id" in msg and "method" in msg:  # server->client request we don't support (e.g. sampling)
                try:
                    self._send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "Not supported"}})
                except McpError:
                    pass
        for fut in list(self.pending.values()):
            if not fut.done():
                fut.set_result({"error": {"message": "server exited"}})

    def _read_err(self):
        for line in self.proc.stderr:
            self.stderr_tail = (self.stderr_tail + [line.strip()])[-20:]

    def call(self, tool: str, args: dict):
        return self._parse_call(self.request("tools/call", {"name": tool, "arguments": args}, timeout=300))

    @staticmethod
    def _parse_call(res: dict):
        parts = []
        for c in res.get("content", []):
            if c.get("type") == "text":
                parts.append({"type": "text", "text": c["text"]})
            elif c.get("type") == "image":
                parts.append({"type": "image", "media_type": c.get("mimeType", "image/png"), "data": c["data"]})
            elif c.get("type") == "resource":
                r = c.get("resource", {})
                parts.append({"type": "text", "text": r.get("text") or f"[resource {r.get('uri')}]"})
        if not parts and "structuredContent" in res:
            parts.append({"type": "text", "text": json.dumps(res["structuredContent"])[:30000]})
        return parts or [{"type": "text", "text": "(no output)"}], bool(res.get("isError"))

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


class McpManager:
    """Starts configured servers on first use and maps exposed tool names back to (server, tool)."""

    def __init__(self):
        self.servers: dict = {}
        self.config_key = ""
        self.names: dict[str, tuple[str, str]] = {}
        self.started: set[str] = set()
        self.on_oauth_update = None  # (name, oauth) -> None: persist refreshed tokens

    def configure(self, entries: list[dict], remote: list[dict] | None = None):
        entries = [e for e in entries if e.get("name") and e.get("command") and e.get("enabled", True)]
        remote = [r for r in (remote or []) if r.get("name") and r.get("url")]
        key = json.dumps([entries, [{k: v for k, v in r.items() if k != "oauth"} | {"signed": bool(r.get("oauth"))} for r in remote]],
                         sort_keys=True)
        if key == self.config_key:
            for r in remote:  # keep refreshed tokens current without restarting
                srv = self.servers.get(r["name"])
                if (srv is not None and hasattr(srv, "oauth") and r.get("oauth")
                        and r["oauth"].get("expires_at", 0) > (srv.oauth or {}).get("expires_at", 0)):
                    srv.oauth = r["oauth"]
            return
        self.stop()
        self.config_key, self.names, self.started = key, {}, set()
        from .mcp_remote import RemoteServer
        for r in remote:
            self.servers[r["name"]] = RemoteServer(r["name"], r["url"], r.get("token", ""), r.get("oauth"),
                                                   lambda n, o: self.on_oauth_update and self.on_oauth_update(n, o))
        for e in entries:
            args = e.get("args") or []
            if isinstance(args, str):
                import shlex
                args = shlex.split(args)
            args = [os.path.expanduser(a) if a.startswith("~") else a for a in args]
            self.servers[e["name"]] = McpServer(e["name"], e["command"], args, e.get("env") or {})

    def tool_specs(self) -> list[dict]:
        specs = []
        for srv in self.servers.values():
            if srv.name not in self.started:
                self.started.add(srv.name)
                try:
                    srv.start()
                    srv.error = ""
                except Exception as e:
                    srv.error = f"{e} {' '.join(getattr(srv, 'stderr_tail', [])[-3:])}".strip()
            for t in srv.tools:
                n = tool_name(srv.name, t["name"])
                self.names[n] = (srv.name, t["name"])
                schema = t.get("inputSchema") or {"type": "object", "properties": {}}
                schema.setdefault("type", "object")
                specs.append({"name": n, "kind": "mcp", "schema": schema,
                              "description": f"[{srv.name}] {t.get('description') or t['name']}"[:1000]})
        return specs

    def status(self) -> list[dict]:
        out = []
        for s in self.servers.values():
            running = bool(s.tools) if not hasattr(s, "stderr_tail") else bool(s.proc and s.proc.poll() is None)
            out.append({"name": s.name, "running": running, "tools": len(s.tools), "error": s.error,
                        "remote": not hasattr(s, "stderr_tail")})
        return out

    def call(self, name: str, args: dict):
        if name not in self.names:
            raise McpError(f"Unknown MCP tool {name}.")
        server, tool = self.names[name]
        return self.servers[server].call(tool, args)

    def stop(self):
        for s in self.servers.values():
            s.stop()
        self.servers = {}
