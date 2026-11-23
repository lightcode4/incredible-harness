"""Tools the agent can call, described once in a provider-neutral way (name, description, JSON schema).

Workspace tools run here (shell, files, search, background jobs, web fetch).
Session tools (todo list, sub-agents, previews) are handled by the app session in gui.py.
"""

from __future__ import annotations

import base64
import difflib
import fnmatch
import html
import os
import re
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path

MAX_OUTPUT = 30_000  # characters returned to the model per tool call


def _schema(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


_S = {"type": "string"}
_EDIT_ITEM = {"type": "object", "properties": {"old_text": _S, "new_text": _S}, "required": ["old_text", "new_text"],
              "additionalProperties": False}

TOOLS = [
    {"name": "bash", "kind": "mutating",
     "description": "Run a shell command in the working directory and return its output. Use for builds, tests, git, "
                    "installing packages. Set run_in_background for servers or long jobs, then read them with bash_output.",
     "schema": _schema({"command": {"type": "string", "description": "The shell command."},
                        "timeout": {"type": "integer", "description": "Seconds before it is stopped (default 120)."},
                        "run_in_background": {"type": "boolean", "description": "Start it and return a job id at once."}},
                       ["command"])},
    {"name": "bash_output", "kind": "readonly",
     "description": "Read new output from a background job started with bash run_in_background, and whether it is still running.",
     "schema": _schema({"job_id": _S}, ["job_id"])},
    {"name": "kill_shell", "kind": "mutating", "description": "Stop a background job.",
     "schema": _schema({"job_id": _S}, ["job_id"])},
    {"name": "read_file", "kind": "readonly",
     "description": "Read a text file. Returns numbered lines. Use offset/limit for large files.",
     "schema": _schema({"path": {"type": "string", "description": "Relative to the working directory, or absolute."},
                        "offset": {"type": "integer", "description": "First line (1-based)."},
                        "limit": {"type": "integer", "description": "Maximum lines."}}, ["path"])},
    {"name": "write_file", "kind": "mutating",
     "description": "Create a file or overwrite it with the complete new contents. Creates parent folders.",
     "schema": _schema({"path": _S, "content": {"type": "string", "description": "Complete file contents."}}, ["path", "content"])},
    {"name": "edit_file", "kind": "mutating",
     "description": "Replace one exact occurrence of old_text with new_text. old_text must appear exactly once; "
                    "include surrounding lines to make it unique.",
     "schema": _schema({"path": _S, "old_text": _S, "new_text": _S}, ["path", "old_text", "new_text"])},
    {"name": "multi_edit", "kind": "mutating",
     "description": "Apply several exact replacements to one file in order, all or nothing. Each old_text must be unique "
                    "in the file at the moment it is applied.",
     "schema": _schema({"path": _S, "edits": {"type": "array", "items": _EDIT_ITEM}}, ["path", "edits"])},
    {"name": "list_files", "kind": "readonly",
     "description": "List files matching a glob such as '**/*.py' (skips .git, node_modules, .venv).",
     "schema": _schema({"pattern": _S}, ["pattern"])},
    {"name": "search", "kind": "readonly",
     "description": "Search file contents with a regular expression. Returns path:line:text.",
     "schema": _schema({"pattern": _S, "path": {"type": "string", "description": "Folder or file (default: working directory)."}},
                       ["pattern"])},
    {"name": "read_pdf", "kind": "readonly",
     "description": "Read a PDF file (path in the folder, or an http(s) URL). With Claude you see the real pages: text, "
                    "tables, charts and scanned pages. Use pages like '1-5' or '2,7' for long documents (default: first 100).",
     "schema": _schema({"source": {"type": "string", "description": "File path or URL of the PDF."},
                        "pages": {"type": "string", "description": "Optional page selection, e.g. '1-10' or '3,5,8-9'."}},
                       ["source"])},
    {"name": "fetch_url", "kind": "readonly", "client_web": True,
     "description": "Fetch a web page (or a local dev server URL) and return its text content.",
     "schema": _schema({"url": _S}, ["url"])},
    # Session tools (handled in gui.py)
    {"name": "todo_write", "kind": "session",
     "description": "Create or update your task list for multi-step work. Send the FULL list every time. "
                    "Use it for any task with 3+ steps; keep exactly one item in_progress while working.",
     "schema": _schema({"todos": {"type": "array", "items": {
         "type": "object", "additionalProperties": False, "required": ["content", "status"],
         "properties": {"content": _S, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}}}}},
         ["todos"])},
    {"name": "task", "kind": "session",
     "description": "Hand a self-contained research job to a sub-agent with read-only tools (read, list, search, fetch). "
                    "Use it to explore a large codebase or look things up without filling this conversation. "
                    "It returns only its final report, so say exactly what to find and what to report.",
     "schema": _schema({"description": {"type": "string", "description": "3-6 word label."},
                        "prompt": {"type": "string", "description": "Full instructions for the sub-agent."}},
                       ["description", "prompt"])},
    {"name": "open_preview", "kind": "session",
     "description": "Open a web page, local dev server URL or HTML file in a preview window so the user can see it.",
     "schema": _schema({"target": {"type": "string", "description": "URL or file path."}}, ["target"])},
    # Built-in browser (handled in gui.py). Clicking/typing/key presses ask for approval like file changes.
    {"name": "browser_navigate", "kind": "browser",
     "description": "Open a URL in your built-in browser (also local dev servers like http://localhost:3000). "
                    "The user sees it live in the Browser panel.",
     "schema": _schema({"url": _S}, ["url"])},
    {"name": "browser_read", "kind": "browser",
     "description": "Get the current page's URL, title, visible text and its interactive elements with click coordinates.",
     "schema": _schema({}, [])},
    {"name": "browser_screenshot", "kind": "browser",
     "description": "Take a screenshot of the current page (1280x800) to see layout and visuals. Use it to check pages you built.",
     "schema": _schema({}, [])},
    {"name": "browser_click", "kind": "browser",
     "description": "Click in the page: by x/y coordinates (from a screenshot or browser_read), by visible text, or by CSS selector.",
     "schema": _schema({"x": {"type": "integer"}, "y": {"type": "integer"}, "text": _S, "selector": _S}, [])},
    {"name": "browser_type", "kind": "browser",
     "description": "Type text into the focused field, or fill the field matching a CSS selector. submit=true presses Enter after.",
     "schema": _schema({"text": _S, "selector": _S, "submit": {"type": "boolean"}}, ["text"])},
    {"name": "browser_press", "kind": "browser",
     "description": "Press a key or shortcut in the page, e.g. Enter, Escape, Tab, ArrowDown, Meta+A.",
     "schema": _schema({"key": _S}, ["key"])},
    {"name": "browser_scroll", "kind": "browser", "description": "Scroll the page by dy pixels (negative scrolls up).",
     "schema": _schema({"dy": {"type": "integer"}}, ["dy"])},
]

