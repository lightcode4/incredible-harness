"""The app engine behind the window.

Session (the app): settings, the shared browser/terminals/computer, scheduler, phone access, and any number of
Chats. Each Chat has its own model conversation, working folder (optionally its own git worktree), approvals,
undo checkpoints, costs and task list, so several chats can run at the same time (parallel agents).

Security: the window server listens only on 127.0.0.1; phone access (off by default) listens on the local network
with its own long random token. Every request must carry the token.
"""

from __future__ import annotations

import argparse
import base64
import fnmatch
import json
import os
import platform
import queue
import re
import secrets
import shlex
import socket
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__, extras, gitops
from . import platform_extras as px
from .mcp_local import McpError, McpManager
from ._build import BUILD, REPO
from .browser import Browser, NeedsBrowser, install_chromium
from .computer import Computer, ComputerError
from .providers import ClaudeProvider, OpenAIProvider, cost_of
from .terminal import Terminal
from .tools import (FILE_CHANGING, MUTATING, READONLY_TOOLS, TOOLS, ToolError, Workspace, describe,
                    project_instructions, validate)

PAGE = (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
HOME = Path.home() / ".incredible-herness"
CONFIG_FILE = HOME / "settings.json"
CHATS = HOME / "chats"
DEFAULT_MODELS = {"claude": "claude-opus-5-5", "openai": "gpt-5", "local": "llama3.1"}
LOCAL_URL = "http://localhost:11434/v1"
IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}


def system_prompt(cwd: str, instructions: str = "", memory: str = "", computer: bool = False, language: str = "") -> str:
    s = (
        "You are Incredible Herness, a capable software and computer agent working on the user's machine. "
        "You can run shell commands, read, write and edit files (including PDFs and Jupyter notebooks), search, "
        "fetch web pages, use your own built-in browser (navigate, read, click, type, screenshot), keep a task list, "
        "start read-only research sub-agents, open previews and remember the user's lasting preferences. "
        f"The working directory is {cwd} on {platform.system()} {platform.release()} ({platform.machine()}).\n\n"
        "How to work:\n"
        "- For tasks with 3 or more steps, write a task list with todo_write first and keep it updated.\n"
        "- Look before you change things: read the relevant files and understand the code.\n"
        "- Make focused edits; prefer edit_file/multi_edit over rewriting whole files.\n"
        "- After edits, the app reports syntax/type problems in the tool result. Fix them before moving on.\n"
        "- Verify your work: run it, run its tests, or open it in the browser and take a screenshot. "
        "If something fails, find and fix the cause.\n"
        "- Run servers and long jobs with run_in_background, then check them with bash_output.\n"
        "- Use git for version control when the folder is a repository, but only commit or push when asked.\n"
        "- Use the task tool to research large codebases without filling this conversation.\n"
        "- Ask the user when a decision is genuinely theirs. Keep replies short and concrete; say plainly what you "
        "changed, what you verified, and what you could not verify."
    )
    if computer:
        s += ("\n- You can also see and control the user's screen with the desktop_* tools. Take a screenshot first, "
              "act in small steps, and check the result with another screenshot.")
    if language:
        s += f"\n- Always write your replies to the user in {language}."
    if memory.strip():
        s += "\n\nWhat you remember about the user (from earlier chats):\n" + memory
    if instructions:
        s += "\n\nProject instructions from the user (follow them):\n" + instructions
    return s


SUBAGENT_SYSTEM = ("You are a research sub-agent with read-only tools. Investigate exactly what you are asked, "
                   "then reply with a concise, specific report (file paths, line numbers, facts). Do not ask questions.")


class Stop(Exception):
    pass


# Website addresses people paste, mapped to the service's real OpenAI-compatible API address.
API_ADDRESS_FIXES = {
    "https://app.haimaker.ai": "https://api.haimaker.ai/v1", "https://haimaker.ai": "https://api.haimaker.ai/v1",
    "https://opencode.ai": "https://opencode.ai/inference/openai/v1", "https://openrouter.ai": "https://openrouter.ai/api/v1",
    "https://groq.com": "https://api.groq.com/openai/v1", "https://console.groq.com": "https://api.groq.com/openai/v1",
    "https://platform.openai.com": "", "https://chatgpt.com": "", "https://together.ai": "https://api.together.xyz/v1",
    "https://platform.deepseek.com": "https://api.deepseek.com/v1",
}


def fix_base_url(url: str) -> str:
    u = (url or "").strip().rstrip("/")
    if u.endswith("/chat/completions"):
        u = u[: -len("/chat/completions")]
    return API_ADDRESS_FIXES.get(u, u)


def explain_error(e: Exception, cfg: dict) -> str:
    """Turn provider errors into plain words with the fix."""
    code = getattr(e, "status_code", None)
    msg = str(getattr(e, "message", "") or e)
    low = msg.lower()
    svc = cfg.get("base_url") or {"claude": "Anthropic", "openai": "OpenAI", "local": "your local model server"}.get(cfg.get("provider"), "the provider")
    if type(e).__name__ in ("APIConnectionError", "APITimeoutError", "ConnectError"):
        return (f"Could not reach {svc}. Check your internet connection" +
                (", and that Ollama or LM Studio is running" if cfg.get("provider") == "local" else "") + ".")
    if code == 401:
        return "The API key was rejected. Paste a valid key in Settings > Models."
    if code == 402 or "insufficient balance" in low or "credit" in low and "add" in low:
        return "Your account has no credit left with this provider. Add credit on their website (billing page), then try again."
    if code == 403:
        return f"This model isn't enabled for your account ({cfg.get('model')}). Choose another model in Settings > Models, or enable it on the provider's website. Details: {msg[:200]}"
    if code in (404, 405):
        return (f"The service address looks wrong (error {code}). In Settings > Models, use a preset or check the Base URL — "
                f"currently {cfg.get('base_url') or 'the default'}. Also check that the model name exists.")
    if code == 410 or "deprecated" in low:
        return f"The model {cfg.get('model')} has been retired. Pick another one in Settings > Models (use Load models)."
    if code == 429:
        return ("The provider is rate-limiting you or a free quota is used up. Wait a minute and retry, or choose a paid model. "
                f"Details: {msg[:200]}")
    if code and code >= 500:
        return f"The provider had a server error ({code}). Try again in a moment."
    return f"{type(e).__name__}: {msg[:400]}"


def _rule_kind(name: str) -> str:
    if name.startswith("browser_"):
        return "browser"
    if name.startswith("desktop_"):
        return "computer"
    if name.startswith("mcp_"):
        return "mcp"
    return {"bash": "bash", "kill_shell": "bash", "write_file": "write", "edit_file": "edit", "multi_edit": "edit",
            "notebook_edit": "edit"}.get(name, name)


def rule_matches(rule: str, name: str, target: str) -> bool:
    m = re.match(r"^\s*(\w+)\s*(?:\((.*)\))?\s*$", rule)
    if not m or m.group(1) != _rule_kind(name):
        return False
    return m.group(2) is None or fnmatch.fnmatch(target, m.group(2).strip())


class EventHub:
    """Every connected window (and phone) gets every event."""

    def __init__(self):
        self.subs: list[queue.Queue] = []
        self.lock = threading.Lock()
        self._default: queue.Queue | None = None

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self.lock:
            self.subs.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.subs:
                self.subs.remove(q)

    def put(self, ev: dict):
        with self.lock:
            subs = list(self.subs)
        for q in subs:
            q.put(ev)

    def get(self, timeout=None):  # simple single-consumer use (tests, scripts)
        if self._default is None:
            self._default = self.subscribe()
        return self._default.get(timeout=timeout)


def lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 80))  # no packet is sent; picks the interface used for the LAN
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


# ==================================================================================================== Chat

class Chat:
    REPLAYABLE = {"user", "text", "thinking", "notice", "tool", "tool_result", "todos", "turn_end", "server_tool",
                  "preview", "plan_ready", "hook"}
    BROWSER_ACTS = {"browser_click", "browser_type", "browser_press"}

    def __init__(self, app: "Session", cwd: str, chat_id: str | None = None, worktree: dict | None = None,
                 unattended: bool = False, title: str = ""):
        self.app = app
        self.id = chat_id or time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)
        self.cwd = cwd
        self.worktree = worktree  # {"path", "branch", "origin"} for parallel worktree chats
        self.rules_dir = worktree["origin"] if worktree else cwd
        self.unattended = unattended  # scheduled runs: nobody is there to approve
        self.title = title
        self.transcript: list[dict] = []
        self.checkpoints: dict[str, dict] = {}
        self.turn = 0
        self.turn_changes: dict[str, str | None] = {}
        self.cost = self.turn_cost = 0.0
        self.tokens = 0
        self.todos: list = []
        self._open = None
        self.busy = False
        self.cancel = threading.Event()
        self.pending_info: dict[str, dict] = {}
        self.provider = None
        self.ws: Workspace | None = None
        self.instruction_files: list[str] = []
        self.build()

    # ---------------------------------------------------------------- setup
    def build(self, keep_history: bool = False):
        cfg = {**self.app.config, "cwd": self.cwd}
        old = self.provider.serialize() if (keep_history and self.provider) else None
        old_kind = getattr(self, "_provider_kind", None)
        self.provider = self.app.make_provider(cfg)
        self._provider_kind = cfg["provider"]
        if old and old_kind == cfg["provider"]:
            self.provider.restore(old)
        if self.ws:
            self.ws.stop_all_jobs()
        self.ws = Workspace(self.cwd, on_file_change=self._snapshot)
        self.ws.sandbox = self.app.config.get("sandbox", "off")
        self.instruction_files = project_instructions(self.cwd)[1]

    # ---------------------------------------------------------------- events
    def emit(self, kind: str, **data):
        ev = {"kind": kind, "chat": self.id, **data}
        self.app.events.put(ev)
        if kind in ("text", "thinking"):
            if self._open == kind and self.transcript and self.transcript[-1]["kind"] == kind:
                self.transcript[-1] = {**self.transcript[-1], "text": self.transcript[-1]["text"] + data["text"]}
            else:
                self.transcript.append(dict(ev))
            self._open = kind
            return
        if kind == "end_block":
            self._open = None
        elif kind in self.REPLAYABLE:
            self._open = None
            self.transcript.append(ev)
        elif kind == "approve":
            self._open = None
            self.transcript.append({**ev, "kind": "tool"})

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

    def server_tool(self, summary):
        self.emit("server_tool", text=summary)

    def usage(self, model, u):
        c = cost_of(model, u)
        self.tokens += sum(u.values())
        if c is not None:
            self.cost += c
            self.turn_cost += c
        self.emit("usage", cost=round(self.cost, 4), tokens=self.tokens, priced=c is not None)

    def before_request(self):
        self._check()
        budget = float(self.app.config.get("budget") or 0)
        if budget > 0 and self.cost >= budget:
            raise Stop(f"Budget of ${budget:.2f} for this chat reached. Raise it in Settings to continue.")

    def _check(self):
        if self.cancel.is_set():
            raise Stop("Stopped.")

    # ---------------------------------------------------------------- undo
    def _snapshot(self, path: Path, old: str | None):
        key = str(path)
        if key not in self.turn_changes:
            self.turn_changes[key] = old if (old is None or len(old) < 2_000_000) else "\0too-large"

    def undo(self, turn: str) -> str:
        changes = self.checkpoints.pop(turn, None)
        if not changes:
            raise ValueError("Nothing to undo for that message (already undone or not saved).")
        restored = []
        for path, old in changes.items():
            p = Path(path)
            if old == "\0too-large":
                continue
            if old is None:
                p.unlink(missing_ok=True)
            else:
                p.write_text(old, encoding="utf-8")
            restored.append(p.name)
        self.notice(f"Undid changes to {', '.join(restored)}.")
        self.save()
        return ", ".join(restored)

    # ---------------------------------------------------------------- permissions
    def folder_rules(self) -> dict:
        return self.app.config["rules"].get(self.rules_dir, {"allow": [], "deny": []})

    def _target(self, name: str, args: dict) -> str:
        if name.startswith("browser_"):
            return self.app.browser_url
        if name.startswith("desktop_"):
            return describe(name, args)
        if name.startswith("mcp_"):
            return name[4:]
        if name == "bash":
            return args["command"]
        if "path" in args:
            try:
                return self.ws.path(args["path"]).relative_to(self.ws.cwd).as_posix()
            except ValueError:
                return str(self.ws.path(args["path"]))
        return args.get("job_id", "")

    def _suggest_rule(self, name: str, args: dict) -> str:
        if name == "bash":
            try:
                words = shlex.split(args["command"])[:2]
            except ValueError:
                words = args["command"].split()[:2]
            return f"bash({' '.join(words)}*)" if words else "bash"
        if name.startswith("browser_"):
            host = urlparse(self.app.browser_url).netloc
            return f"browser(*{host}*)" if host else "browser"
        return f"{_rule_kind(name)}(*)"

    def decide(self, name: str, args: dict) -> str:
        """'allow' | 'deny' | 'ask'"""
        rules, target = self.folder_rules(), self._target(name, args)
        if any(rule_matches(r, name, target) for r in rules.get("deny", [])):
            return "deny"
        if any(rule_matches(r, name, target) for r in rules.get("allow", [])):
            return "allow"
        if self.app.config["auto"] and not name.startswith("desktop_"):  # screen control always asks
            return "allow"
        return "ask"

    def gate(self, name: str, args: dict, info: dict) -> tuple[str, bool] | None:
        """Plan mode, rules and approval for an action. Returns a refusal (text, True) or None to go ahead."""
        if self.app.config["plan"]:
            self.emit("tool", **info, blocked="Plan mode")
            return ("Plan mode is on: do not change anything yet. Finish researching, then present your plan "
                    "as your final message for the user to approve."), True
        verdict = self.decide(name, args)
        if verdict == "deny":
            self.emit("tool", **info, blocked="Blocked by a rule")
            return "Blocked by the user's permission rules for this folder. Do not try to work around it.", True
        if verdict == "ask" and self.unattended:
            self.emit("tool", **info, blocked="Needs approval (scheduled run)")
            return ("Nobody is available to approve this during a scheduled run, and no permission rule allows it. "
                    "Skip it and mention it in your summary."), True
        if verdict == "ask":
            rule = self._suggest_rule(name, args)
            answer = self._ask(info, rule)
            if answer.get("decision") == "always":
                self.app.add_rule(self.rules_dir, rule)
            elif answer.get("decision") != "allow":
                fb = answer.get("feedback") or "The user declined."
                self.emit("tool_result", id=info["id"], output=f"Declined. {fb}", error=True)
                return f"The user did not allow this. Their reply: {fb}", True
        else:
            self.emit("tool", **info)
        return None

    def _ask(self, info: dict, rule: str) -> dict:
        done, answer = threading.Event(), {}
        ev = {**info, "rule": rule}
        self.app.pending[info["id"]] = (done, answer)
        self.pending_info[info["id"]] = ev
        self.emit("approve", **ev)
        if self.app.current is not self:
            extras.notify("Incredible Herness", f"“{self.title or 'A chat'}” needs your approval.")
        try:
            while not done.wait(0.25):
                self._check()
        finally:
            self.app.pending.pop(info["id"], None)
            self.pending_info.pop(info["id"], None)
        return answer

    # ---------------------------------------------------------------- tools
    def run_tool(self, name: str, args: dict):
        self._check()
        if name in self.app.mcp.names:
            return self._mcp_tool(name, args)
        problem = validate(name, args)
        if problem:
            self.emit("tool", id=secrets.token_hex(4), summary=f"invalid {name} call", error=True)
            return f"Invalid input: {problem} Fix the call and try again.", True
        if name == "todo_write":
            self.todos = args["todos"]
            self.emit("todos", todos=self.todos)
            return "Task list updated.", False
        if name == "remember":
            out = extras.remember(args["note"])
            self.emit("tool", id=secrets.token_hex(4), name=name, summary=describe(name, args))
            return out, False
        if name == "task":
            return self._subagent(args)
        if name == "open_preview":
            return self._preview(args["target"])
        if name.startswith("browser_"):
            return self._browser_tool(name, args)
        if name.startswith("desktop_"):
            return self._desktop_tool(name, args)

        call_id = secrets.token_hex(6)
        info = {"id": call_id, "name": name, "summary": describe(name, args)}
        diff = self.ws.preview_diff(name, args)
        if diff:
            info["diff"] = diff
        if name in MUTATING:
            refusal = self.gate(name, args, info)
            if refusal:
                return refusal
        else:
            self.emit("tool", **info)
        self.ws.sandbox = self.app.config.get("sandbox", "off")
        try:
            out, err = self.ws.run(name, args), False
        except ToolError as e:
            out, err = str(e), True
        except Exception as e:
            out, err = f"{type(e).__name__}: {e}", True
        if not err and name in FILE_CHANGING:
            out += self._after_edit(self.ws.path(args["path"]))
        shown = out if isinstance(out, str) else "\n".join(x.get("text", "") for x in out)
        self.emit("tool_result", id=call_id, output=shown[:8000], error=err)
        return out, err

    def _after_edit(self, path: Path) -> str:
        notes = []
        if self.app.config.get("diagnostics", True):
            problems = extras.diagnose(path, self.cwd)
            if problems:
                notes.append(f"Problems found in {path.name} after this edit (fix them):\n{problems}")
        hook = self.app.hooks_for(self.rules_dir).get("after_edit", "").strip()
        if hook:
            code, out = extras.run_hook(hook, self.cwd, {"HERNESS_FILE": str(path)})
            self.emit("hook", name="after edit", command=hook, ok=code == 0, output=out[-1500:])
            if code != 0:
                notes.append(f"The after-edit hook `{hook}` failed:\n{out[-3000:]}")
        return ("\n\n" + "\n\n".join(notes)) if notes else ""

    def _subagent(self, args: dict):
        call_id = secrets.token_hex(6)
        self.emit("tool", id=call_id, name="task", summary=f"Sub-agent: {args['description']}")
        parent = self

        class SubUI:
            steps = 0
            def text(self, c): parent._check()
            def thinking(self, c): parent._check()
            def end_block(self): pass
            def notice(self, m): pass
            def server_tool(self, s): SubUI.steps += 1
            def usage(self, m, u): parent.usage(m, u)
            def before_request(self): parent.before_request()

        allowed = {t["name"] for t in READONLY_TOOLS}

        def sub_run(name, a):
            parent._check()
            SubUI.steps += 1
            problem = validate(name, a)
            if problem or name not in allowed:
                return f"Not available: {problem or name}", True
            try:
                return parent.ws.run(name, a), False
            except Exception as e:
                return str(e), True

        try:
            sub = self.app.make_provider({**self.app.config, "cwd": self.cwd}, tools=READONLY_TOOLS, system=SUBAGENT_SYSTEM,
                                         fast=True)
            sub.turn(args["prompt"], sub_run, SubUI())
            report = sub.last_text() or "(the sub-agent returned no report)"
            self.emit("tool_result", id=call_id, output=f"{SubUI.steps} steps\n\n{report}", error=False)
            return report, False
        except Stop:
            raise
        except Exception as e:
            self.emit("tool_result", id=call_id, output=str(e), error=True)
            return f"Sub-agent failed: {e}", True

    def _preview(self, target: str):
        if not re.match(r"^https?://", target):
            p = self.ws.path(target)
            if not p.exists():
                return f"No such file: {p}", True
            target = p.as_uri()
        try:
            self.app.preview_opener(target)
        except Exception as e:
            return f"Could not open preview: {e}", True
        self.emit("preview", target=target)
        return f"Opened a preview of {target} for the user.", False

    def _browser_tool(self, name: str, args: dict):
        call_id = secrets.token_hex(6)
        info = {"id": call_id, "name": name, "summary": describe(name, args)}
        self.emit("browser_open")
        if name in self.BROWSER_ACTS:
            refusal = self.gate(name, args, info)
            if refusal:
                return refusal
        else:
            self.emit("tool", **info)
        b = self.app.browser
        try:
            if name == "browser_navigate":
                out = b.navigate(args["url"])
            elif name == "browser_read":
                out = b.read()
            elif name == "browser_screenshot":
                img = b.screenshot()
                self.emit("tool_result", id=call_id, output=f"Screenshot of {self.app.browser_url}", error=False, image=img)
                return [{"type": "text", "text": f"Screenshot of {self.app.browser_url} (1280x800):"},
                        {"type": "image", "media_type": "image/jpeg", "data": img}], False
            elif name == "browser_click":
                out = b.click(args.get("x"), args.get("y"), args.get("text"), args.get("selector"))
            elif name == "browser_type":
                out = b.type(args["text"], args.get("selector"), bool(args.get("submit")))
            elif name == "browser_press":
                out = b.press(args["key"])
            else:
                out = b.scroll(args["dy"])
            err = False
        except NeedsBrowser as e:
            self.app.events.put({"kind": "browser_status", "text": str(e), "install": True})
            out, err = f"{e} Ask the user to click 'Install browser engine' in the Browser panel.", True
        except Exception as e:
            out, err = f"{type(e).__name__}: {e}"[:2000], True
        self.emit("tool_result", id=call_id, output=out[:8000], error=err)
        return out, err

    def _mcp_tool(self, name: str, args: dict):
        server, tool = self.app.mcp.names[name]
        call_id = secrets.token_hex(6)
        info = {"id": call_id, "name": name, "summary": f"{server} · {tool} {json.dumps(args)[:120]}"}
        if not isinstance(args, dict):
            return "Tool input must be a JSON object.", True
        refusal = self.gate(name, args, info)
        if refusal:
            return refusal
        try:
            out, err = self.app.mcp.call(name, args)
        except McpError as e:
            out, err = [{"type": "text", "text": str(e)}], True
        shown = "\n".join(p.get("text", "[image]") for p in out)
        self.emit("tool_result", id=call_id, output=shown[:8000], error=err)
        return out, err

    def _desktop_tool(self, name: str, args: dict):
        call_id = secrets.token_hex(6)
        info = {"id": call_id, "name": name, "summary": describe(name, args)}
        if not self.app.config.get("computer"):
            return "Computer use is turned off. The user can enable it in Settings > Tools & limits.", True
        if name != "desktop_screenshot":
            refusal = self.gate(name, args, info)
            if refusal:
                return refusal
        else:
            self.emit("tool", **info)
        c = self.app.computer
        try:
            if name == "desktop_screenshot":
                img, w, h = c.screenshot()
                self.emit("tool_result", id=call_id, output=f"Screenshot {w}x{h}", error=False, image=img)
                return [{"type": "text", "text": f"Screenshot of the screen ({w}x{h}; use these coordinates):"},
                        {"type": "image", "media_type": "image/jpeg", "data": img}], False
            if name == "desktop_click":
                out = c.click(args["x"], args["y"], args.get("button", "left"), bool(args.get("double")))
            elif name == "desktop_type":
                out = c.type(args["text"])
            elif name == "desktop_key":
                out = c.key(args["keys"])
            else:
                out = c.scroll(args["dy"], args.get("x"), args.get("y"))
            err = False
        except ComputerError as e:
            out, err = str(e), True
        except Exception as e:
            out, err = (f"{type(e).__name__}: {e}. On macOS, allow Incredible Herness under System Settings > Privacy & "
                        "Security > Screen Recording and Accessibility.")[:1500], True
        self.emit("tool_result", id=call_id, output=out, error=err)
        return out, err

    # ---------------------------------------------------------------- messages
    def _parts(self, text: str, attachments: list[dict]) -> list[dict]:
        parts = []
        for a in attachments or []:
            name, mime, data = a.get("name", "file"), a.get("mime", ""), a.get("data", "")
            raw = base64.b64decode(data)
            if len(raw) > 20_000_000:
                raise ValueError(f"{name} is larger than 20 MB.")
            if mime in IMAGE_TYPES:
                parts.append({"type": "image", "media_type": mime, "data": data})
            elif mime == "application/pdf" or name.lower().endswith(".pdf"):
                parts.append({"type": "pdf", "name": name, "data": data})
            else:
                try:
                    parts.append({"type": "file", "name": name, "text": raw.decode("utf-8")[:200_000]})
                except UnicodeDecodeError:
                    raise ValueError(f"{name}: only images, PDFs and text files can be attached.")
        if self.app.config["plan"]:
            text = ("[Plan mode is ON: research and read as needed, but do not change anything. End with a clear, "
                    "numbered plan for the user to approve.]\n\n" + text)
        parts.append({"type": "text", "text": text})
        return parts

    def send(self, text: str, attachments: list[dict] | None = None) -> None:
        if self.busy:
            raise RuntimeError("This chat is still working. Press Stop first, or start another chat.")
        expanded = extras.expand_command(text, self.cwd) if text.startswith("/") else None
        parts = self._parts(expanded or text, attachments or [])
        self.busy = True
        self.cancel.clear()
        if not self.title:
            self.title = text.strip().splitlines()[0][:70] if text.strip() else "Attachment"
        self.emit("user", text=text, files=[a.get("name") for a in attachments or []], plan=self.app.config["plan"],
                  command=bool(expanded))
        threading.Thread(target=self._turn, args=(parts,), daemon=True).start()

    def _turn(self, parts) -> None:
        self.turn += 1
        turn_id = f"t{self.turn}"
        self.turn_changes, self.turn_cost = {}, 0.0
        self.emit("status", busy=True)
        try:
            self.provider.turn(parts, self.run_tool, self)
            self._finish_hook()
        except Stop as e:
            self.notice(str(e))
            self.provider.close_interrupted()
        except Exception as e:
            px.log.exception("Turn failed in chat %s", self.id)
            self.notice(explain_error(e, {**self.app.config, "cwd": self.cwd}))
            self.provider.close_interrupted()
        finally:
            self.end_block()
            if self.turn_changes:
                self.checkpoints[turn_id] = self.turn_changes
            self.emit("turn_end", turn=turn_id, files=[Path(p).name for p in self.turn_changes],
                      cost=round(self.turn_cost, 4))
            if self.app.config["plan"]:
                self.emit("plan_ready")
            self.busy = False
            self.emit("status", busy=False)
            self.save()
            if self.app.current is not self or self.unattended:
                extras.notify("Incredible Herness", f"“{self.title}” finished.")

    def _finish_hook(self):
        hook = self.app.hooks_for(self.rules_dir).get("before_finish", "").strip()
        if not hook or self.app.config["plan"] or not self.turn_changes:
            return
        for attempt in range(3):
            self._check()
            code, out = extras.run_hook(hook, self.cwd)
            self.emit("hook", name="before finish", command=hook, ok=code == 0, output=out[-1500:])
            if code == 0 or attempt == 2:
                return
            self.provider.turn(f"The project check `{hook}` failed after your changes:\n\n{out[-4000:]}\n\n"
                               "Fix the problems, then finish.", self.run_tool, self)

    # ---------------------------------------------------------------- persistence
    def save(self):
        if not self.transcript:
            return
        try:
            CHATS.mkdir(parents=True, exist_ok=True)
            data = {"id": self.id, "title": self.title or "Untitled", "updated": time.time(),
                    "provider": self._provider_kind, "model": self.app.config["model"], "cwd": self.cwd,
                    "worktree": self.worktree, "messages": self.provider.serialize(), "transcript": self.transcript,
                    "checkpoints": self.checkpoints, "cost": self.cost, "tokens": self.tokens, "todos": self.todos,
                    "turn": self.turn}
            path = CHATS / f"{self.id}.json"
            path.write_text(json.dumps(data, default=str))
            os.chmod(path, 0o600)
        except Exception as e:
            self.notice(f"Could not save chat: {e}")

    def replay_event(self) -> dict:
        return {"kind": "replay", "chat": {"id": self.id, "title": self.title, "worktree": self.worktree,
                                           "cwd": self.cwd},
                "events": self.transcript, "cost": round(self.cost, 4), "tokens": self.tokens, "todos": self.todos,
                "undoable": list(self.checkpoints), "busy": self.busy, "pending": list(self.pending_info.values())}


