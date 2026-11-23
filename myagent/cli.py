"""Terminal app: chat with an agent that can run commands and edit files in your folder."""

from __future__ import annotations

import argparse
import os
import platform
import sys

from . import __version__
from .tools import MUTATING, ToolError, Workspace, describe, validate

for _stream in (sys.stdout, sys.stderr):
    # Windows consoles default to cp1252; never crash on symbols. (Packaged desktop apps have no console: None.)
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

DIM, BOLD, CYAN, YELLOW, RED, GREEN, RESET = "\033[2m", "\033[1m", "\033[36m", "\033[33m", "\033[31m", "\033[32m", "\033[0m"
if sys.stdout is None or not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
    DIM = BOLD = CYAN = YELLOW = RED = GREEN = RESET = ""

DEFAULT_MODELS = {"claude": "claude-opus-5-5", "openai": "gpt-5", "local": "llama3.1"}
LOCAL_URL = "http://localhost:11434/v1"  # Ollama; LM Studio is http://localhost:1234/v1

HELP = f"""{BOLD}Commands{RESET}
  /provider claude|openai|local   switch provider (starts a fresh conversation)
  /model NAME                     switch model on the current provider (fresh conversation)
  /effort low|medium|high|xhigh|max   Claude thinking effort
  /auto on|off                    auto-approve commands and file edits (careful!)
  /clear                          forget the conversation
  /exit                           quit (or Ctrl-D)
Ctrl-C stops the current turn."""


def system_prompt(cwd: str) -> str:
    return (
        "You are a capable software and computer assistant working in the user's terminal. "
        "You can run shell commands and read, write and edit files with your tools. "
        f"The working directory is {cwd} on {platform.system()} {platform.release()} ({platform.machine()}). "
        "Work step by step: look before you change things, make focused edits, and check your work by "
        "running it or its tests. If a command fails, read the error and fix the cause. "
        "Ask the user when a decision is genuinely theirs. Keep replies short and concrete; "
        "say plainly what you changed and what you verified."
    )


class TerminalUI:
    def __init__(self):
        self._mode = None  # "text" | "thinking"

    def _switch(self, mode):
        if self._mode != mode:
            if self._mode is not None:
                print(RESET)
            self._mode = mode
            print(DIM if mode == "thinking" else "", end="")

    def text(self, chunk):
        self._switch("text")
        print(chunk, end="", flush=True)

    def thinking(self, chunk):
        self._switch("thinking")
        print(chunk, end="", flush=True)

    def end_block(self):
        if self._mode is not None:
            print(RESET)
        self._mode = None

    def notice(self, msg):
        self.end_block()
        print(f"{YELLOW}! {msg}{RESET}")

    def server_tool(self, summary):
        self.end_block()
        print(f"{CYAN}● {summary}{RESET}")

    def usage(self, model, u):
        pass

    def before_request(self):
        pass