TOOLS_BY_NAME = {t["name"]: t for t in TOOLS}
WORKSPACE_TOOLS = [t for t in TOOLS if t["kind"] not in ("session", "browser")]
READONLY_TOOLS = [t for t in TOOLS if t["kind"] == "readonly"]
MUTATING = {t["name"] for t in TOOLS if t["kind"] == "mutating"}
FILE_CHANGING = {"write_file", "edit_file", "multi_edit"}

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", "dist", "build"}
_TYPES = {"string": str, "integer": int, "boolean": bool, "array": list, "object": dict}


def _check(value, spec: dict, where: str) -> str | None:
    expected = _TYPES[spec["type"]]
    if not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
        return f"'{where}' must be of type {spec['type']}."
    if "enum" in spec and value not in spec["enum"]:
        return f"'{where}' must be one of {spec['enum']}."
    if spec["type"] == "array":
        for i, item in enumerate(value):
            err = _check(item, spec["items"], f"{where}[{i}]")
            if err:
                return err
    if spec["type"] == "object" and "properties" in spec:
        for key in spec.get("required", []):
            if key not in value:
                return f"'{where}' is missing '{key}'."
        for key, v in value.items():
            if key not in spec["properties"]:
                return f"'{where}' has unknown field '{key}'."
            err = _check(v, spec["properties"][key], f"{where}.{key}")
            if err:
                return err
    return None


