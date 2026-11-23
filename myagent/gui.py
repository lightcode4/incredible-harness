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

from . import __version__, extras, gallery, gitops, lsp, projects
from .catalog import BY_ID as PROVIDER_BY_ID, PROVIDERS, guess_service
from . import platform_extras as px
from .mcp_local import McpError, McpManager
from .mcp_remote import McpRemoteError, sign_in as mcp_sign_in
from ._build import BUILD, REPO
from .browser import Browser, NeedsBrowser, install_chromium
from .computer import Computer, ComputerError
from .providers import ClaudeProvider, OpenAIProvider, cost_of
from .terminal import Terminal
from .tools import (FILE_CHANGING, MUTATING, READONLY_TOOLS, TOOLS, ToolError, Workspace, apply_hunks, describe, hunks,
                    project_instructions, validate)

PAGE = (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
HOME = Path.home() / ".incredible-herness"
CONFIG_FILE = HOME / "settings.json"
CHATS = HOME / "chats"
HOOK_KINDS = ("session_start", "on_prompt", "before_tool", "after_edit", "before_finish")
DEFAULT_MODELS = {"claude": "claude-opus-5-5", "openai": "gpt-5", "local": "llama3.1"}
LOCAL_URL = "http://localhost:11434/v1"
IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}


def system_prompt(cwd: str, instructions: str = "", memory: str = "", computer: bool = False, language: str = "",
                  skills: dict | None = None) -> str:
    s = (
        "You are LightCode⁴, a capable software and computer agent working on the user's machine. "
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
        "- For a quick web page, chart, visual or small interactive demo that doesn't need to be saved in the project, "
        "reply with ONE self-contained ```html code block (inline CSS/JS; CDN libraries are fine): the app shows it live "
        "as an artifact. Use ```mermaid blocks for diagrams and $$...$$ or \\(...\\) for math; they are drawn for the user.\n"
        "- Ask the user when a decision is genuinely theirs. Keep replies short and concrete; say plainly what you "
        "changed, what you verified, and what you could not verify."
    )
    if computer:
        s += ("\n- You can also see and control the user's screen with the desktop_* tools. Take a screenshot first, "
              "act in small steps, and check the result with another screenshot.")
    if language:
        s += f"\n- Always write your replies to the user in {language}."
    if skills:
        s += ("\n\nSkills you can use (call use_skill with the name to load full instructions when a task matches):\n" +
              "\n".join(f"- {n}: {d}" for n, (d, _) in skills.items()))
    if memory.strip():
        s += "\n\nWhat you remember about the user (from earlier chats):\n" + memory
    if instructions:
        s += "\n\nProject instructions from the user (follow them):\n" + instructions
    return s


SUBAGENT_SYSTEM = ("You are a research sub-agent with read-only tools. Investigate exactly what you are asked, "
                   "then reply with a concise, specific report (file paths, line numbers, facts). Do not ask questions.")
SIDE_NOTE = ("\n\nThis is a SIDE CHAT: the user is asking a quick question next to their main conversation (included "
             "above as history). Answer briefly. You can only read and search; you cannot change files or run commands here.")
SUBAGENT_KINDS = {
    "explore": ("You are a fast explore sub-agent with read-only tools. Find the files, symbols and facts asked for "
                "(use grep, glob, code_symbols, find_references). Reply with a short list of paths, line numbers and facts."),
    "plan": ("You are a planning sub-agent with read-only tools. Study the code involved, then reply with a concrete "
             "step-by-step implementation plan: files to change, what to change in each, risks and how to verify."),
    "research": ("You are a research sub-agent with read-only tools, including web_search and fetch_url. Investigate "
                 "carefully, check sources, and reply with a concise report with facts, links and file references."),
}


class Stop(Exception):
    pass


CONTEXT_WINDOWS = [("haiku", 200_000), ("claude", 1_000_000), ("gemini", 1_000_000), ("gpt-4.1", 1_000_000),
                   ("gpt-5", 400_000), ("grok-4", 256_000), ("kimi", 256_000), ("qwen", 256_000), ("deepseek", 128_000),
                   ("llama", 128_000), ("mistral", 128_000), ("gpt-oss", 128_000), ("glm", 200_000)]


def context_window(model: str, override: int = 0) -> int:
    """How many tokens of conversation the model can hold (approximate; Settings can override)."""
    if override:
        return int(override)
    m = (model or "").lower()
    return next((n for key, n in CONTEXT_WINDOWS if key in m), 128_000)


