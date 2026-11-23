"""Remote MCP servers (Streamable HTTP) for every provider, with one-click OAuth sign-in.

Sign-in follows the MCP authorization spec: discover the authorization server from the server's protected-resource
metadata, register LightCode⁴ automatically (dynamic client registration), then open the provider's sign-in page in the
browser with PKCE and receive the code on a temporary 127.0.0.1 address. Tokens are refreshed automatically.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

PROTOCOL = "2025-06-18"
UA = {"User-Agent": "LightCode4/1.0"}


class McpRemoteError(Exception):
    pass


class NeedsSignIn(McpRemoteError):
    pass


def _http(url: str, data: bytes | None = None, headers: dict | None = None, method: str | None = None, timeout=60):
    req = urllib.request.Request(url, data=data, headers={**UA, **(headers or {})}, method=method)
    return urllib.request.urlopen(req, timeout=timeout)


def _json(url: str, data: dict | None = None, form: dict | None = None) -> dict:
    if form is not None:
        body, ctype = urllib.parse.urlencode(form).encode(), "application/x-www-form-urlencoded"
    elif data is not None:
        body, ctype = json.dumps(data).encode(), "application/json"
    else:
        body, ctype = None, None
    try:
        with _http(url, body, {"Accept": "application/json", **({"Content-Type": ctype} if ctype else {})}) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise McpRemoteError(f"{url} answered {e.code}: {e.read()[:300].decode(errors='replace')}")


# ------------------------------------------------------------------ OAuth sign-in
def _discover(server_url: str) -> dict:
    u = urllib.parse.urlparse(server_url)
    origin = f"{u.scheme}://{u.netloc}"
    resource_meta = None
    try:  # 401 with WWW-Authenticate: Bearer resource_metadata="..."
        _http(server_url, json.dumps({"jsonrpc": "2.0", "id": 0, "method": "ping"}).encode(),
              {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}, timeout=20).close()
    except urllib.error.HTTPError as e:
        hdr = e.headers.get("WWW-Authenticate", "")
        if 'resource_metadata="' in hdr:
            resource_meta = hdr.split('resource_metadata="', 1)[1].split('"', 1)[0]
    candidates = [resource_meta] if resource_meta else []
    candidates += [f"{origin}/.well-known/oauth-protected-resource{u.path}", f"{origin}/.well-known/oauth-protected-resource"]
    auth_server = None
    for c in candidates:
        try:
            meta = _json(c)
            auth_server = (meta.get("authorization_servers") or [None])[0]
            if auth_server:
                break
        except Exception:
            continue
    auth_server = (auth_server or origin).rstrip("/")
    a = urllib.parse.urlparse(auth_server)
    for path in (f"/.well-known/oauth-authorization-server{a.path}", f"/.well-known/openid-configuration{a.path}",
                 "/.well-known/oauth-authorization-server"):
        try:
            return _json(f"{a.scheme}://{a.netloc}{path}")
        except Exception:
            continue
    raise McpRemoteError("This connector doesn't offer sign-in (no OAuth metadata). Paste a token instead.")


def sign_in(server_url: str, open_browser=webbrowser.open, timeout: float = 300) -> dict:
    """Run the browser sign-in and return the saved OAuth state (tokens + client)."""
    meta = _discover(server_url)
    if not meta.get("registration_endpoint"):
        raise McpRemoteError("This service doesn't support automatic app registration. Paste a token instead.")
    result: dict = {}
    done = threading.Event()
    state = secrets.token_urlsafe(16)

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if q.get("state", [""])[0] == state:
                result.update(code=q.get("code", [""])[0], error=q.get("error_description", q.get("error", [""]))[0])
                done.set()
            body = ("<html><body style='font:16px sans-serif;padding:40px'>Signed in. You can close this tab and "
                    "return to LightCode⁴.</body></html>").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)

    srv = HTTPServer(("127.0.0.1", 0), Callback)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    redirect = f"http://127.0.0.1:{srv.server_port}/callback"
    try:
        reg = _json(meta["registration_endpoint"], {
            "client_name": "LightCode⁴", "redirect_uris": [redirect], "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"], "token_endpoint_auth_method": "none"})
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        params = {"response_type": "code", "client_id": reg["client_id"], "redirect_uri": redirect, "state": state,
                  "code_challenge": challenge, "code_challenge_method": "S256", "resource": server_url}
        if meta.get("scopes_supported"):
            params["scope"] = " ".join(meta["scopes_supported"])
        open_browser(meta["authorization_endpoint"] + ("&" if "?" in meta["authorization_endpoint"] else "?")
                     + urllib.parse.urlencode(params))
        if not done.wait(timeout):
            raise McpRemoteError("Sign-in timed out. Try again and finish signing in within 5 minutes.")
        if not result.get("code"):
            raise McpRemoteError(f"Sign-in was cancelled or refused: {result.get('error') or 'no code returned'}")
        form = {"grant_type": "authorization_code", "code": result["code"], "redirect_uri": redirect,
                "client_id": reg["client_id"], "code_verifier": verifier, "resource": server_url}
        if reg.get("client_secret"):
            form["client_secret"] = reg["client_secret"]
        tok = _json(meta["token_endpoint"], form=form)
    finally:
        srv.shutdown()
    return {"access_token": tok["access_token"], "refresh_token": tok.get("refresh_token", ""),
            "expires_at": time.time() + float(tok.get("expires_in") or 3600) - 60, "client_id": reg["client_id"],
            "client_secret": reg.get("client_secret", ""), "token_endpoint": meta["token_endpoint"], "signed_in_at": time.time()}


def refresh(oauth: dict, server_url: str) -> dict:
    if not oauth.get("refresh_token"):
        raise NeedsSignIn("The sign-in expired. Click Sign in again.")
    form = {"grant_type": "refresh_token", "refresh_token": oauth["refresh_token"], "client_id": oauth["client_id"],
            "resource": server_url}
    if oauth.get("client_secret"):
        form["client_secret"] = oauth["client_secret"]
    try:
        tok = _json(oauth["token_endpoint"], form=form)
    except McpRemoteError:
        raise NeedsSignIn("The sign-in expired. Click Sign in again.")
    return {**oauth, "access_token": tok["access_token"], "refresh_token": tok.get("refresh_token") or oauth["refresh_token"],
            "expires_at": time.time() + float(tok.get("expires_in") or 3600) - 60}


# ------------------------------------------------------------------ Streamable HTTP client
class RemoteServer:
    """Same interface as mcp_local.McpServer: start(), tools, call(), stop()."""

    def __init__(self, name: str, url: str, token: str = "", oauth: dict | None = None, on_oauth_update=None):
        self.name, self.url, self.token, self.oauth = name, url, token, oauth
        self.on_oauth_update = on_oauth_update
        self.session_id = ""
        self.tools: list[dict] = []
        self.error = ""
        self.proc = True  # "running" marker for status()
        self.next_id = 1
        self.lock = threading.Lock()

    def _auth(self) -> dict:
        if self.oauth:
            if time.time() > self.oauth.get("expires_at", 0):
                self.oauth = refresh(self.oauth, self.url)
                if self.on_oauth_update:
                    self.on_oauth_update(self.name, self.oauth)
            return {"Authorization": f"Bearer {self.oauth['access_token']}"}
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def _post(self, msg: dict, expect_id: int | None):
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "MCP-Protocol-Version": PROTOCOL, **self._auth()}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        try:
            r = _http(self.url, json.dumps(msg).encode(), headers, timeout=300)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise NeedsSignIn(f"{self.name}: sign-in needed. Open Settings > Connectors and click Sign in.")
            raise McpRemoteError(f"{self.name} answered {e.code}: {e.read()[:200].decode(errors='replace')}")
        with r:
            self.session_id = r.headers.get("Mcp-Session-Id") or self.session_id
            if expect_id is None:
                return None
            body = r.read().decode(errors="replace")
            if "text/event-stream" in (r.headers.get("Content-Type") or ""):
                for block in body.split("\n\n"):
                    data = "".join(l[5:].strip() for l in block.splitlines() if l.startswith("data:"))
                    if data:
                        m = json.loads(data)
                        if m.get("id") == expect_id:
                            return m
                raise McpRemoteError(f"{self.name}: no reply in the event stream.")
            return json.loads(body)

    def request(self, method: str, params: dict) -> dict:
        with self.lock:
            rid = self.next_id
            self.next_id += 1
        m = self._post({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}, rid)
        if "error" in m:
            raise McpRemoteError(f"{self.name}: {m['error'].get('message', m['error'])}")
        return m.get("result", {})

    def start(self):
        self.request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                    "clientInfo": {"name": "incredible-herness", "version": "1"}})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, None)
        tools, cursor = [], None
        while True:
            res = self.request("tools/list", {"cursor": cursor} if cursor else {})
            tools += res.get("tools", [])
            cursor = res.get("nextCursor")
            if not cursor:
                break
        self.tools = tools

    def call(self, tool: str, args: dict):
        from .mcp_local import McpServer
        res = self.request("tools/call", {"name": tool, "arguments": args})
        return McpServer._parse_call(res)

    def stop(self):
        pass