def validate(name: str, args: object) -> str | None:
    """Check tool input against its schema. Returns an error message, or None when valid."""
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        return f"Unknown tool '{name}'."
    if not isinstance(args, dict):
        return "Tool input must be a JSON object."
    return _check(args, tool["schema"], "input")


def describe(name: str, args: dict) -> str:
    """One-line summary of a tool call, shown to the user."""
    if name == "bash":
        return f"$ {args.get('command', '')}" + ("  (background)" if args.get("run_in_background") else "")
    if name in ("edit_file", "multi_edit"):
        return f"edit {args.get('path')}"
    if name == "write_file":
        return f"write {args.get('path')}"
    if name == "read_file":
        return f"read {args.get('path')}"
    if name == "read_pdf":
        return f"read pdf {args.get('source')}" + (f" (pages {args['pages']})" if args.get("pages") else "")
    if name == "fetch_url":
        return f"fetch {args.get('url')}"
    if name.startswith("browser_"):
        detail = args.get("url") or args.get("text") or args.get("selector") or args.get("key") or (
            f"({args['x']}, {args['y']})" if "x" in args else args.get("dy", ""))
        return f"browser {name[8:]} {detail}".strip()
    if name in ("bash_output", "kill_shell"):
        return f"{name} {args.get('job_id')}"
    return f"{name} {args.get('pattern', '')} {args.get('path', '')}".strip()


def _parse_pages(spec: str | None, total: int, default_max: int = 100) -> list[int]:
    """'1-5,8' -> [0,1,2,3,4,7] (0-based, within range)."""
    if not spec:
        return list(range(min(total, default_max)))
    pages: list[int] = []
    for part in spec.replace(" ", "").split(","):
        if not part:
            continue
        a, _, b = part.partition("-")
        try:
            lo, hi = int(a), int(b) if b else int(a)
        except ValueError:
            raise ToolError(f"Bad page selection '{spec}'. Use e.g. '1-5' or '2,7'.")
        pages += [i - 1 for i in range(lo, hi + 1) if 1 <= i <= total]
    if not pages:
        raise ToolError(f"No pages selected; the PDF has {total} pages.")
    return sorted(set(pages))[:600]


def pdf_extract(data: bytes, pages: str | None = None) -> tuple[str, bytes, int]:
    """Returns (text with page markers, PDF bytes of just the selected pages, total page count)."""
    import io

    from pypdf import PdfReader, PdfWriter

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            reader.decrypt("")
        total = len(reader.pages)
    except Exception as e:
        raise ToolError(f"Could not open the PDF: {e}")
    chosen = _parse_pages(pages, total)
    texts = []
    for i in chosen:
        try:
            t = reader.pages[i].extract_text() or ""
        except Exception:
            t = ""
        texts.append(f"--- Page {i + 1} ---\n{t.strip()}")
    text = "\n\n".join(texts)
    if not any(t.split("\n", 1)[1:] and t.split("\n", 1)[1].strip() for t in texts):
        text += "\n\n(No text layer found: this looks like a scanned PDF. Claude can still read the page images.)"
    if len(chosen) == total:
        subset = data
    else:
        w = PdfWriter()
        for i in chosen:
            w.add_page(reader.pages[i])
        buf = io.BytesIO()
        w.write(buf)
        subset = buf.getvalue()
    note = f"PDF with {total} pages; showing {'all' if len(chosen) == total else f'{len(chosen)} selected'} pages."
    return note + "\n\n" + text, subset, total


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT:
        return text
    half = MAX_OUTPUT // 2
    return f"{text[:half]}\n\n... [{len(text) - MAX_OUTPUT} characters cut] ...\n\n{text[-half:]}"


class ToolError(Exception):
    pass


def _apply_edits(text: str, edits: list[dict]) -> str:
    for i, e in enumerate(edits, 1):
        count = text.count(e["old_text"])
        label = f"Edit {i}: " if len(edits) > 1 else ""
        if count == 0:
            raise ToolError(f"{label}old_text was not found. Read the file and copy the text exactly.")
        if count > 1:
            raise ToolError(f"{label}old_text appears {count} times. Include more surrounding lines so it is unique.")
        text = text.replace(e["old_text"], e["new_text"], 1)
    return text