class QuietUI:
    """Runs a helper model call without showing its text in the chat."""
    def __init__(self, chat): self.chat = chat
    def text(self, c): self.chat._check()
    def thinking(self, c): self.chat._check()
    def end_block(self): pass
    def notice(self, m): pass
    def server_tool(self, s): pass
    def usage(self, m, u): self.chat.usage(m, u, count_context=False)
    def before_request(self): self.chat.before_request()


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
                 unattended: bool = False, title: str = "", project: str | None = None, side: bool = False):
        self.app = app
        self.project = project  # id of the Project this chat belongs to (shared instructions and files)
        self.side = side  # a side chat: read-only quick questions that never touch the main chat
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
        self.context = 0  # tokens the model saw on its last request
        self._partial: set | None = None
        self.build()

    # ---------------------------------------------------------------- setup
    def build(self, keep_history: bool = False):
        cfg = {**self.app.config, "cwd": self.cwd, "project": self.project}
        old = self.provider.serialize() if (keep_history and self.provider) else None
        old_kind = getattr(self, "_provider_kind", None)
        comp = getattr(self.provider, "compactions", 0)
        if self.side:
            self.provider = self.app.make_provider(cfg, tools=[t for t in READONLY_TOOLS],
                                                   system=self.app.system_for(cfg) + SIDE_NOTE)
        else:
            self.provider = self.app.make_provider(cfg)
        self.provider.compactions = comp
        self._provider_kind = cfg["provider"]
        if old and old_kind == cfg["provider"]:
            self.provider.restore(old)
        if self.ws:
            self.ws.stop_all_jobs()
        self.ws = Workspace(self.cwd, on_file_change=self._snapshot)
        self.ws.on_job = lambda: self.emit("jobs_changed")
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

    def usage(self, model, u, count_context: bool = True):
        c = cost_of(model, u)
        self.tokens += sum(u.values())
        if c is not None:
            self.cost += c
            self.turn_cost += c
        if count_context:
            self.context = u.get("input", 0) + u.get("cache_read", 0) + u.get("cache_write", 0)
        extras.log_usage(model, u, c, self.id)
        self.emit("usage", cost=round(self.cost, 4), tokens=self.tokens, priced=c is not None, **self.context_info())

    def context_info(self) -> dict:
        window = context_window(self.app.config.get("model", ""), self.app.config.get("context_window") or 0)
        return {"context": self.context, "window": window}

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
            elif answer.get("decision") == "partial" and info.get("hunks"):
                self._partial = {int(i) for i in answer.get("hunks") or []}
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
            extras.notify("LightCode⁴", f"“{self.title or 'A chat'}” needs your approval.")
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
        if self.side and name not in {t["name"] for t in READONLY_TOOLS}:
            return "Side chats can only look things up; ask in the main chat to make changes.", True
        if name in self.app.mcp.names:
            return self._mcp_tool(name, args)
        problem = validate(name, args)
        if problem:
            self.emit("tool", id=secrets.token_hex(4), summary=f"invalid {name} call", error=True)
            return f"Invalid input: {problem} Fix the call and try again.", True
        hook = self.app.hooks_for(self.rules_dir).get("before_tool", "").strip()
        if hook and name not in ("todo_write", "use_skill"):
            code, out = extras.run_hook(hook, self.cwd, {"HERNESS_TOOL": name, "HERNESS_INPUT": json.dumps(args)[:100_000]})
            self.emit("hook", name="before tool", command=hook, ok=code == 0, output=out[-1500:])
            if code != 0:
                self.emit("tool", id=secrets.token_hex(4), name=name, summary=describe(name, args), blocked="Blocked by a hook")
                return f"Blocked by the user's before-tool check `{hook}`:\n{out[-3000:]}", True
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
        if name == "use_skill":
            self.emit("tool", id=secrets.token_hex(4), name=name, summary=describe(name, args))
            try:
                return extras.load_skill(self.cwd, args["name"]), False
            except ValueError as e:
                return str(e), True
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
            try:
                hs = hunks(*self.ws.proposed(name, args)[1:])
                if 1 < len(hs) <= 60 and name != "notebook_edit":
                    info["hunks"] = hs
            except ToolError:
                pass
        self._partial = None
        if name in MUTATING:
            refusal = self.gate(name, args, info)
            if refusal:
                return refusal
        else:
            self.emit("tool", **info)
        self.ws.sandbox = self.app.config.get("sandbox", "off")
        partial, self._partial = self._partial, None
        try:
            if partial is not None:
                p, old, new = self.ws.proposed(name, args)
                n = len(info["hunks"])
                self.ws.run("write_file", {"path": str(p), "content": apply_hunks(old, new, partial)})
                skipped = [h for h in info["hunks"] if h["i"] not in partial]
                out, err = (f"The user applied only {len(partial)} of your {n} changes to {p.name}. These changes were NOT "
                            "applied (do not re-apply them unless asked):\n" +
                            "\n".join(f"@ line {h['line']}\n{h['text']}" for h in skipped)), False
            else:
                out, err = self.ws.run(name, args), False
        except ToolError as e:
            out, err = str(e), True
        except Exception as e:
            out, err = f"{type(e).__name__}: {e}", True
        if not err and name in FILE_CHANGING:
            out += self._after_edit(self.ws.path(args["path"]))
        shown = out if isinstance(out, str) else "\n".join(x.get("text", "") for x in out)
        self.emit("tool_result", id=call_id, output=shown[:8000], error=err)
        if name in FILE_CHANGING or name in ("bash", "kill_shell"):
            self.emit("fs_changed", path=str(args.get("path", "")))  # Changes and Files tabs refresh live
        return out, err

    def _after_edit(self, path: Path) -> str:
        notes = []
        if self.app.config.get("diagnostics", True):
            problems = extras.diagnose(path, self.cwd)
            if not problems and lsp.find_server(path):
                problems = lsp.MANAGER.errors_after_edit(self.ws.cwd, path)
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
        kind = args.get("kind") if args.get("kind") in SUBAGENT_KINDS else None
        self.emit("tool", id=call_id, name="task", summary=f"{(kind or 'sub').capitalize()}-agent: {args['description']}")
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

        tools = [t for t in READONLY_TOOLS if kind in (None, "research") or not t.get("client_web")]
        allowed = {t["name"] for t in tools}

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
            sub = self.app.make_provider({**self.app.config, "cwd": self.cwd, "web": self.app.config.get("web") and kind != "explore"},
                                         tools=tools, system=SUBAGENT_KINDS.get(kind, SUBAGENT_SYSTEM),
                                         fast=kind in (None, "explore"))
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
        except (McpError, McpRemoteError) as e:
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
            out, err = (f"{type(e).__name__}: {e}. On macOS, allow LightCode⁴ under System Settings > Privacy & "
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
        first = not any(e["kind"] == "user" for e in self.transcript)
        mark = {"m": len(self.provider.serialize()), "turn": self.turn + 1, "c": getattr(self.provider, "compactions", 0)}
        self.emit("user", text=text, files=[a.get("name") for a in attachments or []], plan=self.app.config["plan"],
                  command=bool(expanded), idx=len(self.transcript), mark=mark)
        threading.Thread(target=self._turn, args=(parts, first, text), daemon=True).start()

    def _prompt_hooks(self, parts, first: bool, text: str):
        hooks = self.app.hooks_for(self.rules_dir)
        extra = []
        for key, label, on in (("session_start", "session start", first), ("on_prompt", "on prompt", True)):
            hook = (hooks.get(key) or "").strip()
            if not hook or not on:
                continue
            code, out = extras.run_hook(hook, self.cwd, {"HERNESS_PROMPT": text[:100_000]})
            self.emit("hook", name=label, command=hook, ok=code == 0, output=out[-1500:])
            if code != 0 and key == "on_prompt":
                raise Stop(f"The on-prompt check `{hook}` blocked this message:\n{out[-1500:]}")
            if out.strip():
                extra.append(f"[Context from the {label} hook `{hook}`]\n{out.strip()[-8000:]}")
        if extra:
            parts[-1] = {**parts[-1], "text": parts[-1]["text"] + "\n\n" + "\n\n".join(extra)}

    # ---------------------------------------------------------------- edit, fork, compact
    def _user_event(self, idx: int) -> dict:
        if self.busy:
            raise RuntimeError("Stop this chat first.")
        if not (0 <= idx < len(self.transcript)) or self.transcript[idx]["kind"] != "user" or "mark" not in self.transcript[idx]:
            raise ValueError("That message can't be edited (it was sent by an older version).")
        ev = self.transcript[idx]
        msgs = self.provider.serialize()
        if ev["mark"].get("c", 0) != getattr(self.provider, "compactions", 0) or ev["mark"]["m"] > len(msgs):
            raise ValueError("The conversation was summarized after that message, so it can't be rewound. Fork is still possible from later messages.")
        return ev

    def rewind(self, idx: int, text: str, undo_files: bool = True) -> str:
        ev = self._user_event(idx)
        restored = []
        if undo_files:
            for t in sorted((k for k in self.checkpoints if int(k[1:]) >= ev["mark"]["turn"]), key=lambda k: -int(k[1:])):
                restored.append(self.undo(t))
        self.provider.restore(json.loads(json.dumps(self.provider.serialize()[: ev["mark"]["m"]], default=str)))
        self.transcript = self.transcript[:idx]
        self.save()
        self.app.events.put(self.replay_event())
        if text.strip():
            self.send(text)
        return ", ".join(r for r in restored if r)

    def fork(self, idx: int) -> "Chat":
        if self.busy:
            raise RuntimeError("Stop this chat first.")
        # keep this message and its reply: cut just before the next message
        nxt = next((i for i in range(max(idx, 0) + 1, len(self.transcript)) if self.transcript[i]["kind"] == "user"), None)
        ev = self._user_event(nxt) if nxt is not None else None
        m = ev["mark"]["m"] if ev else len(self.provider.serialize())
        idx = nxt if nxt is not None else len(self.transcript)
        chat = self.app._new_chat_obj(self.cwd, worktree=self.worktree, title=f"Fork: {self.title or 'chat'}"[:70])
        chat.provider.restore(json.loads(json.dumps(self.provider.serialize()[:m], default=str)))
        chat.transcript = json.loads(json.dumps(self.transcript[:idx], default=str))
        for e in chat.transcript:
            e["chat"] = chat.id
        chat.turn, chat.cost, chat.tokens = self.turn, 0.0, 0
        chat.notice(f"Forked from “{self.title or 'chat'}”. Changes here don't affect the original chat.")
        chat.save()
        return chat

    def compact(self, focus: str = "") -> None:
        if self.busy:
            raise RuntimeError("Stop this chat first.")
        msgs = self.provider.serialize()
        if len([m for m in msgs if m["role"] != "system"]) < 2:
            raise ValueError("There's nothing to summarize yet.")
        self.busy = True
        self.emit("status", busy=True)
        threading.Thread(target=self._compact, args=(msgs, focus), daemon=True).start()

    def _compact(self, msgs, focus):
        try:
            self.cancel.clear()
            self.notice("Summarizing the conversation to free up context…")
            convo = json.dumps(msgs, default=str)[-400_000:]
            helper = self.app.make_provider({**self.app.config, "cwd": self.cwd, "web": False}, tools=[], system=(
                "Summarize this agent conversation so the work can continue: the user's goals, decisions, files changed, "
                "current state and next steps. Be specific and complete." + (f" Focus on: {focus}" if focus else "")))
            helper.turn("Conversation to summarize:\n" + convo, lambda n, a: ("No tools here.", True), QuietUI(self))
            summary = helper.last_text().strip()
            if not summary:
                raise ValueError("The model returned an empty summary.")
            self.provider.reset_with_summary(summary)
            self.context = 0
            self.emit("compacted", summary=summary[:4000], compactions=self.provider.compactions)
            self.notice("Conversation summarized. Earlier details were condensed; the work continues from the summary.")
        except Stop as e:
            self.notice(str(e))
        except Exception as e:
            self.notice("Could not summarize: " + explain_error(e, {**self.app.config, "cwd": self.cwd}))
        finally:
            self.busy = False
            self.emit("usage", cost=round(self.cost, 4), tokens=self.tokens, priced=True, **self.context_info())
            self.emit("status", busy=False)
            self.save()

    def _turn(self, parts, first: bool = False, text: str = "") -> None:
        self.turn += 1
        turn_id = f"t{self.turn}"
        self.turn_changes, self.turn_cost = {}, 0.0
        self.emit("status", busy=True)
        try:
            self._prompt_hooks(parts, first, text)
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
            if (self.app.current is not self or self.unattended) and not self.side:
                extras.notify("LightCode⁴", f"“{self.title}” finished.")

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
        if not self.transcript or self.side:
            return
        try:
            CHATS.mkdir(parents=True, exist_ok=True)
            data = {"id": self.id, "title": self.title or "Untitled", "updated": time.time(),
                    "provider": self._provider_kind, "model": self.app.config["model"], "cwd": self.cwd,
                    "worktree": self.worktree, "messages": self.provider.serialize(), "transcript": self.transcript,
                    "checkpoints": self.checkpoints, "cost": self.cost, "tokens": self.tokens, "todos": self.todos,
                    "turn": self.turn, "context": self.context, "project": self.project, "compactions": getattr(self.provider, "compactions", 0)}
            path = CHATS / f"{self.id}.json"
            path.write_text(json.dumps(data, default=str))
            os.chmod(path, 0o600)
        except Exception as e:
            self.notice(f"Could not save chat: {e}")

    def replay_event(self) -> dict:
        proj = self.app.project_by_id(self.project)
        return {"kind": "replay", "chat": {"id": self.id, "title": self.title, "worktree": self.worktree,
                                           "cwd": self.cwd, "project": proj["name"] if proj else None},
                "events": list(self.transcript), "cost": round(self.cost, 4), "tokens": self.tokens, "todos": self.todos,
                **self.context_info(), "compactions": getattr(self.provider, "compactions", 0),
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
                       "recent_folders": [], "ui_language": "en", "service": "", "keys": {}, "service_models": {},
                       "projects": [], "quick_hotkey": "<ctrl>+<alt>+<space>"}
        self.quick_toggle = self.quick_hide = self.focus_main = self.on_hotkey = None  # set by the desktop app
        self.side_chats: dict[str, Chat] = {}  # main chat id -> its side chat
        self.mcp = McpManager()
        self._oauth_fresh: dict[str, dict] = {}  # newest tokens per connector (survive settings being re-applied)
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
        self.tunnel, self.remote_url = None, ""
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
        c = self.config
        if not c.get("service"):  # older settings: one key for one provider -> remember it under its service
            c["service"] = guess_service(c["provider"], c.get("base_url", ""))
            if c.get("api_key"):
                c["keys"].setdefault(c["service"], c["api_key"])
            if c["service"] == "custom":
                c.setdefault("custom_base", c.get("base_url", ""))
            c["service_models"].setdefault(c["service"], c.get("model", ""))

    def save(self):
        try:
            HOME.mkdir(parents=True, exist_ok=True)
            CONFIG_FILE.write_text(json.dumps({k: v for k, v in self.config.items() if k not in ("auto", "plan")}, indent=2))
            os.chmod(CONFIG_FILE, 0o600)  # holds API keys: readable only by you
        except OSError:
            pass

    def tools_for(self, cfg: dict) -> list[dict]:
        self.mcp.on_oauth_update = self._save_oauth
        self.mcp.configure(cfg.get("mcp_local") or [], cfg.get("mcp") or [])
        return [t for t in TOOLS if t["kind"] != "desktop" or cfg.get("computer")] + self.mcp.tool_specs()

    def fast_model(self, cfg: dict) -> str:
        """Cheaper model for sub-agents and simple steps (model routing)."""
        if cfg.get("fast_model"):
            return cfg["fast_model"]
        return "claude-haiku-5-5" if cfg["provider"] == "claude" else cfg["model"]

    def system_for(self, cfg: dict) -> str:
        text, _ = project_instructions(cfg["cwd"])
        proj = self.project_by_id(cfg.get("project"))
        if proj:
            text = (projects.context(proj) + ("\n\n" + text if text else ""))
        return system_prompt(cfg["cwd"], text, extras.read_memory(), bool(cfg.get("computer")), cfg.get("language", ""),
                             extras.skills(cfg["cwd"]))

    def project_by_id(self, pid):
        return next((p for p in self.config.get("projects") or [] if p["id"] == pid), None) if pid else None

    def make_provider(self, cfg: dict, tools=None, system: str | None = None, fast: bool = False):
        tools = tools if tools is not None else self.tools_for(cfg)
        if fast:
            cfg = {**cfg, "model": self.fast_model(cfg)}
        if system is None:
            system = self.system_for(cfg)
        common = dict(tools=tools, compaction=cfg["compaction"])
        if cfg["provider"] == "claude":
            return ClaudeProvider(cfg["model"], system, effort=cfg["effort"], api_key=cfg["api_key"] or None,
                                  web=cfg["web"], mcp_servers=[], **common)  # connectors run in LightCode⁴ for every provider
        if cfg["provider"] == "openai":
            return OpenAIProvider(cfg["model"], system, base_url=cfg["base_url"] or os.environ.get("OPENAI_BASE_URL"),
                                  api_key=cfg["api_key"] or None, label_prefix="openai", **common)
        return OpenAIProvider(cfg["model"], system, base_url=cfg["base_url"] or LOCAL_URL,
                              api_key=cfg["api_key"] or "local", label_prefix="local", **common)

    def resolve_service(self, cfg: dict, new: dict) -> dict:
        """Fill provider kind, address and key from the chosen catalog service."""
        cfg = {**cfg, "keys": dict(cfg.get("keys") or {}), "service_models": dict(cfg.get("service_models") or {})}
        if "service" not in new and ("provider" in new or "base_url" in new):  # older Settings fields
            cfg["service"] = guess_service(cfg["provider"], fix_base_url(cfg.get("base_url", "")))
        svc = PROVIDER_BY_ID.get(cfg.get("service") or "") or PROVIDER_BY_ID["claude"]
        cfg["service"] = svc["id"]
        if new.get("api_key"):
            cfg["keys"][svc["id"]] = new["api_key"]
        cfg["provider"] = svc["kind"]
        if svc.get("custom") or svc["kind"] == "local":
            cfg["base_url"] = cfg.get("base_url") if ("base_url" in new and new["base_url"]) else (
                cfg.get("base_url") if self.config.get("service") == svc["id"] and cfg.get("base_url") else svc["base"])
        else:
            cfg["base_url"] = svc["base"]
        cfg["api_key"] = cfg["keys"].get(svc["id"], "")
        if new.get("model"):
            cfg["service_models"][svc["id"]] = new["model"]
        elif "service" in new and new["service"] != self.config.get("service"):
            cfg["model"] = cfg["service_models"].get(svc["id"]) or svc["model"]
        if not cfg.get("model"):
            cfg["model"] = svc["model"]
        return cfg

    def apply_config(self, new: dict, keep_history: bool = False) -> None:
        cfg = {**self.config, **{k: v for k, v in new.items() if k in self.config}}
        cfg = self.resolve_service(cfg, new)
        if cfg["provider"] not in DEFAULT_MODELS:
            raise ValueError(f"Unknown provider {cfg['provider']}")
        if not cfg["model"]:
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
        for m in cfg.get("mcp") or []:  # never replace a refreshed token with an older copy
            fresh = self._oauth_fresh.get(m.get("name"))
            if fresh and m.get("oauth") and fresh.get("expires_at", 0) > m["oauth"].get("expires_at", 0):
                m["oauth"] = fresh
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
        hidden = ("api_key", "github_token", "rules", "hooks", "voice_key", "mcp_local", "keys")
        c = {k: v for k, v in self.config.items() if k not in hidden}
        c["api_key"] = bool(self.config["api_key"])
        c["github_token"] = bool(self.config["github_token"])
        c["voice_key"] = bool(self.config["voice_key"])
        chat = self.current
        c["rules"] = chat.folder_rules() if chat else {"allow": [], "deny": []}
        c["hooks"] = self.hooks_for(chat.rules_dir) if chat else {}
        c["instructions"] = chat.instruction_files if chat else []
        c["chat_cwd"] = chat.cwd if chat else c["cwd"]
        c["chat_project"] = chat.project if chat else None
        c["build"], c["version"], c["phone_url"] = BUILD, __version__, self.phone_url
        c["remote_url"] = self.remote_url
        c["context_window"] = self.config.get("context_window") or 0
        c["commands"] = self.command_list()
        c["mcp_local"] = [{**m, "env": {k: "" for k in (m.get("env") or {})}} for m in self.config["mcp_local"]]
        c["mcp"] = [{"name": m.get("name"), "url": m.get("url"), "token": "", "has_token": bool(m.get("token")),
                     "signed_in": bool(m.get("oauth"))} for m in self.config["mcp"]]
        c["mcp_status"] = self.mcp.status()
        c["fast_model_default"] = self.fast_model(self.config)
        c["keys_set"] = sorted(k for k, v in (self.config.get("keys") or {}).items() if v)
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
        chat = Chat(self, cwd, chat_id=d["id"], worktree=wt, title=d.get("title", ""), project=d.get("project"))
        chat.provider.restore(d["messages"])
        chat.transcript, chat.checkpoints = d.get("transcript", []), d.get("checkpoints", {})
        chat.cost, chat.tokens, chat.todos, chat.turn = d.get("cost", 0), d.get("tokens", 0), d.get("todos", []), d.get("turn", 0)
        chat.context = d.get("context", 0)
        chat.provider.compactions = d.get("compactions", 0)
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

    # ---------------------------------------------------------------- side chat
    def side_send(self, text: str) -> str:
        main = self.current
        if not main:
            raise RuntimeError("Open a chat first.")
        side = self.side_chats.get(main.id)
        if side is None:
            side = Chat(self, main.cwd, chat_id=f"side-{main.id}", title="Side chat", project=main.project, side=True)
            hist = json.loads(json.dumps(main.provider.serialize(), default=str))
            if type(side.provider) is type(main.provider):
                side.provider.restore(hist)
                side.provider.close_interrupted()
            self.side_chats[main.id] = side
        side.send(text)
        return side.id

    def side_reset(self):
        main = self.current
        side = self.side_chats.pop(main.id, None) if main else None
        if side:
            side.cancel.set()

    # ---------------------------------------------------------------- quick chat
    def quick_send(self, text: str, client: str = ""):
        """Start a new chat from the quick-chat box and bring the main window forward."""
        if not text.strip():
            raise ValueError("Type a message first.")
        cwd = self.current.rules_dir if self.current else self.config["cwd"]
        chat = self._new_chat_obj(cwd)
        self.select(chat, client)
        chat.send(text)
        if self.quick_hide:
            self.quick_hide()
        if self.focus_main:
            self.focus_main()
        return chat.id

    # ---------------------------------------------------------------- search
    def search_chats(self, q: str, limit: int = 40) -> list[dict]:
        q = q.strip().lower()
        if len(q) < 2:
            return []
        hits = []
        for p in sorted(CHATS.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:1000]:
            try:
                d = json.loads(p.read_text())
            except (OSError, ValueError):
                continue
            live = self.chats.get(d.get("id"))
            events = live.transcript if live else d.get("transcript", [])
            title = (live.title if live else d.get("title")) or "Untitled"
            best = None
            if q in title.lower():
                best = ("title", title)
            for e in events:
                t = e.get("text") if e.get("kind") in ("user", "text") else None
                if t and q in t.lower():
                    i = t.lower().index(q)
                    best = ("user" if e["kind"] == "user" else "reply", ("…" if i > 50 else "") + t[max(0, i - 50): i + 90].replace("\n", " ") + "…")
                    break
            if best:
                hits.append({"id": d["id"], "title": title, "where": best[0], "snippet": best[1], "updated": d.get("updated", 0),
                             "project": d.get("project")})
            if len(hits) >= limit:
                break
        return hits

    # ---------------------------------------------------------------- projects
    def save_project(self, data: dict) -> dict:
        projs = self.config.setdefault("projects", [])
        proj = self.project_by_id(data.get("id"))
        if proj is None:
            proj = {"id": "p" + secrets.token_hex(4), "created": time.time(), "files": []}
            projs.append(proj)
        for k in ("name", "folder", "instructions", "color"):
            if k in data:
                proj[k] = str(data[k])[:20000]
        proj["name"] = proj.get("name", "").strip() or "Untitled project"
        if proj.get("folder") and not Path(os.path.expanduser(proj["folder"])).is_dir():
            raise ValueError(f"No such folder: {proj['folder']}")
        self.save()
        return proj

    def delete_project(self, pid: str):
        self.config["projects"] = [p for p in self.config.get("projects") or [] if p["id"] != pid]
        projects.delete_files(pid)
        self.save()

    def new_project_chat(self, pid: str, client: str = ""):
        proj = self.project_by_id(pid)
        if not proj:
            raise ValueError("That project no longer exists.")
        folder = os.path.expanduser(proj.get("folder") or "") or self.config["cwd"]
        chat = self._new_chat_obj(folder, project=pid)
        self.select(chat, client)

    def list_chats(self) -> list[dict]:
        seen, out = set(), []
        for c in self.chats.values():
            if c.transcript:
                out.append({"id": c.id, "title": c.title or "Untitled", "updated": time.time(), "running": c.busy,
                            "approval": bool(c.pending_info), "worktree": bool(c.worktree), "project": c.project})
                seen.add(c.id)
        for p in sorted(CHATS.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:200]:
            if p.stem in seen:
                continue
            try:
                d = json.loads(p.read_text())
                out.append({"id": d["id"], "title": d.get("title", "Untitled"), "updated": d.get("updated", 0),
                            "running": False, "approval": False, "worktree": bool(d.get("worktree")), "project": d.get("project")})
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
                                     headers={"Accept": "application/vnd.github+json", "User-Agent": "LightCode4"})
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

    def _save_oauth(self, name: str, oauth: dict):
        self._oauth_fresh[name] = oauth
        for m in self.config["mcp"]:
            if m.get("name") == name:
                m["oauth"] = oauth
        self.save()

    def mcp_login(self, name: str):
        entry = next((m for m in self.config["mcp"] if m.get("name") == name), None)
        if not entry:
            raise ValueError("Save the connector first, then click Sign in.")

        def run():
            try:
                self.events.put({"kind": "toast", "text": f"Opening the {name} sign-in page in your browser…"})
                entry["oauth"] = mcp_sign_in(entry["url"])
                self.save()
                self.mcp.config_key = ""  # restart connectors with the new sign-in
                if self.current and not self.current.busy:
                    self.current.build(keep_history=True)
                self.events.put({"kind": "toast", "text": f"Signed in to {name}. Its tools are ready."})
                self.events.put({"kind": "config", "config": self.public_config()})
            except Exception as e:
                px.log.warning("MCP sign-in failed for %s: %s", name, e)
                self.events.put({"kind": "toast", "text": f"{name} sign-in failed: {e}"})
        threading.Thread(target=run, daemon=True).start()

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
                               md, f"LightCode⁴: {chat.title}")

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
            self.set_remote(False)
            self.phone_server.shutdown()
            self.phone_server, self.phone_url = None, ""

    def set_remote(self, on: bool) -> str:
        """Access from anywhere: a free Cloudflare quick tunnel to the phone page. The Mac must stay on."""
        if not on:
            px.stop_tunnel(self.tunnel)
            self.tunnel, self.remote_url = None, ""
            return ""
        if self.remote_url:
            return self.remote_url
        if not self.phone_server:
            self.config["phone"] = True
            self.set_phone(True)
            self.save()
        if not self.phone_server:
            raise RuntimeError("Phone access could not start.")
        self.tunnel, base = px.start_tunnel(self.phone_server.server_port)
        self.remote_url = base + "/?" + self.phone_url.split("?", 1)[1]
        return self.remote_url

    def shutdown(self):
        self._sched_stop.set()
        self.mcp.stop()
        lsp.MANAGER.close_all()
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
                vend = Path(__file__).parent / "static" / "vendor"
                f = vend / "fonts" / Path(path).name if path.startswith("/vendor/fonts/") else vend / Path(path).name
                if f.is_file():
                    ctype = {".css": "text/css", ".woff2": "font/woff2", ".json": "application/json"}.get(f.suffix, "application/javascript")
                    return self._send(f.read_bytes(), ctype)
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
                        item[1].update(decision=data.get("decision"), feedback=data.get("feedback", ""),
                                       hunks=[int(i) for i in data.get("hunks") or []])
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
                elif path == "/api/jobs":
                    result = {"jobs": chat.ws.job_list() if chat else []}
                elif path == "/api/jobs/stop":
                    chat.ws._kill_shell(str(data.get("id")))
                    result = {"jobs": chat.ws.job_list()}
                elif path == "/api/files/list":
                    result = {"items": chat.ws.list_dir(str(data.get("path", ""))), "root": str(chat.ws.cwd)}
                elif path == "/api/files/read":
                    result = chat.ws.read_for_view(str(data.get("path", "")))
                elif path == "/api/files/write":
                    result = chat.ws.write_from_view(str(data.get("path", "")), str(data.get("text", "")), data.get("mtime"))
                    chat.emit("fs_changed", path=str(data.get("path", "")))
                elif path == "/api/side/send":
                    result = {"side": session.side_send(str(data.get("text", "")))}
                elif path == "/api/side/reset":
                    session.side_reset()
                elif path == "/api/side/stop":
                    side = session.side_chats.get(session.chat_id)
                    if side:
                        side.cancel.set()
                elif path == "/api/chats/search":
                    result = {"hits": session.search_chats(str(data.get("q", "")))}
                elif path == "/api/projects":
                    result = {"projects": [{**p, "files": projects.list_files(p["id"])} for p in session.config.get("projects") or []],
                              "chats": session.list_chats()}
                elif path == "/api/project/save":
                    result = {"project": session.save_project(data.get("project") or {})}
                elif path == "/api/project/delete":
                    session.delete_project(str(data.get("id")))
                elif path == "/api/project/file":
                    if data.get("remove"):
                        projects.remove_file(str(data.get("id")), str(data.get("name")))
                    else:
                        projects.add_file(str(data.get("id")), str(data.get("name")), str(data.get("data", "")))
                    result = {"files": projects.list_files(str(data.get("id")))}
                elif path == "/api/project/chat":
                    session.new_project_chat(str(data.get("id")), client)
                elif path == "/api/quick/send":
                    result = {"chat": session.quick_send(str(data.get("text", "")))}
                elif path == "/api/quick/hide":
                    if session.quick_hide:
                        session.quick_hide()
                elif path == "/api/quick/toggle":
                    if not session.quick_toggle:
                        raise RuntimeError("Quick chat works in the LightCode⁴ desktop app.")
                    session.quick_toggle()
                elif path == "/api/gallery":
                    result = {"skills": gallery.catalog(), "skills_custom": [n for n in extras.skills(session.config["cwd"]) if n not in gallery.BY_ID],
                              "connectors": [m["name"] for m in session.config.get("mcp") or []] + [m["name"] for m in session.config.get("mcp_local") or []]}
                elif path == "/api/gallery/skill":
                    (gallery.remove if data.get("remove") else gallery.install)(str(data.get("id")))
                    result = {"skills": gallery.catalog()}
                elif path == "/api/lsp":
                    result = {"langs": lsp.status()}
                elif path == "/api/lsp/install":
                    lang = str(data.get("lang"))
                    def _inst(lang=lang):
                        try:
                            lsp.install(lang)
                            session.events.put({"kind": "lsp_install", "lang": lang, "ok": True, "langs": lsp.status()})
                        except Exception as e:
                            session.events.put({"kind": "lsp_install", "lang": lang, "ok": False, "error": str(e), "langs": lsp.status()})
                    threading.Thread(target=_inst, daemon=True).start()
                elif path == "/api/usage":
                    result = extras.usage_summary(int(data.get("days", 30)))
                elif path == "/api/artifact":
                    name = re.sub(r"[^\w .-]+", "", str(data.get("name") or "artifact")).strip()[:60] or "artifact"
                    content = str(data.get("content", ""))
                    if data.get("action") == "open":
                        import tempfile as _tf
                        f = Path(_tf.gettempdir()) / f"lightcode4-{secrets.token_hex(4)}.html"
                        f.write_text(content, encoding="utf-8")
                        session.preview_opener(f.as_uri())
                        result = {"path": str(f)}
                    else:
                        f = extras.downloads_dir() / f"{name}.{'svg' if data.get('lang') == 'svg' and content.lstrip().startswith('<svg') else 'html'}"
                        n = 1
                        while f.exists():
                            n += 1
                            f = f.with_name(f"{name} ({n}){f.suffix}")
                        f.write_text(content, encoding="utf-8")
                        result = {"path": str(f)}
                elif path == "/api/rewind":
                    result = {"restored": chat.rewind(int(data.get("idx", -1)), str(data.get("text", "")),
                                                      bool(data.get("undo_files", True)))}
                elif path == "/api/fork":
                    new = chat.fork(int(data.get("idx", 10**9)))
                    session.select(new, client)
                    if str(data.get("text", "")).strip():
                        new.send(str(data["text"]))
                elif path == "/api/compact":
                    chat.compact(str(data.get("focus", "")))
                elif path == "/api/remote":
                    result = {"url": session.set_remote(bool(data.get("on")))}
                    session.events.put({"kind": "config", "config": session.public_config()})
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
                            session.config["hooks"][folder] = {k: str(h.get(k, "")).strip() for k in HOOK_KINDS}
                        if "mcp" in data:  # remote connectors: blank token keeps the saved one; keep sign-ins
                            old = {m["name"]: m for m in session.config["mcp"]}
                            for m in data["mcp"]:
                                prev = old.get(m.get("name")) or {}
                                m["token"] = m.get("token") or prev.get("token", "")
                                if prev.get("oauth") and prev.get("url") == m.get("url"):
                                    m["oauth"] = prev["oauth"]
                                m.pop("has_token", None)
                                m.pop("signed_in", None)
                        if "mcp_local" in data:  # blank env values mean "keep the saved secret"
                            old = {m["name"]: m for m in session.config["mcp_local"]}
                            for m in data["mcp_local"]:
                                prev = (old.get(m.get("name")) or {}).get("env") or {}
                                m["env"] = {k: (v or prev.get(k, "")) for k, v in (m.get("env") or {}).items()}
                        hotkey_changed = "quick_hotkey" in data and data["quick_hotkey"] != session.config.get("quick_hotkey")
                        session.apply_config(data, keep_history=True)
                        if hotkey_changed and session.on_hotkey:
                            session.on_hotkey(session.config.get("quick_hotkey", ""))
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
                elif path == "/api/providers":
                    keys = session.config.get("keys") or {}
                    result = {"providers": [{**{k: v for k, v in p.items()}, "has_key": bool(keys.get(p["id"])) or bool(p.get("nokey")),
                                             "model_saved": (session.config.get("service_models") or {}).get(p["id"], "")} for p in PROVIDERS],
                              "current": session.config.get("service")}
                elif path == "/api/provider/key":
                    sid = str(data.get("service", ""))
                    if sid not in PROVIDER_BY_ID:
                        raise ValueError("Unknown provider")
                    session.config["keys"][sid] = str(data.get("key", "")).strip()
                    if not session.config["keys"][sid]:
                        session.config["keys"].pop(sid)
                    if sid == session.config.get("service"):
                        session.apply_config({"service": sid}, keep_history=True)
                    session.save()
                    session.events.put({"kind": "config", "config": session.public_config()})
                elif path == "/api/provider/check":  # test a provider (key, address, model) without switching to it
                    sid = str(data.get("service", ""))
                    tmp = session.resolve_service({**session.config, "service": sid, "model": data.get("model") or ""},
                                                  {"service": sid, **({"api_key": data["key"]} if data.get("key") else {}),
                                                   **({"base_url": data["base_url"]} if data.get("base_url") else {}),
                                                   **({"model": data["model"]} if data.get("model") else {})})
                    try:
                        if data.get("what") == "models":
                            result = {"ok": True, "models": session.list_models(tmp)}
                        else:
                            result = {"ok": True, "reply": session.test_connection(tmp)}
                    except Exception as e:
                        result = {"ok": False, "error": explain_error(e, tmp)}
                elif path == "/api/mcp/login":
                    session.mcp_login(str(data.get("name", "")))
                elif path == "/api/selfcheck":
                    from .selfcheck import run as selfcheck_run
                    result = {"results": selfcheck_run(session, model_test=bool(data.get("model")))}
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
    p = argparse.ArgumentParser(prog="herness-web", description="LightCode⁴ in a browser tab.")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--cwd", default=None)
    p.add_argument("--no-browser", action="store_true")
    args = p.parse_args(argv)
    server, url, session = start_server(args.cwd, args.port)
    print(f"LightCode⁴ {__version__} at {url}\nCtrl-C to quit.")
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        session.shutdown()
        server.shutdown()


if __name__ == "__main__":
    main()
