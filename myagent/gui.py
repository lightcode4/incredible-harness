"""The app engine behind the window: one Session (settings, chats, approvals, undo, costs) served to the
page over a local HTTP server.

Security: listens only on 127.0.0.1 and every request must carry a secret token from the page URL,
so other websites cannot send commands to it.
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
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__
from ._build import BUILD, REPO
from .browser import Browser, NeedsBrowser, install_chromium
from .providers import ClaudeProvider, OpenAIProvider, cost_of
from .terminal import Terminal
from .tools import (MUTATING, READONLY_TOOLS, TOOLS, ToolError, Workspace, describe,
                    project_instructions, validate)

PAGE = (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
HOME = Path.home() / ".incredible-herness"
CONFIG_FILE = HOME / "settings.json"
CHATS = HOME / "chats"
DEFAULT_MODELS = {"claude": "claude-opus-5-5", "openai": "gpt-5", "local": "llama3.1"}
LOCAL_URL = "http://localhost:11434/v1"
IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}


def system_prompt(cwd: str, instructions: str = "") -> str:
    s = (
        "You are Incredible Herness, a capable software and computer agent working on the user's machine. "
        "You can run shell commands, read, write and edit files, search, fetch web pages, keep a task list, "
        "start sub-agents for research and open previews. "
        f"The working directory is {cwd} on {platform.system()} {platform.release()} ({platform.machine()}).\n\n"
        "How to work:\n"
        "- For tasks with 3 or more steps, write a task list with todo_write first and keep it updated.\n"
        "- Look before you change things: read the relevant files and understand the code.\n"
        "- Make focused edits; prefer edit_file/multi_edit over rewriting whole files.\n"
        "- Verify your work: run it, run its tests, or open a preview. If something fails, find and fix the cause.\n"
        "- Run servers and long jobs with run_in_background, then check them with bash_output.\n"
        "- Use the task tool to research large codebases without filling this conversation.\n"
        "- Ask the user when a decision is genuinely theirs. Keep replies short and concrete; "
        "say plainly what you changed and what you verified, and what you could not verify."
    )
    if instructions:
        s += "\n\nProject instructions from the user (follow them):\n" + instructions
    return s


SUBAGENT_SYSTEM = ("You are a research sub-agent with read-only tools. Investigate exactly what you are asked, "
                   "then reply with a concise, specific report (file paths, line numbers, facts). Do not ask questions.")


class Stop(Exception):
    pass


def _rule_kind(name: str) -> str:
    if name.startswith("browser_"):
        return "browser"
    return {"bash": "bash", "kill_shell": "bash", "write_file": "write", "edit_file": "edit", "multi_edit": "edit"}.get(name, name)


def rule_matches(rule: str, name: str, target: str) -> bool:
    m = re.match(r"^\s*(\w+)\s*(?:\((.*)\))?\s*$", rule)
    if not m or m.group(1) != _rule_kind(name):
        return False
    return m.group(2) is None or fnmatch.fnmatch(target, m.group(2).strip())


class Session:
    def __init__(self):
        self.events: "queue.Queue[dict]" = queue.Queue()
        self.lock = threading.Lock()
        self.busy = False
        self.cancel = threading.Event()
        self.pending: dict[str, tuple[threading.Event, dict]] = {}
        self.config = {"provider": "claude", "model": DEFAULT_MODELS["claude"], "effort": "high", "base_url": "",
                       "api_key": "", "cwd": str(Path.home()), "web": True, "compaction": True, "budget": 0.0,
                       "mcp": [], "rules": {}, "github_token": "", "auto": False, "plan": False}
        self.provider = None
        self.ws: Workspace | None = None
        self.instruction_files: list[str] = []
        self.preview_opener = webbrowser.open
        self.browser = Browser(HOME / "browser", self._browser_frame, self._browser_state)
        self.browser_url = ""
        self.terminals: dict[str, Terminal] = {}
        self._new_chat_state()

    # ------------------------------------------------------------------ chat state
    def _new_chat_state(self):
        self.chat_id = time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)
        self.chat_title = ""
        self.transcript: list[dict] = []
        self.checkpoints: dict[str, dict] = {}
        self.turn = 0
        self.turn_changes: dict[str, str | None] = {}
        self.cost = 0.0
        self.tokens = 0
        self.turn_cost = 0.0
        self.todos: list = []
        self._open = None

    # ------------------------------------------------------------------ events
    REPLAYABLE = {"user", "text", "thinking", "notice", "tool", "tool_result", "todos", "turn_end", "server_tool",
                  "preview", "plan_ready"}

    def emit(self, kind: str, **data):
        ev = {"kind": kind, **data}
        self.events.put(ev)
        if kind in ("text", "thinking"):
            if self._open == kind and self.transcript and self.transcript[-1]["kind"] == kind:
                self.transcript[-1] = {**self.transcript[-1], "text": self.transcript[-1]["text"] + data["text"]}
            else:
                self.transcript.append(dict(ev))
            self._open = kind
            return
        if kind == "end_block":
            self._open = None
        if kind in self.REPLAYABLE:
            self._open = None
            self.transcript.append({**ev, "kind": "tool"} if kind == "approve" else ev)
        if kind == "approve":
            self.transcript.append({**ev, "kind": "tool"})

    # UI protocol used by providers
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
        budget = float(self.config.get("budget") or 0)
        if budget > 0 and self.cost >= budget:
            raise Stop(f"Budget of ${budget:.2f} for this chat reached. Raise it in Settings to continue.")

    def _check(self):
        if self.cancel.is_set():
            raise Stop("Stopped.")

    # ------------------------------------------------------------------ settings
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

    def make_provider(self, cfg: dict, tools=None, system: str | None = None):
        tools = tools if tools is not None else TOOLS
        if system is None:
            text, _ = project_instructions(cfg["cwd"])
            system = system_prompt(cfg["cwd"], text)
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
        old_messages = self.provider.serialize() if (keep_history and self.provider) else None
        prov = self.make_provider(cfg)
        if old_messages and cfg["provider"] == self.config["provider"]:
            prov.restore(old_messages)
        if self.ws:
            self.ws.stop_all_jobs()
        self.config, self.provider = cfg, prov
        self.ws = Workspace(cfg["cwd"], on_file_change=self._snapshot)
        self.instruction_files = project_instructions(cfg["cwd"])[1]
        self.save()

    def public_config(self) -> dict:
        c = {k: v for k, v in self.config.items() if k not in ("api_key", "github_token", "rules")}
        c["api_key"] = bool(self.config["api_key"])
        c["github_token"] = bool(self.config["github_token"])
        c["rules"] = self.folder_rules()
        c["instructions"] = self.instruction_files
        c["build"] = BUILD
        c["version"] = __version__
        return c

    def folder_rules(self) -> dict:
        return self.config["rules"].get(self.config["cwd"], {"allow": [], "deny": []})

    # ------------------------------------------------------------------ undo checkpoints
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
        self.emit("notice", text=f"Undid changes to {', '.join(restored)}.")
        self.save_chat()
        return ", ".join(restored)

    # ------------------------------------------------------------------ permissions
    def _target(self, name: str, args: dict) -> str:
        if name.startswith("browser_"):
            return self.browser_url
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
            host = urlparse(self.browser_url).netloc
            return f"browser(*{host}*)" if host else "browser"
        return f"{_rule_kind(name)}(*)"

    def decide(self, name: str, args: dict) -> str:
        """'allow' | 'deny' | 'ask'"""
        rules, target = self.folder_rules(), self._target(name, args)
        if any(rule_matches(r, name, target) for r in rules.get("deny", [])):
            return "deny"
        if self.config["auto"] or any(rule_matches(r, name, target) for r in rules.get("allow", [])):
            return "allow"
        return "ask"

    def add_rule(self, rule: str, kind: str = "allow"):
        rules = self.config["rules"].setdefault(self.config["cwd"], {"allow": [], "deny": []})
        if rule not in rules[kind]:
            rules[kind].append(rule)
        self.save()

    # ------------------------------------------------------------------ tools
    def run_tool(self, name: str, args: dict) -> tuple[str, bool]:
        self._check()
        problem = validate(name, args)
        if problem:
            self.emit("tool", id=secrets.token_hex(4), summary=f"invalid {name} call", error=True)
            return f"Invalid input: {problem} Fix the call and try again.", True
        if name == "todo_write":
            self.todos = args["todos"]
            self.emit("todos", todos=self.todos)
            return "Task list updated.", False
        if name == "task":
            return self._subagent(args)
        if name == "open_preview":
            return self._preview(args["target"])
        if name.startswith("browser_"):
            return self._browser_tool(name, args)

        call_id = secrets.token_hex(6)
        info = {"id": call_id, "name": name, "summary": describe(name, args)}
        diff = self.ws.preview_diff(name, args)
        if diff:
            info["diff"] = diff
        if name in MUTATING:
            if self.config["plan"]:
                self.emit("tool", **info, blocked="Plan mode")
                return ("Plan mode is on: do not change anything yet. Finish researching, then present your plan "
                        "as your final message for the user to approve."), True
            verdict = self.decide(name, args)
            if verdict == "deny":
                self.emit("tool", **info, blocked="Blocked by a rule")
                return "Blocked by the user's permission rules for this folder. Do not try to work around it.", True
            if verdict == "ask":
                rule = self._suggest_rule(name, args)
                answer = self._ask(info, rule)
                if answer.get("decision") == "always":
                    self.add_rule(rule)
                    self.emit("config", config=self.public_config())
                elif answer.get("decision") != "allow":
                    fb = answer.get("feedback") or "The user declined."
                    self.emit("tool_result", id=call_id, output=f"Declined. {fb}", error=True)
                    return f"The user did not allow this. Their reply: {fb}", True
            else:
                self.emit("tool", **info)
        else:
            self.emit("tool", **info)
        try:
            out, err = self.ws.run(name, args), False
        except ToolError as e:
            out, err = str(e), True
        except Exception as e:
            out, err = f"{type(e).__name__}: {e}", True
        self.emit("tool_result", id=call_id, output=out[:8000], error=err)
        return out, err

    # ------------------------------------------------------------------ built-in browser
    BROWSER_ACTS = {"browser_click", "browser_type", "browser_press"}

    def _browser_frame(self, data, w, h):
        self.events.put({"kind": "browser_frame", "data": data, "w": w, "h": h})

    def _browser_state(self, st):
        self.browser_url = st.get("url", "")
        self.events.put({"kind": "browser_state", **st})

    def _browser_tool(self, name: str, args: dict):
        call_id = secrets.token_hex(6)
        info = {"id": call_id, "name": name, "summary": describe(name, args)}
        self.emit("browser_open")
        if name in self.BROWSER_ACTS:
            if self.config["plan"]:
                self.emit("tool", **info, blocked="Plan mode")
                return "Plan mode is on: you may look at pages but not click or type yet.", True
            verdict = self.decide(name, args)
            if verdict == "deny":
                self.emit("tool", **info, blocked="Blocked by a rule")
                return "Blocked by the user's permission rules.", True
            if verdict == "ask":
                rule = self._suggest_rule(name, args)
                answer = self._ask(info, rule)
                if answer.get("decision") == "always":
                    self.add_rule(rule)
                    self.emit("config", config=self.public_config())
                elif answer.get("decision") != "allow":
                    fb = answer.get("feedback") or "The user declined."
                    self.emit("tool_result", id=call_id, output=f"Declined. {fb}", error=True)
                    return f"The user did not allow this. Their reply: {fb}", True
            else:
                self.emit("tool", **info)
        else:
            self.emit("tool", **info)
        b = self.browser
        try:
            if name == "browser_navigate":
                out = b.navigate(args["url"])
            elif name == "browser_read":
                out = b.read()
            elif name == "browser_screenshot":
                img = b.screenshot()
                self.emit("tool_result", id=call_id, output=f"Screenshot of {self.browser_url}", error=False, image=img)
                return [{"type": "text", "text": f"Screenshot of {self.browser_url} (1280x800):"},
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
            self.emit("browser_status", text=str(e), install=True)
            out, err = f"{e} Ask the user to click 'Install browser engine' in the Browser panel.", True
        except Exception as e:
            out, err = f"{type(e).__name__}: {e}"[:2000], True
        self.emit("tool_result", id=call_id, output=out[:8000], error=err)
        return out, err

    def install_browser(self):
        def run():
            try:
                self.emit("browser_status", text="Downloading the browser engine (about 100 MB)…")
                install_chromium(lambda line: self.events.put({"kind": "browser_status", "text": line}))
                self.emit("browser_status", text="Browser engine installed.", done=True)
            except Exception as e:
                self.emit("browser_status", text=f"Install failed: {e}", install=True)
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

    # ------------------------------------------------------------------ terminals
    def open_terminal(self, cols: int, rows: int) -> str:
        t = Terminal(self.config["cwd"], lambda tid, data: self.events.put({"kind": "term", "id": tid, "data": data}),
                     lambda tid: self.events.put({"kind": "term_exit", "id": tid}), cols, rows)
        self.terminals[t.id] = t
        return t.id

    def shutdown(self):
        for t in self.terminals.values():
            t.close()
        self.browser.close()
        if self.ws:
            self.ws.stop_all_jobs()

    def _ask(self, info: dict, rule: str) -> dict:
        done, answer = threading.Event(), {}
        self.pending[info["id"]] = (done, answer)
        self.emit("approve", **info, rule=rule)
        try:
            while not done.wait(0.25):
                self._check()
        finally:
            self.pending.pop(info["id"], None)
        return answer

    def _subagent(self, args: dict) -> tuple[str, bool]:
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

        def sub_run(name, a):
            parent._check()
            SubUI.steps += 1
            problem = validate(name, a)
            if problem or name not in {t["name"] for t in READONLY_TOOLS}:
                return f"Not available: {problem or name}", True
            try:
                return self.ws.run(name, a), False
            except Exception as e:
                return str(e), True

        try:
            sub = self.make_provider(self.config, tools=READONLY_TOOLS, system=SUBAGENT_SYSTEM)
            sub.turn(args["prompt"], sub_run, SubUI())
            report = sub.last_text() or "(the sub-agent returned no report)"
            self.emit("tool_result", id=call_id, output=f"{SubUI.steps} steps\n\n{report}", error=False)
            return report, False
        except Stop:
            raise
        except Exception as e:
            self.emit("tool_result", id=call_id, output=str(e), error=True)
            return f"Sub-agent failed: {e}", True

    def _preview(self, target: str) -> tuple[str, bool]:
        if not re.match(r"^https?://", target):
            p = self.ws.path(target)
            if not p.exists():
                return f"No such file: {p}", True
            target = p.as_uri()
        try:
            self.preview_opener(target)
        except Exception as e:
            return f"Could not open preview: {e}", True
        self.emit("preview", target=target)
        return f"Opened a preview of {target} for the user.", False

    # ------------------------------------------------------------------ messages
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
        if self.config["plan"]:
            text = ("[Plan mode is ON: research and read as needed, but do not change anything. End with a clear, "
                    "numbered plan for the user to approve.]\n\n" + text)
        parts.append({"type": "text", "text": text})
        return parts

    def send(self, text: str, attachments: list[dict]) -> None:
        if not self.provider:
            raise RuntimeError("Finish setup in Settings first.")
        with self.lock:
            if self.busy:
                raise RuntimeError("Still working on the last message. Press Stop first.")
            parts = self._parts(text, attachments)
            self.busy = True
            self.cancel.clear()
        if not self.chat_title:
            self.chat_title = text.strip().splitlines()[0][:70] if text.strip() else "Attachment"
        self.emit("user", text=text, files=[a.get("name") for a in attachments or []], plan=self.config["plan"])
        threading.Thread(target=self._turn, args=(parts,), daemon=True).start()

    def _turn(self, parts) -> None:
        self.turn += 1
        turn_id = f"t{self.turn}"
        self.turn_changes, self.turn_cost = {}, 0.0
        self.emit("status", busy=True)
        try:
            self.provider.turn(parts, self.run_tool, self)
        except Stop as e:
            self.notice(str(e))
            self.provider.close_interrupted()
        except Exception as e:
            self.notice(f"{type(e).__name__}: {e}")
            self.provider.close_interrupted()
        finally:
            self.end_block()
            if self.turn_changes:
                self.checkpoints[turn_id] = self.turn_changes
            self.emit("turn_end", turn=turn_id, files=[Path(p).name for p in self.turn_changes],
                      cost=round(self.turn_cost, 4))
            if self.config["plan"]:
                self.emit("plan_ready")
            self.busy = False
            self.emit("status", busy=False)
            self.save_chat()

    # ------------------------------------------------------------------ chat history
    def save_chat(self):
        if not self.transcript:
            return
        try:
            CHATS.mkdir(parents=True, exist_ok=True)
            data = {"id": self.chat_id, "title": self.chat_title or "Untitled", "updated": time.time(),
                    "provider": self.config["provider"], "model": self.config["model"], "cwd": self.config["cwd"],
                    "messages": self.provider.serialize() if self.provider else [], "transcript": self.transcript,
                    "checkpoints": self.checkpoints, "cost": self.cost, "tokens": self.tokens, "todos": self.todos,
                    "turn": self.turn}
            path = CHATS / f"{self.chat_id}.json"
            path.write_text(json.dumps(data, default=str))
            os.chmod(path, 0o600)
        except Exception as e:
            self.emit("notice", text=f"Could not save chat: {e}")

    def list_chats(self) -> list[dict]:
        out = []
        for p in sorted(CHATS.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:200]:
            try:
                d = json.loads(p.read_text())
                out.append({"id": d["id"], "title": d.get("title", "Untitled"), "updated": d.get("updated", 0)})
            except (OSError, ValueError, KeyError):
                continue
        return out

    def open_chat(self, chat_id: str):
        path = CHATS / f"{Path(chat_id).name}.json"
        d = json.loads(path.read_text())
        changes = {"provider": d["provider"], "model": d["model"]}
        if Path(d.get("cwd", "")).is_dir():
            changes["cwd"] = d["cwd"]
        self.apply_config(changes)
        self.provider.restore(d["messages"])
        self._new_chat_state()
        self.chat_id, self.chat_title = d["id"], d.get("title", "")
        self.transcript, self.checkpoints = d.get("transcript", []), d.get("checkpoints", {})
        self.cost, self.tokens, self.todos, self.turn = d.get("cost", 0), d.get("tokens", 0), d.get("todos", []), d.get("turn", 0)
        self.events.put({"kind": "replay", "events": self.transcript, "chat": {"id": self.chat_id, "title": self.chat_title},
                         "cost": round(self.cost, 4), "tokens": self.tokens, "todos": self.todos,
                         "undoable": list(self.checkpoints)})
        self.emit("config", config=self.public_config())

    def new_chat(self):
        if self.provider:
            self.provider.clear()
        self._new_chat_state()
        self.events.put({"kind": "replay", "events": [], "chat": {"id": self.chat_id, "title": ""}, "cost": 0,
                         "tokens": 0, "todos": [], "undoable": []})

    # ------------------------------------------------------------------ project instructions & updates
    def instructions_file(self) -> Path:
        for name in self.instruction_files:
            return Path(self.config["cwd"]) / name
        return Path(self.config["cwd"]) / "HERNESS.md"

    def check_update(self) -> dict:
        req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/releases?per_page=5",
                                     headers={"Accept": "application/vnd.github+json", "User-Agent": "IncredibleHerness"})
        if self.config["github_token"]:
            req.add_header("Authorization", f"Bearer {self.config['github_token']}")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                releases = json.loads(r.read())
        except Exception as e:
            return {"error": f"Could not check for updates ({e}). If the repository is private, add a GitHub token in Settings."}
        best = None
        for rel in releases:
            m = re.match(r"build-(\d+)$", rel.get("tag_name", ""))
            if m and (best is None or int(m.group(1)) > best[0]):
                best = (int(m.group(1)), rel.get("html_url"))
        if not best:
            return {"latest": None, "current": BUILD, "update": False}
        return {"latest": best[0], "current": BUILD, "update": best[0] > BUILD, "url": best[1]}


# ---------------------------------------------------------------------- HTTP server

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

        def _json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlparse(self.path).path
            if path.startswith("/vendor/"):
                f = Path(__file__).parent / "static" / "vendor" / Path(path).name
                if f.is_file():
                    body = f.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/css" if f.suffix == ".css" else "application/javascript")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_error(404)
                return
            if not self._authed():
                return
            if path == "/":
                body = PAGE.replace("__TOKEN__", token).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/events":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                try:
                    first = {"kind": "config", "config": session.public_config()}
                    self.wfile.write(f"data: {json.dumps(first)}\n\n".encode())
                    self.wfile.flush()
                    while True:
                        try:
                            ev = session.events.get(timeout=15)
                            self.wfile.write(f"data: {json.dumps(ev, default=str)}\n\n".encode())
                        except queue.Empty:
                            self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
            else:
                self.send_error(404)

        def do_POST(self):
            if not self._authed():
                return
            try:
                data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                path = urlparse(self.path).path
                result: dict = {"ok": True}
                if path == "/api/send":
                    session.send(str(data.get("text", "")), data.get("attachments") or [])
                elif path == "/api/approve":
                    item = session.pending.get(data.get("id"))
                    if item:
                        item[1].update(decision=data.get("decision"), feedback=data.get("feedback", ""))
                        item[0].set()
                elif path == "/api/stop":
                    session.cancel.set()
                elif path == "/api/clear":
                    session.new_chat()
                elif path == "/api/chats":
                    result = {"chats": session.list_chats(), "current": session.chat_id}
                elif path == "/api/chat/open":
                    if session.busy:
                        raise RuntimeError("Stop the current task first.")
                    session.open_chat(str(data.get("id")))
                elif path == "/api/chat/delete":
                    (CHATS / f"{Path(str(data.get('id'))).name}.json").unlink(missing_ok=True)
                    if data.get("id") == session.chat_id:
                        session.new_chat()
                elif path == "/api/undo":
                    result = {"restored": session.undo(str(data.get("turn")))}
                elif path == "/api/config":
                    if session.busy and set(data) - {"auto", "plan"}:
                        raise RuntimeError("Stop the current task before changing settings.")
                    if set(data) <= {"auto", "plan"}:
                        session.config.update({k: bool(v) for k, v in data.items()})
                    else:
                        if "rules" in data:  # rules arrive for the current folder only
                            r = data.pop("rules")
                            session.config["rules"][data.get("cwd") or session.config["cwd"]] = {
                                "allow": [x.strip() for x in r.get("allow", []) if x.strip()],
                                "deny": [x.strip() for x in r.get("deny", []) if x.strip()]}
                        session.apply_config(data, keep_history=True)
                    session.emit("config", config=session.public_config())
                elif path == "/api/instructions":
                    f = session.instructions_file()
                    if "text" in data:
                        f.parent.mkdir(parents=True, exist_ok=True)
                        f.write_text(data["text"], encoding="utf-8")
                        session.apply_config({}, keep_history=True)
                        session.emit("config", config=session.public_config())
                    result = {"file": f.name, "text": f.read_text(errors="replace") if f.is_file() else ""}
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
                        session.browser.call(lambda page: None)  # push a fresh frame
                elif path == "/api/browser/input":
                    try:
                        session.browser_input(data)
                    except NeedsBrowser as e:
                        session.emit("browser_status", text=str(e), install=True)
                elif path == "/api/browser/install":
                    session.install_browser()
                elif path == "/api/update":
                    result = session.check_update()
                elif path == "/api/open-url":
                    url = str(data.get("url", ""))
                    if re.match(r"^https://github\.com/", url):
                        webbrowser.open(url)
                else:
                    return self._json({"error": "not found"}, 404)
                self._json(result)
            except Exception as e:
                self._json({"error": str(e)}, 400)

    return Handler


def start_server(cwd: str | None = None, port: int = 0, session: Session | None = None):
    """Start the engine on 127.0.0.1. Returns (server, url, session). port=0 picks a free port."""
    session = session or Session()
    session.load()
    try:
        session.apply_config({"cwd": cwd} if cwd else {})
    except Exception as e:  # e.g. no API key yet: the window opens Settings
        session.config["cwd"] = cwd or session.config["cwd"]
        session.emit("setup", text=str(e))
    token = secrets.token_urlsafe(16)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(session, token))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}/?token={token}", session


def main(argv=None):
    p = argparse.ArgumentParser(prog="herness-web", description="Incredible Herness in a browser tab.")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--cwd", default=None)
    p.add_argument("--no-browser", action="store_true")
    args = p.parse_args(argv)
    server, url, _ = start_server(args.cwd, args.port)
    print(f"Incredible Herness {__version__} at {url}\nCtrl-C to quit.")
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        _.shutdown()
        server.shutdown()


if __name__ == "__main__":
    main()