class App:
    def __init__(self, args):
        self.args = args
        self.ws = Workspace(args.cwd)
        self.auto = args.auto
        self.ui = TerminalUI()
        self.provider_name = args.provider
        self.model = args.model or DEFAULT_MODELS[args.provider]
        self.effort = args.effort
        self.provider = self._make_provider()

    def _make_provider(self):
        from .providers import ClaudeProvider, OpenAIProvider

        sp = system_prompt(str(self.ws.cwd))
        if self.provider_name == "claude":
            return ClaudeProvider(self.model, sp, effort=self.effort)
        if self.provider_name == "openai":
            return OpenAIProvider(self.model, sp, base_url=self.args.base_url or os.environ.get("OPENAI_BASE_URL"),
                                  api_key=self.args.api_key, label_prefix="openai")
        return OpenAIProvider(self.model, sp, base_url=self.args.base_url or os.environ.get("LOCAL_BASE_URL", LOCAL_URL),
                              api_key=self.args.api_key or "local", label_prefix="local")

    # --- tool execution with approval -------------------------------------------------

    def run_tool(self, name: str, args: dict) -> tuple[str, bool]:
        self.ui.end_block()
        problem = validate(name, args)
        if problem:
            print(f"{RED}✗ bad tool call: {problem}{RESET}")
            return f"Invalid input: {problem} Fix the call and try again.", True
        summary = describe(name, args)
        print(f"{CYAN}● {summary}{RESET}")
        diff = self.ws.preview_diff(name, args)
        if diff:
            for line in diff.splitlines()[:80]:
                print((GREEN if line.startswith("+") else RED if line.startswith("-") else DIM) + line + RESET)
        if name in MUTATING and not self.auto:
            ans = input(f"{BOLD}  allow? [y]es / [n]o / [a]lways / or type feedback: {RESET}").strip()
            if ans.lower() in ("a", "always"):
                self.auto = True
            elif ans.lower() not in ("y", "yes", ""):
                reason = ans if ans.lower() not in ("n", "no") else "The user declined."
                return f"The user did not allow this. Their reply: {reason}", True
        try:
            out = self.ws.run(name, args)
            err = False
        except ToolError as e:
            out, err = str(e), True
        except Exception as e:  # keep the session alive on any tool crash
            out, err = f"{type(e).__name__}: {e}", True
        preview = (out if isinstance(out, str) else "\n".join(x.get("text", "") for x in out)).strip().splitlines()
        shown = "\n".join(preview[:8]) + (f"\n… ({len(preview) - 8} more lines)" if len(preview) > 8 else "")
        print(f"{RED if err else DIM}{shown}{RESET}")
        return out, err

    # --- REPL ---------------------------------------------------------------------------

    def command(self, line: str) -> bool:
        """Handle a /command. Returns False to quit."""
        cmd, _, arg = line[1:].partition(" ")
        arg = arg.strip()
        if cmd in ("exit", "quit"):
            return False
        if cmd == "help":
            print(HELP)
        elif cmd == "clear":
            self.provider.clear()
            print("Conversation cleared.")
        elif cmd == "provider" and arg in DEFAULT_MODELS:
            self.provider_name, self.model = arg, DEFAULT_MODELS[arg]
            self._swap()
        elif cmd == "model" and arg:
            self.model = arg
            self._swap()
        elif cmd == "effort" and arg in ("low", "medium", "high", "xhigh", "max"):
            self.effort = arg
            if hasattr(self.provider, "effort"):
                self.provider.effort = arg
            print(f"Effort: {arg}")
        elif cmd == "auto" and arg in ("on", "off"):
            self.auto = arg == "on"
            print(f"Auto-approve: {arg}")
        else:
            print(HELP)
        return True

    def _swap(self):
        try:
            self.provider = self._make_provider()
            print(f"Now using {self.provider.label} (new conversation).")
        except Exception as e:
            print(f"{RED}Could not switch: {e}{RESET}")

    def run(self):
        print(f"{BOLD}myagent {__version__}{RESET} · {self.provider.label} · {self.ws.cwd}")
        print(f"{DIM}Type a task. /help for commands. Commands and edits ask first"
              f"{' (auto-approve is ON)' if self.auto else ''}.{RESET}")
        while True:
            try:
                line = input(f"\n{BOLD}› {RESET}").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if not line:
                continue
            if line.startswith("/"):
                if not self.command(line):
                    return
                continue
            try:
                self.provider.turn(line, self.run_tool, self.ui)
                self.ui.end_block()
            except KeyboardInterrupt:
                self.ui.notice("Interrupted.")
                self.provider.close_interrupted()
            except Exception as e:
                self.ui.notice(f"{type(e).__name__}: {e}")
                self.provider.close_interrupted()


def main(argv=None):
    p = argparse.ArgumentParser(prog="myagent", description="Your own terminal coding agent.")
    p.add_argument("prompt", nargs="*", help="Optional first task (then stays interactive).")
    p.add_argument("-p", "--provider", choices=list(DEFAULT_MODELS), default=os.environ.get("MYAGENT_PROVIDER", "claude"))
    p.add_argument("-m", "--model", default=os.environ.get("MYAGENT_MODEL"))
    p.add_argument("--base-url", help="API base URL for openai/local providers (any OpenAI-compatible server).")
    p.add_argument("--api-key", help="API key for openai/local providers (default: OPENAI_API_KEY).")
    p.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--cwd", default=os.getcwd(), help="Working folder (default: current folder).")
    p.add_argument("--auto", action="store_true", help="Auto-approve commands and edits.")
    p.add_argument("--version", action="version", version=__version__)
    args = p.parse_args(argv)

    try:
        app = App(args)
    except Exception as e:
        sys.exit(f"Could not start {args.provider}: {e}")
    if args.prompt:
        try:
            app.provider.turn(" ".join(args.prompt), app.run_tool, app.ui)
            app.ui.end_block()
        except KeyboardInterrupt:
            app.ui.notice("Interrupted.")
            app.provider.close_interrupted()
    app.run()


if __name__ == "__main__":
    main()