# ==================================================================================================== Session

class Session:
    def __init__(self):
        self.events = EventHub()
        self.config = {"provider": "claude", "model": DEFAULT_MODELS["claude"], "effort": "high", "base_url": "",
                       "api_key": "", "cwd": str(Path.home()), "web": True, "compaction": True, "budget": 0.0,
                       "mcp": [], "rules": {}, "hooks": {}, "github_token": "", "diagnostics": True,
                       "computer": False, "schedules": [], "phone": False, "voice_key": "", "auto": False, "plan": False,
                       "fast_model": "", "mcp_local": [], "sandbox": "off", "language": "", "chat_order": [],
                       "recent_folders": [], "ui_language": "en"}
        self.mcp = McpManager()
        self.quit_app = None  # set by the desktop app: closes the window
        self.new_window = None  # set by the desktop app: opens another window
        self.pending: dict[str, tuple[threading.Event, dict]] = {}
        self.preview_opener = webbrowser.open
        self.browser = Browser(HOME / "browser", self._browser_frame, self._browser_state)
        self.browser_url = ""
        self.computer = Computer()
        self.terminals: dict[str, Terminal] = {}
        self.chats: dict[str, Chat] = {}
        self.current: Chat | None = None
        self.phone_server = None
        self.phone_url = ""
        self._handler_factory = None
        self._sched_stop = threading.Event()

    # ---------------------------------------------------------------- compatibility helpers (current chat)
    @property
    def provider(self):
        return self.current.provider if self.current else None

    @property
    def ws(self):
        return self.current.ws if self.current else None

    @property
    def transcript(self):
        return self.current.transcript if self.current else []

    @property
    def chat_id(self):
        return self.current.id if self.current else ""

    @property
    def instruction_files(self):
        return self.current.instruction_files if self.current else []

    @property
    def busy(self):
        return bool(self.current and self.current.busy)

    def send(self, text, attachments=None):
        if not self.current:
            raise RuntimeError("Finish setup in Settings first.")
        self.current.send(text, attachments)

    def undo(self, turn):
        return self.current.undo(turn)

    # ---------------------------------------------------------------- settings
    def load(self):
        try:
            saved = json.loads(CONFIG_FILE.read_text())
            self.config.update({k: v for k, v in saved.items() if k in self.config and k not in ("auto", "plan")})
        except (OSError, ValueError):
            pass

    def save(self):
        try:
            HOME.mkdir(parents=True, exist_ok=True)
            CONFIG_FILE.write_text(json.dumps({k: v for k, v in self.config.items() if k not in ("auto", "plan")}, indent=2))
            os.chmod(CONFIG_FILE, 0o600)  # holds API keys: readable only by you
        except OSError:
            pass

    def tools_for(self, cfg: dict) -> list[dict]:
        self.mcp.configure(cfg.get("mcp_local") or [])
        return [t for t in TOOLS if t["kind"] != "desktop" or cfg.get("computer")] + self.mcp.tool_specs()

    def fast_model(self, cfg: dict) -> str:
        """Cheaper model for sub-agents and simple steps (model routing)."""
        if cfg.get("fast_model"):
            return cfg["fast_model"]
        return "claude-haiku-5-5" if cfg["provider"] == "claude" else cfg["model"]

    def make_provider(self, cfg: dict, tools=None, system: str | None = None, fast: bool = False):
        tools = tools if tools is not None else self.tools_for(cfg)
        if fast:
            cfg = {**cfg, "model": self.fast_model(cfg)}
        if system is None:
            text, _ = project_instructions(cfg["cwd"])
            system = system_prompt(cfg["cwd"], text, extras.read_memory(), bool(cfg.get("computer")), cfg.get("language", ""))
        common = dict(tools=tools, compaction=cfg["compaction"])
        if cfg["provider"] == "claude":
            return ClaudeProvider(cfg["model"], system, effort=cfg["effort"], api_key=cfg["api_key"] or None,
                                  web=cfg["web"], mcp_servers=cfg["mcp"], **common)
        if cfg["provider"] == "openai":
            return OpenAIProvider(cfg["model"], system, base_url=cfg["base_url"] or os.environ.get("OPENAI_BASE_URL"),
                                  api_key=cfg["api_key"] or None, label_prefix="openai", **common)
        return OpenAIProvider(cfg["model"], system, base_url=cfg["base_url"] or LOCAL_URL,
                              api_key=cfg["api_key"] or "local", label_prefix="local", **common)

    def apply_config(self, new: dict, keep_history: bool = False) -> None:
        cfg = {**self.config, **{k: v for k, v in new.items() if k in self.config}}
        if cfg["provider"] not in DEFAULT_MODELS:
            raise ValueError(f"Unknown provider {cfg['provider']}")
        if not cfg["model"] or (new.get("provider") not in (None, self.config["provider"]) and not new.get("model")):
            cfg["model"] = DEFAULT_MODELS[cfg["provider"]]
        cwd = Path(os.path.expanduser(cfg["cwd"] or str(Path.home())))
        if not cwd.is_dir():
            raise ValueError(f"Folder not found: {cwd}")
        cfg["cwd"] = str(cwd.resolve())
        cfg["budget"] = max(0.0, float(cfg.get("budget") or 0))
        cfg["base_url"] = fix_base_url(cfg.get("base_url", ""))
        cfg["recent_folders"] = ([cfg["cwd"]] + [f for f in cfg.get("recent_folders", []) if f != cfg["cwd"] and Path(f).is_dir()])[:10]
        self.make_provider(cfg)  # validate (e.g. API key present) before changing anything
        old = self.config
        self.config = cfg
        if self.current is None or (self.current.busy is False and self.current.transcript == [] and not keep_history):
            self.current = self._new_chat_obj(cfg["cwd"])
        elif not self.current.busy:
            if cfg["cwd"] != old["cwd"] and not self.current.worktree:
                self.current.cwd = self.current.rules_dir = cfg["cwd"]
            self.current.build(keep_history=keep_history)
        self.save()
        if bool(cfg.get("phone")) != bool(self.phone_server):
            self.set_phone(bool(cfg.get("phone")))

    def _new_chat_obj(self, cwd: str, **kw) -> Chat:
        chat = Chat(self, cwd, **kw)
        self.chats[chat.id] = chat
        return chat

    def public_config(self) -> dict:
        hidden = ("api_key", "github_token", "rules", "hooks", "voice_key", "mcp_local")
        c = {k: v for k, v in self.config.items() if k not in hidden}
        c["api_key"] = bool(self.config["api_key"])
        c["github_token"] = bool(self.config["github_token"])
        c["voice_key"] = bool(self.config["voice_key"])
        chat = self.current
        c["rules"] = chat.folder_rules() if chat else {"allow": [], "deny": []}
        c["hooks"] = self.hooks_for(chat.rules_dir) if chat else {}
        c["instructions"] = chat.instruction_files if chat else []
        c["chat_cwd"] = chat.cwd if chat else c["cwd"]
        c["build"], c["version"], c["phone_url"] = BUILD, __version__, self.phone_url
        c["commands"] = self.command_list()
        c["mcp_local"] = [{**m, "env": {k: "" for k in (m.get("env") or {})}} for m in self.config["mcp_local"]]
        c["mcp_status"] = self.mcp.status()
        c["fast_model_default"] = self.fast_model(self.config)
        c["sandbox_available"] = px.sandbox_available()
        return c

    def add_rule(self, folder: str, rule: str, kind: str = "allow"):
        rules = self.config["rules"].setdefault(folder, {"allow": [], "deny": []})
        if rule not in rules[kind]:
            rules[kind].append(rule)
        self.save()
        self.events.put({"kind": "config", "config": self.public_config()})

    def hooks_for(self, folder: str) -> dict:
        return self.config["hooks"].get(folder, {})

    def command_list(self) -> list[dict]:
        cwd = self.current.cwd if self.current else self.config["cwd"]
        out = [{"name": n, "description": d, "kind": "ui"} for n, d in extras.UI_COMMANDS.items()]
        out += [{"name": n, "description": d, "kind": "prompt"} for n, (d, _) in extras.commands(cwd).items()]
        return out

    # ---------------------------------------------------------------- chats
    def select(self, chat: Chat, client: str = ""):
        self.current = chat
        self.events.put({**chat.replay_event(), "client": client})
        self.events.put({"kind": "config", "config": self.public_config()})

    def new_chat(self, worktree: bool = False, client: str = ""):
        cwd = self.current.rules_dir if self.current else self.config["cwd"]
        wt = None
        if worktree:
            name = time.strftime("%m%d-%H%M%S")
            path, branch = gitops.add_worktree(cwd, name)
            wt = {"path": path, "branch": branch, "origin": cwd}
        chat = self._new_chat_obj(wt["path"] if wt else cwd, worktree=wt)
        if wt:
            chat.notice(f"Parallel chat on branch {wt['branch']} in its own folder ({wt['path']}). "
                        "Your main folder is untouched until you merge.")
        self.select(chat, client)

    def open_chat(self, chat_id: str, client: str = ""):
        live = self.chats.get(chat_id)
        if live:
            return self.select(live, client)
        d = json.loads((CHATS / f"{Path(chat_id).name}.json").read_text())
        cwd = d.get("cwd") if Path(d.get("cwd", "")).is_dir() else self.config["cwd"]
        if d.get("provider") != self.config["provider"] or d.get("model") != self.config["model"]:
            self.config.update(provider=d["provider"], model=d["model"])
        wt = d.get("worktree") if d.get("worktree") and Path(d["worktree"]["path"]).is_dir() else None
        chat = Chat(self, cwd, chat_id=d["id"], worktree=wt, title=d.get("title", ""))
        chat.provider.restore(d["messages"])
        chat.transcript, chat.checkpoints = d.get("transcript", []), d.get("checkpoints", {})
        chat.cost, chat.tokens, chat.todos, chat.turn = d.get("cost", 0), d.get("tokens", 0), d.get("todos", []), d.get("turn", 0)
        self.chats[chat.id] = chat
        self.select(chat, client)

    def delete_chat(self, chat_id: str):
        live = self.chats.get(chat_id)
        if live and live.busy:
            raise RuntimeError("Stop that chat before deleting it.")
        self.chats.pop(chat_id, None)
        (CHATS / f"{Path(chat_id).name}.json").unlink(missing_ok=True)
        if self.current and self.current.id == chat_id:
            self.current = None
            self.new_chat()

    def list_chats(self) -> list[dict]:
        seen, out = set(), []
        for c in self.chats.values():
            if c.transcript:
                out.append({"id": c.id, "title": c.title or "Untitled", "updated": time.time(), "running": c.busy,
                            "approval": bool(c.pending_info), "worktree": bool(c.worktree)})
                seen.add(c.id)
        for p in sorted(CHATS.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:200]:
            if p.stem in seen:
                continue
            try:
                d = json.loads(p.read_text())
                out.append({"id": d["id"], "title": d.get("title", "Untitled"), "updated": d.get("updated", 0),
                            "running": False, "approval": False, "worktree": bool(d.get("worktree"))})
            except (OSError, ValueError, KeyError):
                continue
        order = {cid: i for i, cid in enumerate(self.config.get("chat_order") or [])}
        pinned = sorted([c for c in out if c["id"] in order], key=lambda c: order[c["id"]])
        live = [c for c in out if c["id"] in self.chats and c["id"] not in order]
        rest = [c for c in out if c["id"] not in self.chats and c["id"] not in order]
        return pinned + live + rest

    def finish_worktree(self, merge: bool) -> str:
        chat = self.current
        if not chat or not chat.worktree:
            raise ValueError("This chat is not running in a worktree.")
        if chat.busy:
            raise RuntimeError("Stop the chat first.")
        chat.ws.stop_all_jobs()
        msg = gitops.finish_worktree(chat.worktree["path"], merge)
        origin = chat.worktree["origin"]
        chat.worktree, chat.cwd = None, origin
        chat.build(keep_history=True)
        chat.notice(msg)
        chat.save()
        self.select(chat)
        return msg

    # ---------------------------------------------------------------- browser, terminals
    def _browser_frame(self, data, w, h):
        self.events.put({"kind": "browser_frame", "data": data, "w": w, "h": h})

    def _browser_state(self, st):
        self.browser_url = st.get("url", "")
        self.events.put({"kind": "browser_state", **st})

    def install_browser(self):
        def run():
            try:
                self.events.put({"kind": "browser_status", "text": "Downloading the browser engine (about 100 MB)…"})
                install_chromium(lambda line: self.events.put({"kind": "browser_status", "text": line}))
                self.events.put({"kind": "browser_status", "text": "Browser engine installed.", "done": True})
            except Exception as e:
                self.events.put({"kind": "browser_status", "text": f"Install failed: {e}", "install": True})
        threading.Thread(target=run, daemon=True).start()

    def browser_input(self, d: dict):
        b, kind = self.browser, d.get("type")
        if kind == "navigate":
            b.navigate(str(d.get("url", "")))
        elif kind in ("back", "forward", "reload"):
            b.history(kind)
        elif kind == "click":
            b.click(int(d["x"]), int(d["y"]))
        elif kind == "text":
            b.type(str(d.get("text", "")))
        elif kind == "key":
            b.press(str(d.get("key", "")))
        elif kind == "scroll":
            b.scroll(int(d.get("dy", 0)))

    def open_terminal(self, cols: int, rows: int) -> str:
        cwd = self.current.cwd if self.current else self.config["cwd"]
        t = Terminal(cwd, lambda tid, data: self.events.put({"kind": "term", "id": tid, "data": data}),
                     lambda tid: self.events.put({"kind": "term_exit", "id": tid}), cols, rows)
        self.terminals[t.id] = t
        return t.id

    # ---------------------------------------------------------------- git
    def git_action(self, d: dict) -> dict:
        cwd = self.current.cwd if self.current else self.config["cwd"]
        a = d.get("action")
        if a == "status":
            return gitops.status(cwd)
        if a == "diff":
            return {"diff": gitops.diff(cwd, d.get("path"))}
        if a == "commit":
            return {"message": gitops.commit(cwd, str(d.get("message", "")), d.get("files"))}
        if a == "checkout":
            return {"message": gitops.checkout(cwd, str(d.get("branch")), bool(d.get("create")))}
        if a == "push":
            return {"message": gitops.push(cwd)}
        if a == "pull":
            return {"message": gitops.pull(cwd)}
        if a == "pr":
            return {"message": gitops.create_pr(cwd)}
        if a == "init":
            return {"message": gitops.git(cwd, "init").strip()}
        raise ValueError(f"Unknown git action {a}")

    # ---------------------------------------------------------------- export, voice, updates
    def export(self, fmt: str) -> str:
        chat = self.current
        if not chat or not chat.transcript:
            raise ValueError("Nothing to export yet.")
        md = extras.export_markdown(chat.title or "Chat", chat.transcript)
        safe = re.sub(r"[^\w\- ]+", "", chat.title or "chat").strip()[:60] or "chat"
        path = extras.downloads_dir() / f"{safe}.{'html' if fmt == 'html' else 'md'}"
        path.write_text(extras.export_html(chat.title, md) if fmt == "html" else md, encoding="utf-8")
        return str(path)

    def transcribe(self, data_b64: str, mime: str) -> str:
        key = self.config["voice_key"] or os.environ.get("OPENAI_API_KEY") or (
            self.config["api_key"] if self.config["provider"] == "openai" else "")
        if not key:
            raise ValueError("Voice typing needs an OpenAI API key for transcription (Settings > Tools & limits).")
        import openai

        ext = "webm" if "webm" in mime else "mp4" if "mp4" in mime else "wav"
        r = openai.OpenAI(api_key=key).audio.transcriptions.create(
            model="whisper-1", file=(f"speech.{ext}", base64.b64decode(data_b64), mime or "audio/webm"))
        return r.text

    def check_update(self) -> dict:
        req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/releases?per_page=5",
                                     headers={"Accept": "application/vnd.github+json", "User-Agent": "IncredibleHerness"})
        if self.config["github_token"]:
            req.add_header("Authorization", f"Bearer {self.config['github_token']}")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                releases = json.loads(r.read())
        except Exception as e:
            return {"error": f"Could not check for updates ({e})."}
        best = None
        for rel in releases:
            m = re.match(r"build-(\d+)$", rel.get("tag_name", ""))
            if m and (best is None or int(m.group(1)) > best[0]):
                best = (int(m.group(1)), rel.get("html_url"))
        if not best:
            return {"latest": None, "current": BUILD, "update": False}
        return {"latest": best[0], "current": BUILD, "update": best[0] > BUILD, "url": best[1]}

    def _client(self, cfg: dict):
        if cfg["provider"] == "claude":
            import anthropic
            return anthropic.Anthropic(api_key=cfg["api_key"]) if cfg["api_key"] else anthropic.Anthropic()
        import openai
        base = cfg["base_url"] or (LOCAL_URL if cfg["provider"] == "local" else None)
        return openai.OpenAI(base_url=base, api_key=cfg["api_key"] or ("local" if cfg["provider"] == "local" else None))

    def list_models(self, cfg: dict) -> list[str]:
        cfg = {**cfg, "base_url": fix_base_url(cfg.get("base_url", ""))}
        c = self._client(cfg)
        try:
            ids = [m.id for m in c.models.list()]
        except (AttributeError, TypeError, ValueError):
            raise ValueError(f"{cfg['base_url'] or 'That address'} answered, but not like an AI API (it looks like a website). "
                             "Use the service's API address — pick a preset under Service.")
        skip = ("embed", "image", "tts", "whisper", "moderation", "dall-e", "transcribe")
        return sorted(i for i in ids if not any(x in i for x in skip))

    def test_connection(self, cfg: dict) -> str:
        cfg = {**cfg, "base_url": fix_base_url(cfg.get("base_url", ""))}
        c = self._client(cfg)
        if cfg["provider"] == "claude":
            r = c.messages.create(model=cfg["model"], max_tokens=16, messages=[{"role": "user", "content": "Reply with: ok"}])
            return "".join(getattr(b, "text", "") for b in r.content).strip() or "ok"
        r = c.chat.completions.create(model=cfg["model"], max_tokens=16, messages=[{"role": "user", "content": "Reply with: ok"}])
        return (r.choices[0].message.content or "ok").strip()

    def install_update(self):
        def run():
            try:
                url = f"https://api.github.com/repos/{REPO}/releases/latest"
                info = self.check_update()
                if info.get("url"):
                    url = f"https://api.github.com/repos/{REPO}/releases/tags/build-{info['latest']}"
                path = px.download_update(url, self.config["github_token"],
                                          lambda pct: self.events.put({"kind": "update_progress", "pct": pct}))
                px.launch_updater(path)
                self.events.put({"kind": "update_progress", "pct": 100, "text": "Installing… the app will restart."})
                time.sleep(1.5)
                self.shutdown()
                if self.quit_app:
                    self.quit_app()
                os._exit(0)
            except Exception as e:
                px.log.exception("Update failed")
                self.events.put({"kind": "update_progress", "error": str(e)})
        threading.Thread(target=run, daemon=True).start()

    def share(self, chat: "Chat") -> str:
        if not chat or not chat.transcript:
            raise ValueError("Nothing to share yet.")
        md = extras.export_markdown(chat.title or "Chat", chat.transcript)
        return px.publish_gist(self.config["github_token"], re.sub(r"[^\w\-]+", "-", chat.title or "chat")[:60] + ".md",
                               md, f"Incredible Herness: {chat.title}")

    # ---------------------------------------------------------------- scheduled tasks
    def start_scheduler(self):
        threading.Thread(target=self._scheduler, daemon=True).start()

    def due(self, task: dict, now: float) -> bool:
        if not task.get("enabled", True) or not task.get("prompt"):
            return False
        last = float(task.get("last") or 0)
        every = int(task.get("every") or 0)
        if every > 0:
            return now - last >= every * 60
        at = task.get("at") or ""
        if re.match(r"^\d{1,2}:\d{2}$", at):
            h, m = map(int, at.split(":"))
            lt = time.localtime(now)
            today_at = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, m, 0, 0, 0, -1))
            return now >= today_at and last < today_at
        return False

    def run_task(self, task: dict):
        cwd = task.get("cwd") if Path(task.get("cwd", "") or "/nonexistent").is_dir() else self.config["cwd"]
        chat = self._new_chat_obj(cwd, unattended=True, title=f"⏰ {task.get('name') or 'Scheduled task'}")
        task["last"] = time.time()
        self.save()
        chat.send(task["prompt"])
        self.events.put({"kind": "chats_changed"})
        return chat

    def _scheduler(self):
        while not self._sched_stop.wait(30):
            now = time.time()
            for task in list(self.config.get("schedules", [])):
                try:
                    if self.due(task, now):
                        self.run_task(task)
                except Exception as e:
                    self.events.put({"kind": "toast", "text": f"Scheduled task failed to start: {e}"})

    # ---------------------------------------------------------------- phone access
    def set_phone(self, on: bool):
        if on and not self.phone_server and self._handler_factory:
            token = secrets.token_urlsafe(24)
            srv = ThreadingHTTPServer(("0.0.0.0", 8790), self._handler_factory(self, token))
            srv.daemon_threads = True
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            self.phone_server, self.phone_url = srv, f"http://{lan_ip()}:{srv.server_port}/?token={token}"
        elif not on and self.phone_server:
            self.phone_server.shutdown()
            self.phone_server, self.phone_url = None, ""

    def shutdown(self):
        self._sched_stop.set()
        self.mcp.stop()
        for t in self.terminals.values():
            t.close()
        self.browser.close()
        for c in self.chats.values():
            if c.ws:
                c.ws.stop_all_jobs()
        if self.phone_server:
            self.phone_server.shutdown()


