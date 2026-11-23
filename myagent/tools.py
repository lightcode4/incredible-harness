"""Tools the agent can call: run shell commands, read/write/edit files, find files, search text.

Each tool is described once in a provider-neutral way (name, description, JSON schema);
providers.py converts these to the Claude or OpenAI tool format.
"""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess
from pathlib import Path

MAX_OUTPUT = 30_000  # characters returned to the model per tool call


def _schema(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


TOOLS = [
    {
        "name": "bash",
        "description": "Run a shell command in the working directory and return its output (stdout and stderr). "
        "Use for builds, tests, git, installing packages and anything else a terminal does.",
        "schema": _schema(
            {
                "command": {"type": "string", "description": "The shell command to run."},
                "timeout": {"type": "integer", "description": "Seconds before the command is stopped (default 120)."},
            },
            ["command"],
        ),
    },
    {
        "name": "read_file",
        "description": "Read a text file. Returns numbered lines. Use offset/limit for large files.",
        "schema": _schema(
            {
                "path": {"type": "string", "description": "File path, relative to the working directory or absolute."},
                "offset": {"type": "integer", "description": "First line to return (1-based)."},
                "limit": {"type": "integer", "description": "Maximum number of lines to return."},
            },
            ["path"],
        ),
    },
    {
        "name": "write_file",
        "description": "Create a file or overwrite it with the given full contents. Creates parent folders.",
        "schema": _schema(
            {
                "path": {"type": "string", "description": "File path."},
                "content": {"type": "string", "description": "The complete new file contents."},
            },
            ["path", "content"],
        ),
    },
    {
        "name": "edit_file",
        "description": "Replace one exact occurrence of old_text with new_text in a file. "
        "old_text must appear exactly once; include surrounding lines to make it unique.",
        "schema": _schema(
            {
                "path": {"type": "string", "description": "File path."},
                "old_text": {"type": "string", "description": "Exact text to replace (must be unique in the file)."},
                "new_text": {"type": "string", "description": "Replacement text."},
            },
            ["path", "old_text", "new_text"],
        ),
    },
    {
        "name": "list_files",
        "description": "List files matching a glob pattern such as '**/*.py' (skips .git, node_modules, .venv).",
        "schema": _schema({"pattern": {"type": "string", "description": "Glob pattern, e.g. 'src/**/*.ts'."}}, ["pattern"]),
    },
    {
        "name": "search",
        "description": "Search file contents with a regular expression. Returns matching lines as path:line:text.",
        "schema": _schema(
            {
                "pattern": {"type": "string", "description": "Regular expression."},
                "path": {"type": "string", "description": "Folder or file to search (default: working directory)."},
            },
            ["pattern"],
        ),
    },
]

TOOLS_BY_NAME = {t["name"]: t for t in TOOLS}

# Tools that change something: the user is asked before each one runs (unless auto-approve is on).
NEEDS_APPROVAL = {"bash", "write_file", "edit_file"}

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", "dist", "build"}

_TYPES = {"string": str, "integer": int}


def validate(name: str, args: object) -> str | None:
    """Check tool input against its schema. Returns an error message, or None when valid."""
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        return f"Unknown tool '{name}'."
    if not isinstance(args, dict):
        return "Tool input must be a JSON object."
    schema = tool["schema"]
    for key in schema["required"]:
        if key not in args:
            return f"Missing required parameter '{key}'."
    for key, value in args.items():
        spec = schema["properties"].get(key)
        if spec is None:
            return f"Unknown parameter '{key}'."
        expected = _TYPES[spec["type"]]
        if not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
            return f"Parameter '{key}' must be a {spec['type']}."
    return None


def describe(name: str, args: dict) -> str:
    """One-line summary of a tool call, shown to the user."""
    if name == "bash":
        return f"$ {args.get('command', '')}"
    if name == "edit_file":
        return f"edit {args.get('path')}"
    if name == "write_file":
        return f"write {args.get('path')} ({len(args.get('content', ''))} chars)"
    if name == "read_file":
        return f"read {args.get('path')}"
    return f"{name} {args.get('pattern', '')} {args.get('path', '')}".strip()


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT:
        return text
    half = MAX_OUTPUT // 2
    return f"{text[:half]}\n\n... [{len(text) - MAX_OUTPUT} characters cut] ...\n\n{text[-half:]}"


class ToolError(Exception):
    pass


class Workspace:
    """Runs tools inside one working directory."""

    def __init__(self, cwd: str):
        self.cwd = Path(cwd).resolve()

    def _path(self, p: str) -> Path:
        path = Path(os.path.expanduser(p))
        return path if path.is_absolute() else (self.cwd / path)

    def run(self, name: str, args: dict) -> str:
        return _truncate(getattr(self, f"_{name}")(**args))

    def _bash(self, command: str, timeout: int = 120) -> str:
        try:
            proc = subprocess.run(
                command, shell=True, cwd=self.cwd, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=max(1, min(timeout, 1800)), stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired as e:
            out = (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            raise ToolError(f"Command timed out after {timeout}s.\n{out}")
        out = proc.stdout + (("\n[stderr]\n" + proc.stderr) if proc.stderr else "")
        if proc.returncode != 0:
            raise ToolError(f"Exit code {proc.returncode}\n{out}")
        return out or "(no output)"

    def _read_file(self, path: str, offset: int = 1, limit: int = 2000) -> str:
        p = self._path(path)
        if not p.is_file():
            raise ToolError(f"No such file: {p}")
        lines = p.read_text(errors="replace").splitlines()
        start = max(offset, 1)
        chunk = lines[start - 1 : start - 1 + limit]
        body = "\n".join(f"{i:6}\t{line}" for i, line in enumerate(chunk, start))
        more = len(lines) - (start - 1 + len(chunk))
        return (body or "(empty file)") + (f"\n... {more} more lines (use offset)" if more > 0 else "")

    def _write_file(self, path: str, content: str) -> str:
        p = self._path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return f"Wrote {len(content)} characters to {p}"

    def _edit_file(self, path: str, old_text: str, new_text: str) -> str:
        p = self._path(path)
        if not p.is_file():
            raise ToolError(f"No such file: {p}")
        text = p.read_text()
        count = text.count(old_text)
        if count == 0:
            raise ToolError("old_text was not found in the file. Read the file and copy the text exactly.")
        if count > 1:
            raise ToolError(f"old_text appears {count} times. Include more surrounding lines so it is unique.")
        p.write_text(text.replace(old_text, new_text, 1))
        return f"Edited {p}"

    def _walk(self, root: Path):
        if root.is_file():
            yield root
            return
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for f in filenames:
                yield Path(dirpath) / f

    def _list_files(self, pattern: str) -> str:
        hits = []
        for f in self._walk(self.cwd):
            rel = f.relative_to(self.cwd).as_posix()
            if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(rel, pattern.removeprefix("**/")):
                hits.append(rel)
            if len(hits) >= 500:
                break
        return "\n".join(sorted(hits)) or "No files matched."

    def _search(self, pattern: str, path: str = ".") -> str:
        root = self._path(path)
        if shutil.which("rg"):
            proc = subprocess.run(["rg", "-n", "--no-heading", "-m", "50", pattern, str(root)],
                                  capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=self.cwd)
            if proc.returncode > 1:
                raise ToolError(proc.stderr)
            return proc.stdout or "No matches."
        try:
            rx = re.compile(pattern)
        except re.error as e:
            raise ToolError(f"Bad regular expression: {e}")
        hits = []
        for f in self._walk(root):
            try:
                for i, line in enumerate(f.read_text(errors="strict").splitlines(), 1):
                    if rx.search(line):
                        hits.append(f"{f.relative_to(self.cwd) if f.is_relative_to(self.cwd) else f}:{i}:{line[:300]}")
            except (UnicodeDecodeError, OSError):
                continue
            if len(hits) >= 300:
                break
        return "\n".join(hits) or "No matches."