class Workspace:
    """Runs workspace tools inside one working directory."""

    def __init__(self, cwd: str, on_file_change=None):
        self.cwd = Path(cwd).resolve()
        self.on_file_change = on_file_change  # called with (Path, old_text_or_None) before a file changes
        self.jobs: dict[str, dict] = {}
        self._job_n = 0

    def path(self, p: str) -> Path:
        path = Path(os.path.expanduser(p))
        return (path if path.is_absolute() else (self.cwd / path)).resolve()

    def run(self, name: str, args: dict):
        out = getattr(self, f"_{name}")(**args)
        return _truncate(out) if isinstance(out, str) else out  # lists are text+document parts

    # ---- diff preview (computed before approval; nothing is written) ----
    def preview_diff(self, name: str, args: dict) -> str | None:
        if name not in FILE_CHANGING:
            return None
        p = self.path(args["path"])
        old = p.read_text(errors="replace") if p.is_file() else ""
        try:
            if name == "write_file":
                new = args["content"]
            elif name == "edit_file":
                new = _apply_edits(old, [args])
            else:
                new = _apply_edits(old, args["edits"])
        except ToolError as e:
            return f"(cannot apply: {e})"
        try:
            rel = p.relative_to(self.cwd).as_posix()
        except ValueError:
            rel = str(p)
        diff = difflib.unified_diff(old.splitlines(), new.splitlines(), f"a/{rel}" if p.exists() else "/dev/null",
                                    f"b/{rel}", lineterm="", n=3)
        return "\n".join(diff)[:12000] or "(no changes)"

    def _before_change(self, p: Path):
        if self.on_file_change:
            self.on_file_change(p, p.read_text(errors="replace") if p.is_file() else None)

    # ---- shell ----
    def _bash(self, command: str, timeout: int = 120, run_in_background: bool = False) -> str:
        if run_in_background:
            return self._start_job(command)
        try:
            proc = subprocess.run(command, shell=True, cwd=self.cwd, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=max(1, min(timeout, 1800)), stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired as e:
            out = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            raise ToolError(f"Command timed out after {timeout}s. Use run_in_background for long jobs.\n{out}")
        out = proc.stdout + (("\n[stderr]\n" + proc.stderr) if proc.stderr else "")
        if proc.returncode != 0:
            raise ToolError(f"Exit code {proc.returncode}\n{out}")
        return out or "(no output)"

    def _start_job(self, command: str) -> str:
        self._job_n += 1
        job_id = f"job{self._job_n}"
        log = tempfile.NamedTemporaryFile(prefix=f"herness-{job_id}-", suffix=".log", delete=False)
        proc = subprocess.Popen(command, shell=True, cwd=self.cwd, stdout=log, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=(os.name != "nt"))
        log.close()
        self.jobs[job_id] = {"proc": proc, "log": log.name, "pos": 0, "command": command}
        return f"Started background job {job_id} (pid {proc.pid}). Read its output with bash_output."

    def _bash_output(self, job_id: str) -> str:
        job = self.jobs.get(job_id)
        if not job:
            raise ToolError(f"No job {job_id}. Jobs: {', '.join(self.jobs) or 'none'}")
        with open(job["log"], "rb") as f:
            f.seek(job["pos"])
            data = f.read()
            job["pos"] = f.tell()
        code = job["proc"].poll()
        state = "still running" if code is None else f"finished with exit code {code}"
        return f"[{job_id} {state}]\n" + (data.decode(errors="replace") or "(no new output)")

    def _kill_shell(self, job_id: str) -> str:
        job = self.jobs.get(job_id)
        if not job:
            raise ToolError(f"No job {job_id}.")
        if job["proc"].poll() is None:
            job["proc"].terminate()
            try:
                job["proc"].wait(5)
            except subprocess.TimeoutExpired:
                job["proc"].kill()
        return f"Stopped {job_id}."

    def stop_all_jobs(self):
        for job_id in list(self.jobs):
            try:
                self._kill_shell(job_id)
            except Exception:
                pass

    # ---- files ----
    def _read_file(self, path: str, offset: int = 1, limit: int = 2000) -> str:
        p = self.path(path)
        if not p.is_file():
            raise ToolError(f"No such file: {p}")
        if p.suffix.lower() == ".pdf":
            return pdf_extract(p.read_bytes())[0] + "\n\n(Tip: read_pdf shows the real pages, including tables and images.)"
        lines = p.read_text(errors="replace").splitlines()
        start = max(offset, 1)
        chunk = lines[start - 1: start - 1 + limit]
        body = "\n".join(f"{i:6}\t{line}" for i, line in enumerate(chunk, start))
        more = len(lines) - (start - 1 + len(chunk))
        return (body or "(empty file)") + (f"\n... {more} more lines (use offset)" if more > 0 else "")

    def _write_file(self, path: str, content: str) -> str:
        p = self.path(path)
        self._before_change(p)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} characters to {p}"

    def _edit_file(self, path: str, old_text: str, new_text: str) -> str:
        return self._multi_edit(path, [{"old_text": old_text, "new_text": new_text}])

    def _multi_edit(self, path: str, edits: list[dict]) -> str:
        p = self.path(path)
        if not p.is_file():
            raise ToolError(f"No such file: {p}")
        new = _apply_edits(p.read_text(errors="replace"), edits)
        self._before_change(p)
        p.write_text(new, encoding="utf-8")
        return f"Applied {len(edits)} edit(s) to {p}"

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
        root = self.path(path)
        if shutil.which("rg"):
            proc = subprocess.run(["rg", "-n", "--no-heading", "-m", "50", pattern, str(root)], capture_output=True,
                                  text=True, encoding="utf-8", errors="replace", cwd=self.cwd)
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
                        rel = f.relative_to(self.cwd) if f.is_relative_to(self.cwd) else f
                        hits.append(f"{rel}:{i}:{line[:300]}")
            except (UnicodeDecodeError, OSError):
                continue
            if len(hits) >= 300:
                break
        return "\n".join(hits) or "No matches."

    # ---- PDFs ----
    def _read_pdf(self, source: str, pages: str | None = None):
        if re.match(r"^https?://", source):
            data, _ = self._download(source)
            name = source.rstrip("/").rsplit("/", 1)[-1] or "document.pdf"
        else:
            p = self.path(source)
            if not p.is_file():
                raise ToolError(f"No such file: {p}")
            data, name = p.read_bytes(), p.name
        if not data.startswith(b"%PDF"):
            raise ToolError(f"{name} is not a PDF file.")
        text, subset, _ = pdf_extract(data, pages)
        if len(subset) > 30_000_000:
            return _truncate(text + "\n\n(Too large to send as pages; text only. Select fewer pages to see them.)")
        return [{"type": "pdf", "name": name, "data": base64.b64encode(subset).decode(), "text": _truncate(text)}]

    # ---- web ----
    def _download(self, url: str) -> tuple[bytes, str]:
        req = urllib.request.Request(url, headers={"User-Agent": "IncredibleHerness/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read(50_000_000), r.headers.get("Content-Type", "")
        except Exception as e:
            raise ToolError(f"Could not fetch {url}: {e}")

    def _fetch_url(self, url: str) -> str:
        if not re.match(r"^https?://", url):
            raise ToolError("Only http(s) URLs can be fetched.")
        raw, ctype = self._download(url)
        if "pdf" in ctype or raw.startswith(b"%PDF"):
            return pdf_extract(raw)[0]
        text = raw[:3_000_000].decode(errors="replace")
        if "html" in ctype or "<html" in text[:500].lower():
            text = re.sub(r"(?is)<(script|style|noscript|svg).*?</\1>", " ", text)
            text = re.sub(r"(?i)<(br|p|div|li|h[1-6]|tr)[^>]*>", "\n", text)
            text = html.unescape(re.sub(r"<[^>]+>", " ", text))
            text = re.sub(r"[ \t]+", " ", text)
            text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
        return text


def project_instructions(cwd: str) -> tuple[str, list[str]]:
    """Text of the folder's instruction files (HERNESS.md, CLAUDE.md, AGENTS.md) and their names."""
    names, parts = [], []
    for name in ("HERNESS.md", "CLAUDE.md", "AGENTS.md", ".herness/instructions.md"):
        p = Path(cwd) / name
        if p.is_file():
            try:
                parts.append(f"## {name}\n{p.read_text(errors='replace')[:20000]}")
                names.append(name)
            except OSError:
                pass
    return "\n\n".join(parts), names