# ==================================================================================================== HTTP

def make_handler(session: Session, token: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _authed(self) -> bool:
            q = parse_qs(urlparse(self.path).query)
            if secrets.compare_digest(self.headers.get("X-Token", "") or q.get("token", [""])[0], token):
                return True
            self.send_error(403)
            return False

        def _send(self, body: bytes, ctype: str, code=200):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(json.dumps(obj, default=str).encode(), "application/json", code)

        def do_GET(self):
            path = urlparse(self.path).path
            if path.startswith("/vendor/"):
                f = Path(__file__).parent / "static" / "vendor" / Path(path).name
                if f.is_file():
                    return self._send(f.read_bytes(), "text/css" if f.suffix == ".css" else "application/javascript")
                return self.send_error(404)
            if not self._authed():
                return
            if path == "/":
                self._send(PAGE.replace("__TOKEN__", token).encode(), "text/html; charset=utf-8")
            elif path == "/api/events":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                sub = session.events.subscribe()
                try:
                    first = [{"kind": "config", "config": session.public_config()}]
                    if session.current:
                        first.append(session.current.replay_event())
                    for ev in first:
                        self.wfile.write(f"data: {json.dumps(ev, default=str)}\n\n".encode())
                    self.wfile.flush()
                    while True:
                        try:
                            ev = sub.get(timeout=15)
                            self.wfile.write(f"data: {json.dumps(ev, default=str)}\n\n".encode())
                        except queue.Empty:
                            self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
                finally:
                    session.events.unsubscribe(sub)
            else:
                self.send_error(404)

        def do_POST(self):
            if not self._authed():
                return
            try:
                data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                path = urlparse(self.path).path
                result: dict = {"ok": True}
                client = str(data.get("client", ""))
                chat = session.chats.get(data.get("chat")) or session.current
                if path == "/api/send":
                    if not chat:
                        raise RuntimeError("Finish setup in Settings first.")
                    chat.send(str(data.get("text", "")), data.get("attachments") or [])
                elif path == "/api/approve":
                    item = session.pending.get(data.get("id"))
                    if item:
                        item[1].update(decision=data.get("decision"), feedback=data.get("feedback", ""))
                        item[0].set()
                elif path == "/api/stop":
                    target = session.chats.get(data.get("chat")) or chat
                    if target:
                        target.cancel.set()
                elif path in ("/api/clear", "/api/chat/new"):
                    session.new_chat(worktree=bool(data.get("worktree")), client=client)
                elif path == "/api/chats":
                    result = {"chats": session.list_chats(), "current": session.chat_id}
                elif path == "/api/chat/open":
                    session.open_chat(str(data.get("id")), client)
                elif path == "/api/chats/order":
                    session.config["chat_order"] = [str(x) for x in data.get("order", [])][:500]
                    session.save()
                elif path == "/api/chat/delete":
                    session.delete_chat(str(data.get("id")))
                elif path == "/api/worktree/finish":
                    session.current = chat
                    result = {"message": session.finish_worktree(bool(data.get("merge")))}
                elif path == "/api/undo":
                    result = {"restored": chat.undo(str(data.get("turn")))}
                elif path == "/api/config":
                    if set(data) <= {"auto", "plan"}:
                        session.config.update({k: bool(v) for k, v in data.items()})
                    else:
                        if chat and chat.busy:
                            raise RuntimeError("Stop the current task before changing settings.")
                        folder = chat.rules_dir if chat else session.config["cwd"]
                        if "rules" in data:
                            r = data.pop("rules")
                            session.config["rules"][folder] = {
                                "allow": [x.strip() for x in r.get("allow", []) if x.strip()],
                                "deny": [x.strip() for x in r.get("deny", []) if x.strip()]}
                        if "hooks" in data:
                            h = data.pop("hooks")
                            session.config["hooks"][folder] = {k: str(h.get(k, "")).strip() for k in ("after_edit", "before_finish")}
                        if "mcp_local" in data:  # blank env values mean "keep the saved secret"
                            old = {m["name"]: m for m in session.config["mcp_local"]}
                            for m in data["mcp_local"]:
                                prev = (old.get(m.get("name")) or {}).get("env") or {}
                                m["env"] = {k: (v or prev.get(k, "")) for k, v in (m.get("env") or {}).items()}
                        session.apply_config(data, keep_history=True)
                    session.events.put({"kind": "config", "config": session.public_config()})
                elif path == "/api/instructions":
                    cwd = chat.cwd if chat else session.config["cwd"]
                    names = project_instructions(cwd)[1]
                    f = Path(cwd) / (names[0] if names else "HERNESS.md")
                    if "text" in data:
                        f.parent.mkdir(parents=True, exist_ok=True)
                        f.write_text(data["text"], encoding="utf-8")
                        if chat and not chat.busy:
                            chat.build(keep_history=True)
                        session.events.put({"kind": "config", "config": session.public_config()})
                    result = {"file": f.name, "text": f.read_text(errors="replace") if f.is_file() else ""}
                elif path == "/api/memory":
                    if "text" in data:
                        extras.write_memory(str(data["text"]))
                    result = {"text": extras.read_memory()}
                elif path == "/api/commands":
                    result = {"commands": session.command_list()}
                elif path == "/api/schedules":
                    if "schedules" in data:
                        session.config["schedules"] = [
                            {"id": s.get("id") or secrets.token_hex(4), "name": str(s.get("name", ""))[:80],
                             "prompt": str(s.get("prompt", "")), "cwd": str(s.get("cwd", "")),
                             "at": str(s.get("at", "")), "every": int(s.get("every") or 0),
                             "enabled": bool(s.get("enabled", True)), "last": s.get("last", 0)}
                            for s in data["schedules"] if s.get("prompt")]
                        session.save()
                    if data.get("run"):
                        task = next((t for t in session.config["schedules"] if t["id"] == data["run"]), None)
                        if task:
                            session.select(session.run_task(task))
                    result = {"schedules": session.config["schedules"]}
                elif path == "/api/git":
                    result = session.git_action(data)
                elif path == "/api/export":
                    result = {"path": session.export(str(data.get("format", "md")))}
                elif path == "/api/transcribe":
                    result = {"text": session.transcribe(str(data.get("data", "")), str(data.get("mime", "")))}
                elif path == "/api/term/open":
                    result = {"id": session.open_terminal(data.get("cols", 100), data.get("rows", 30))}
                elif path == "/api/term/input":
                    t = session.terminals.get(data.get("id"))
                    if t:
                        t.write(str(data.get("data", "")))
                elif path == "/api/term/resize":
                    t = session.terminals.get(data.get("id"))
                    if t:
                        t.resize(data.get("cols", 100), data.get("rows", 30))
                elif path == "/api/term/close":
                    t = session.terminals.pop(data.get("id"), None)
                    if t:
                        t.close()
                elif path == "/api/browser/view":
                    session.browser.viewers = max(0, session.browser.viewers + (1 if data.get("on") else -1))
                    if data.get("on") and session.browser.thread and session.browser.thread.is_alive():
                        session.browser.call(lambda page: None)
                elif path == "/api/browser/input":
                    try:
                        session.browser_input(data)
                    except NeedsBrowser as e:
                        session.events.put({"kind": "browser_status", "text": str(e), "install": True})
                elif path == "/api/browser/install":
                    session.install_browser()
                elif path in ("/api/models", "/api/test-connection"):
                    cfg = {**session.config, **{k: v for k, v in data.items() if k in ("provider", "model", "base_url", "api_key") and v}}
                    if cfg["provider"] not in DEFAULT_MODELS:
                        raise ValueError("Unknown provider")
                    try:
                        if path == "/api/models":
                            result = {"models": session.list_models(cfg)}
                        else:
                            result = {"ok": True, "reply": session.test_connection(cfg)}
                    except Exception as e:
                        result = {"ok": False, "error": explain_error(e, cfg)}
                elif path == "/api/update":
                    result = session.check_update()
                elif path == "/api/update/install":
                    session.install_update()
                elif path == "/api/share":
                    url = session.share(chat)
                    webbrowser.open(url)
                    result = {"url": url}
                elif path == "/api/report":
                    webbrowser.open(px.problem_report_url(REPO, __version__, BUILD, str(data.get("text", ""))))
                elif path == "/api/logs/open":
                    px.LOG_DIR.mkdir(parents=True, exist_ok=True)
                    px.open_path(px.LOG_DIR)
                elif path == "/api/settings/export":
                    result = px.export_settings(session.config, bool(data.get("include_keys")))
                elif path == "/api/settings/import":
                    if chat and chat.busy:
                        raise RuntimeError("Stop the current task first.")
                    new = px.import_settings(data.get("data"), session.config)
                    session.apply_config(new, keep_history=True)
                    session.config.update({k: new[k] for k in ("rules", "hooks", "schedules", "chat_order") if k in new})
                    session.save()
                    session.events.put({"kind": "config", "config": session.public_config()})
                elif path == "/api/window/new":
                    if not session.new_window:
                        raise RuntimeError("Extra windows are available in the desktop app.")
                    session.new_window()
                elif path == "/api/open-url":
                    url = str(data.get("url", ""))
                    if re.match(r"^https://github\.com/", url):
                        webbrowser.open(url)
                else:
                    return self._json({"error": "not found"}, 404)
                self._json(result)
            except Exception as e:
                px.log.warning("Request %s failed: %s", self.path.split("?")[0], e)
                self._json({"error": str(e)}, 400)

    return Handler


def start_server(cwd: str | None = None, port: int = 0, session: Session | None = None):
    """Start the engine on 127.0.0.1. Returns (server, url, session). port=0 picks a free port."""
    px.setup_logging()
    session = session or Session()
    session.load()
    session._handler_factory = make_handler
    try:
        session.apply_config({"cwd": cwd} if cwd else {})
    except Exception as e:  # e.g. no API key yet: the window opens Settings
        session.config["cwd"] = cwd or session.config["cwd"]
        session.events.put({"kind": "setup", "text": str(e)})
    token = secrets.token_urlsafe(16)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(session, token))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    session.start_scheduler()
    return server, f"http://127.0.0.1:{server.server_port}/?token={token}", session


def main(argv=None):
    p = argparse.ArgumentParser(prog="herness-web", description="Incredible Herness in a browser tab.")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--cwd", default=None)
    p.add_argument("--no-browser", action="store_true")
    args = p.parse_args(argv)
    server, url, session = start_server(args.cwd, args.port)
    print(f"Incredible Herness {__version__} at {url}\nCtrl-C to quit.")
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        session.shutdown()
        server.shutdown()


if __name__ == "__main__":
    main()
