"""Code intelligence through real language servers (LSP), when they are installed.

Uses whichever server is on this computer for the file's language: Pyright / pylsp (Python), typescript-language-server
(JS/TS), gopls (Go), rust-analyzer (Rust), clangd (C/C++), sourcekit-lsp (Swift), solargraph (Ruby).
Gives exact go-to-definition, references, hover (types and docs) and diagnostics. Without a server the agent falls back to
the pattern-based code_symbols / find_references tools.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

SERVERS = [  # (extensions, language id, candidate commands)
    ({".py", ".pyi"}, "python", [["pyright-langserver", "--stdio"], ["basedpyright-langserver", "--stdio"], ["pylsp"]]),
    ({".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}, "typescript", [["typescript-language-server", "--stdio"]]),
    ({".go"}, "go", [["gopls"]]),
    ({".rs"}, "rust", [["rust-analyzer"]]),
    ({".c", ".h", ".cc", ".cpp", ".hpp", ".cxx", ".m", ".mm"}, "cpp", [["clangd"]]),
    ({".swift"}, "swift", [["sourcekit-lsp"]]),
    ({".rb"}, "ruby", [["solargraph", "stdio"]]),
]
LANG_IDS = {".tsx": "typescriptreact", ".jsx": "javascriptreact", ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
            ".c": "c", ".h": "c", ".m": "objective-c", ".mm": "objective-cpp"}
EXTRA_PATH = ["/opt/homebrew/bin", "/usr/local/bin", str(Path.home() / ".local/bin"), str(Path.home() / ".npm-global/bin"),
              str(Path.home() / "go/bin"), str(Path.home() / ".cargo/bin"), "/usr/bin"]
INSTALL_HINT = ("No language server is installed for this kind of file. For exact results install one, e.g. "
                "`npm i -g pyright typescript-language-server typescript` (Python, JS/TS), `brew install gopls` (Go), "
                "`rustup component add rust-analyzer` (Rust), `brew install llvm` (clangd). Meanwhile, use code_symbols "
                "and find_references.")


LSP_HOME = Path.home() / ".incredible-herness" / "lsp"  # servers installed from Settings live here (no admin rights needed)


def search_path() -> str:
    parts = [str(LSP_HOME / "node_modules" / ".bin")] + os.environ.get("PATH", "").split(os.pathsep)
    nvm = Path.home() / ".nvm/versions/node"
    if nvm.is_dir():
        parts += [str(p / "bin") for p in sorted(nvm.iterdir(), reverse=True)]
    return os.pathsep.join(dict.fromkeys(parts + EXTRA_PATH))


def find_server(path: Path):
    """(command, language id) for this file, or None."""
    ext = path.suffix.lower()
    for exts, lang, cmds in SERVERS:
        if ext in exts:
            for cmd in cmds:
                exe = shutil.which(cmd[0], path=search_path())
                if exe:
                    return [exe] + cmd[1:], LANG_IDS.get(ext, lang)
    return None


def available() -> list[str]:
    seen = []
    for _, _, cmds in SERVERS:
        for cmd in cmds:
            if shutil.which(cmd[0], path=search_path()):
                seen.append(cmd[0])
    return seen


def uri(p: Path) -> str:
    return "file://" + quote(str(p.resolve()).replace("\\", "/") if os.name != "nt" else "/" + str(p.resolve()).replace("\\", "/"))


def path_of(u: str) -> Path:
    p = unquote(urlparse(u).path)
    return Path(p[1:] if os.name == "nt" and p.startswith("/") else p)


class LspError(Exception):
    pass


class Client:
    def __init__(self, cmd: list[str], root: Path):
        env = {**os.environ, "PATH": search_path()}
        self.proc = subprocess.Popen(cmd, cwd=root, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     env=env)
        self.root, self.n, self.lock = root, 0, threading.Lock()
        self.pending: dict[int, dict] = {}
        self.diags: dict[str, list] = {}
        self.diag_event = threading.Condition()
        self.versions: dict[str, int] = {}
        threading.Thread(target=self._read, daemon=True).start()
        self.request("initialize", {"processId": os.getpid(), "rootUri": uri(root), "workspaceFolders": [{"uri": uri(root), "name": root.name}],
                                    "capabilities": {"textDocument": {"publishDiagnostics": {}, "hover": {"contentFormat": ["plaintext", "markdown"]},
                                                                      "definition": {}, "references": {}, "synchronization": {"didSave": True}}}},
                     timeout=30)
        self.notify("initialized", {})

    def _send(self, msg: dict):
        body = json.dumps(msg).encode()
        with self.lock:
            self.proc.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
            self.proc.stdin.flush()

    def _read(self):
        out = self.proc.stdout
        while True:
            length = None
            while True:
                line = out.readline()
                if not line:
                    return
                line = line.strip()
                if not line:
                    break
                if line.lower().startswith(b"content-length:"):
                    length = int(line.split(b":")[1])
            if length is None:
                continue
            msg = json.loads(out.read(length))
            if "id" in msg and ("result" in msg or "error" in msg):
                slot = self.pending.get(msg["id"])
                if slot is not None:
                    slot["msg"] = msg
                    slot["event"].set()
            elif msg.get("method") == "textDocument/publishDiagnostics":
                with self.diag_event:
                    self.diags[msg["params"]["uri"]] = msg["params"].get("diagnostics", [])
                    self.diag_event.notify_all()
            elif "id" in msg and "method" in msg:  # a request from the server (e.g. workspace/configuration)
                result = [None] * len(msg.get("params", {}).get("items", [])) if msg["method"] == "workspace/configuration" else None
                self._send({"jsonrpc": "2.0", "id": msg["id"], "result": result})

    def request(self, method: str, params: dict, timeout: float = 15):
        self.n += 1
        slot = {"event": threading.Event()}
        self.pending[self.n] = slot
        self._send({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params})
        if not slot["event"].wait(timeout):
            self.pending.pop(self.n, None)
            raise LspError(f"The language server did not answer {method} in time.")
        msg = self.pending.pop(self.n)["msg"]
        if "error" in msg:
            raise LspError(msg["error"].get("message", "language server error"))
        return msg.get("result")

    def notify(self, method: str, params: dict):
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def sync(self, path: Path, lang: str):
        u, text = uri(path), path.read_text(errors="replace")
        with self.diag_event:
            self.diags.pop(u, None)  # cleared before sending, so the fresh report can't be missed
        if u in self.versions:
            self.versions[u] += 1
            self.notify("textDocument/didChange", {"textDocument": {"uri": u, "version": self.versions[u]},
                                                   "contentChanges": [{"text": text}]})
            self.notify("textDocument/didSave", {"textDocument": {"uri": u}})
        else:
            self.versions[u] = 1
            self.notify("textDocument/didOpen", {"textDocument": {"uri": u, "languageId": lang, "version": 1, "text": text}})
        return u

    def alive(self) -> bool:
        return self.proc.poll() is None

    def close(self):
        try:
            self.request("shutdown", {}, timeout=3)
            self.notify("exit", {})
        except Exception:
            pass
        self.proc.kill()


class Manager:
    def __init__(self):
        self.clients: dict[tuple, Client] = {}
        self.lock = threading.Lock()

    def client(self, path: Path, root: Path):
        found = find_server(path)
        if not found:
            return None, None
        cmd, lang = found
        key = (cmd[0], str(root))
        with self.lock:
            c = self.clients.get(key)
            if c is None or not c.alive():
                c = self.clients[key] = Client(cmd, root)
        return c, lang

    def close_all(self):
        for c in list(self.clients.values()):
            c.close()
        self.clients.clear()

    # ---- actions used by the code_intel tool ----
    def run(self, root: Path, path: Path, action: str, line: int | None, column: int | None, symbol: str | None) -> str:
        if not path.is_file():
            raise LspError(f"No such file: {path}")
        c, lang = self.client(path, root)
        if c is None:
            raise LspError(INSTALL_HINT)
        u = c.sync(path, lang)
        if action == "diagnostics":
            return _fmt_diags(root, path, self.wait_diags(c, u, 8))
        if not line:
            if not symbol:
                raise LspError("Give line and column (1-based), or a symbol name to look up in this file.")
            for i, text in enumerate(path.read_text(errors="replace").splitlines(), 1):
                col = _find_word(text, symbol)
                if col >= 0:
                    line, column = i, col + 1
                    break
            else:
                raise LspError(f"{symbol} does not appear in {path.name}.")
        pos = {"textDocument": {"uri": u}, "position": {"line": line - 1, "character": max(0, (column or 1) - 1)}}
        if action == "hover":
            r = c.request("textDocument/hover", pos)
            if not r:
                return "No information at that position."
            cont = r.get("contents")
            if isinstance(cont, dict):
                return cont.get("value", "")
            if isinstance(cont, list):
                return "\n".join(x.get("value", "") if isinstance(x, dict) else str(x) for x in cont)
            return str(cont)
        if action == "definition":
            r = c.request("textDocument/definition", pos)
        elif action == "references":
            r = c.request("textDocument/references", {**pos, "context": {"includeDeclaration": True}})
        else:
            raise LspError("action must be definition, references, hover or diagnostics")
        locs = r if isinstance(r, list) else ([r] if r else [])
        if not locs:
            return f"No {action} found."
        out = []
        for loc in locs[:200]:
            target = loc.get("uri") or loc.get("targetUri")
            rng = loc.get("range") or loc.get("targetSelectionRange") or loc.get("targetRange")
            p = path_of(target)
            ln = rng["start"]["line"] + 1
            try:
                src = p.read_text(errors="replace").splitlines()[ln - 1].strip()[:160]
            except (OSError, IndexError):
                src = ""
            out.append(f"{_rel(root, p)}:{ln}:{rng['start']['character'] + 1}  {src}")
        return "\n".join(out)

    def wait_diags(self, c: Client, u: str, timeout: float):
        end = time.time() + timeout
        with c.diag_event:
            while u not in c.diags and time.time() < end:
                c.diag_event.wait(end - time.time())
            return c.diags.get(u, [])

    def errors_after_edit(self, root: Path, path: Path, timeout: float = 6) -> str | None:
        """Errors the language server reports for a just-edited file (None if no server or no errors)."""
        try:
            c, lang = self.client(path, root)
            if c is None:
                return None
            u = c.sync(path, lang)
            errs = [d for d in self.wait_diags(c, u, timeout) if d.get("severity", 1) == 1]
            return _fmt_diags(root, path, errs) if errs else None
        except Exception:
            return None


MANAGER = Manager()


def _find_word(text: str, word: str) -> int:
    import re
    m = re.search(rf"(?<![\w$]){re.escape(word)}(?![\w$])", text)
    return m.start() if m else -1


def _rel(root: Path, p: Path) -> str:
    try:
        return p.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(p)


def _fmt_diags(root: Path, path: Path, diags: list) -> str:
    if not diags:
        return f"No problems reported in {path.name}."
    sev = {1: "error", 2: "warning", 3: "info", 4: "hint"}
    return "\n".join(f"{_rel(root, path)}:{d['range']['start']['line'] + 1}:{d['range']['start']['character'] + 1}: "
                     f"{sev.get(d.get('severity', 1), 'error')}: {d.get('message', '').strip()}" for d in diags[:100])


# ---------------------------------------------------------------- one-click setup (Settings > Tools)
LANGS = [  # id, label, server names to look for, how to install: (needs tool, command)
    ("python", "Python", ["pyright-langserver", "basedpyright-langserver", "pylsp"], ("npm", ["install", "--prefix", "{home}", "pyright"])),
    ("typescript", "JavaScript & TypeScript", ["typescript-language-server"], ("npm", ["install", "--prefix", "{home}", "typescript-language-server", "typescript"])),
    ("go", "Go", ["gopls"], ("go", ["install", "golang.org/x/tools/gopls@latest"])),
    ("rust", "Rust", ["rust-analyzer"], ("rustup", ["component", "add", "rust-analyzer"])),
    ("cpp", "C, C++ & Objective-C", ["clangd"], None),
    ("swift", "Swift", ["sourcekit-lsp"], None),
]
MANUAL = {"cpp": "Comes with Xcode Command Line Tools on Mac (xcode-select --install) or LLVM (brew install llvm).",
          "swift": "Comes with Xcode Command Line Tools on Mac (xcode-select --install)."}


def status() -> list[dict]:
    out = []
    for lid, label, names, how in LANGS:
        found = next((n for n in names if shutil.which(n, path=search_path())), None)
        tool = how[0] if how else None
        out.append({"id": lid, "label": label, "server": found, "can_install": bool(how and shutil.which(tool, path=search_path())),
                    "needs": tool, "manual": MANUAL.get(lid, "")})
    return out


def install(lang: str, timeout: int = 900) -> str:
    """Install the language server for `lang`. Returns its output; raises LspError with the reason on failure."""
    entry = next((e for e in LANGS if e[0] == lang), None)
    if not entry or not entry[3]:
        raise LspError(MANUAL.get(lang, "This language server can't be installed from here."))
    tool, args = entry[3]
    exe = shutil.which(tool, path=search_path())
    if not exe:
        need = {"npm": "Node.js (https://nodejs.org or brew install node)", "go": "Go (https://go.dev/dl or brew install go)",
                "rustup": "Rust (https://rustup.rs)"}[tool]
        raise LspError(f"Installing this needs {need} first.")
    LSP_HOME.mkdir(parents=True, exist_ok=True)
    cmd = [exe] + [a.replace("{home}", str(LSP_HOME)) for a in args]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env={**os.environ, "PATH": search_path()},
                           stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise LspError("The install took too long and was stopped.")
    out = (r.stdout + r.stderr)[-3000:]
    if r.returncode != 0:
        raise LspError(f"The install failed:\n{out}")
    if not any(shutil.which(n, path=search_path()) for n in entry[2]):
        raise LspError(f"The install finished but the server wasn't found.\n{out}")
    MANAGER.close_all()
    return out
