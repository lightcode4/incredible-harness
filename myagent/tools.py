"""Tools the agent can call, described once in a provider-neutral way (name, description, JSON schema).

Workspace tools run here (shell, files, search, background jobs, web fetch).
Session tools (todo list, sub-agents, previews) are handled by the app session in gui.py.
"""

from __future__ import annotations

import base64
import json
import difflib
import fnmatch
import html
import os
import re
import shutil
import subprocess
import tempfile
import time
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
    {"name": "notebook_edit", "kind": "mutating",
     "description": "Edit a Jupyter notebook (.ipynb) cell: mode replace (default) sets a cell's source, insert adds a new "
                    "cell at that index, delete removes it. Read the notebook with read_file first to see cell numbers.",
     "schema": _schema({"path": _S, "cell": {"type": "integer", "description": "0-based cell index."},
                        "source": _S, "cell_type": {"type": "string", "enum": ["code", "markdown"]},
                        "mode": {"type": "string", "enum": ["replace", "insert", "delete"]}}, ["path", "cell"])},
    {"name": "read_pdf", "kind": "readonly",
     "description": "Read a PDF file (path in the folder, or an http(s) URL). With Claude you see the real pages: text, "
                    "tables, charts and scanned pages. Use pages like '1-5' or '2,7' for long documents (default: first 100).",
     "schema": _schema({"source": {"type": "string", "description": "File path or URL of the PDF."},
                        "pages": {"type": "string", "description": "Optional page selection, e.g. '1-10' or '3,5,8-9'."}},
                       ["source"])},
    {"name": "web_search", "kind": "readonly", "client_web": True,
     "description": "Search the web and get the top results (title, link, snippet). Then use fetch_url to read a page.",
     "schema": _schema({"query": _S}, ["query"])},
    {"name": "code_symbols", "kind": "readonly",
     "description": "Code navigation: list the definitions (functions, classes, methods, types, constants) in a file, or find "
                    "where a name is defined across the project (go to definition). Python is exact; other languages use patterns.",
     "schema": _schema({"name": {"type": "string", "description": "Name to find the definition of (optional)."},
                        "path": {"type": "string", "description": "File or folder (default: whole project)."}}, [])},
    {"name": "code_intel", "kind": "readonly",
     "description": "Exact code intelligence from the language server installed for this file's language (Pyright, "
                    "TypeScript, gopls, rust-analyzer, clangd, sourcekit-lsp...): go to definition, find references, hover "
                    "(type and docs) or diagnostics (errors and warnings). Give line and column (1-based) or a symbol name. "
                    "If no server is installed it says so; then use code_symbols / find_references.",
     "schema": _schema({"action": {"type": "string", "enum": ["definition", "references", "hover", "diagnostics"]},
                        "path": _S, "line": {"type": "integer"}, "column": {"type": "integer"},
                        "symbol": {"type": "string", "description": "Name to look up in the file instead of line/column."}},
                       ["action", "path"])},
    {"name": "find_references", "kind": "readonly",
     "description": "Code navigation: find every place a name is used (whole-word matches, definitions marked).",
     "schema": _schema({"name": _S, "path": {"type": "string", "description": "Folder or file (default: whole project)."}}, ["name"])},
    {"name": "notebook_run", "kind": "mutating",
     "description": "Run a Jupyter notebook's code cells in order (fresh Python), save their outputs into the notebook and "
                    "report results. Stops at the first error. Use 'upto' to run only the first N cells.",
     "schema": _schema({"path": _S, "upto": {"type": "integer", "description": "Run cells 0..upto (optional)."}}, ["path"])},
    {"name": "use_skill", "kind": "session",
     "description": "Load a skill's full instructions (and the list of its files/scripts) when the task matches one of the "
                    "skills listed in your instructions.",
     "schema": _schema({"name": _S}, ["name"])},
    {"name": "read_document", "kind": "readonly",
     "description": "Read a Word (.docx), PowerPoint (.pptx) or Excel (.xlsx) file as text: paragraphs and headings, slide by "
                    "slide, or sheet by sheet as rows.",
     "schema": _schema({"path": _S}, ["path"])},
    {"name": "read_media", "kind": "readonly",
     "description": "Understand an audio or video file: key frames (as images you can see) and a transcript of the sound "
                    "(needs an OpenAI key for the transcript).",
     "schema": _schema({"path": _S, "frames": {"type": "integer", "description": "How many frames from a video (1-12, default 6)."}}, ["path"])},
    {"name": "geocode", "kind": "readonly",
     "description": "Find places on the map (OpenStreetMap): an address, city or landmark -> names with latitude/longitude.",
     "schema": _schema({"query": _S}, ["query"])},
    {"name": "places_nearby", "kind": "readonly",
     "description": "Points of interest near a location (OpenStreetMap), e.g. cafe, restaurant, pharmacy, hotel, museum, "
                    "supermarket. Returns names, coordinates, address, opening hours. Show results on a map with an html "
                    "artifact using Leaflet (https://unpkg.com/leaflet@1.9.4/dist/leaflet.js) and OpenStreetMap tiles.",
     "schema": _schema({"lat": {"type": "number"}, "lon": {"type": "number"}, "kind": _S,
                        "radius": {"type": "integer", "description": "Meters (default 1000, max 20000)."}}, ["lat", "lon", "kind"])},
    {"name": "generate_image", "kind": "mutating",
     "description": "Create an image from a description, or edit an existing image (give its path as 'edit'). Saves a PNG "
                    "into the project and shows it. Uses the image provider chosen in Settings > Services.",
     "schema": _schema({"prompt": _S, "path": {"type": "string", "description": "Where to save, e.g. images/hero.png."},
                        "size": {"type": "string", "description": "e.g. 1024x1024, 1536x1024, 1024x1536."},
                        "edit": {"type": "string", "description": "Path of an image to edit instead of starting fresh."}}, ["prompt"])},
    {"name": "upload_file", "kind": "mutating",
     "description": "Upload a file to the user's cloud storage (S3 or Cloudflare R2, set up in Settings > Services) and "
                    "return a link to share it.",
     "schema": _schema({"path": _S}, ["path"])},
    {"name": "github_get", "kind": "readonly",
     "description": "Read from GitHub's REST API with the user's connected GitHub account (Settings > GitHub). GET only. "
                    "path examples: /user/repos?per_page=50, /repos/OWNER/REPO/issues?state=open, /repos/OWNER/REPO/pulls, "
                    "/repos/OWNER/REPO/actions/runs?per_page=5, /repos/OWNER/REPO/contents/README.md. Git itself and the gh "
                    "command line are also signed in when you run commands.",
     "schema": _schema({"path": _S}, ["path"])},
    {"name": "github_api", "kind": "mutating",
     "description": "Change something on GitHub with the user's connected account: create issues, comment, open or update pull "
                    "requests, add labels, create repositories... method is POST, PATCH, PUT or DELETE; body is the JSON "
                    "payload. Example: POST /repos/OWNER/REPO/pulls {title, head, base, body}.",
     "schema": _schema({"method": _S, "path": _S, "body": {"type": "object"}}, ["method", "path"])},
    {"name": "chrome_tabs", "kind": "readonly",
     "description": "List the tabs open in the user's own Chrome (needs the LightCode4 Chrome extension). The user is signed in "
                    "to their accounts there: be careful, and prefer the built-in browser for anything that doesn't need them.",
     "schema": _schema({}, [])},
    {"name": "chrome_read", "kind": "readonly",
     "description": "Read the text, links and form fields of a tab in the user's Chrome (default: the active tab).",
     "schema": _schema({"tab": {"type": "integer"}}, [])},
    {"name": "chrome_screenshot", "kind": "readonly",
     "description": "Screenshot of the visible part of the active tab in the user's Chrome.",
     "schema": _schema({}, [])},
    {"name": "chrome_open", "kind": "mutating",
     "description": "Open a URL in the user's Chrome: in a new tab (default) or the active tab (new_tab=false). Or switch to a tab by id.",
     "schema": _schema({"url": _S, "new_tab": {"type": "boolean"}, "tab": {"type": "integer"}}, [])},
    {"name": "chrome_click", "kind": "mutating",
     "description": "Click in a tab of the user's Chrome by visible text or CSS selector (always asks the user first).",
     "schema": _schema({"text": _S, "selector": _S, "tab": {"type": "integer"}}, [])},
    {"name": "chrome_type", "kind": "mutating",
     "description": "Type into a field of the user's Chrome (CSS selector, or the focused field); submit=true presses Enter / submits the form.",
     "schema": _schema({"text": _S, "selector": _S, "submit": {"type": "boolean"}, "tab": {"type": "integer"}}, ["text"])},
    {"name": "fetch_url", "kind": "readonly", "client_web": True,
     "description": "Fetch a web page (or a local dev server URL) and return its text content.",
     "schema": _schema({"url": _S}, ["url"])},
    # Session tools (handled in gui.py)
    {"name": "connect_github", "kind": "session",
     "description": "Connect the user's GitHub account when they ask (or when a GitHub task needs it and it isn't connected): "
                    "shows a sign-in card with a short code and opens GitHub's approval page.",
     "schema": _schema({}, [])},
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
                        "prompt": {"type": "string", "description": "Full instructions for the sub-agent."},
                        "kind": {"type": "string",
                                 "description": "explore: fast search of the codebase (cheaper model); plan: design an "
                                                "implementation plan; research: careful investigation incl. the web; or the "
                                                "name of one of the user's custom agents listed in your instructions. "
                                                "Several task calls in one message run in parallel."}},
                       ["description", "prompt"])},
    {"name": "open_preview", "kind": "session",
     "description": "Open a web page, local dev server URL or HTML file in a preview window so the user can see it.",
     "schema": _schema({"target": {"type": "string", "description": "URL or file path."}}, ["target"])},
    {"name": "remember", "kind": "session",
     "description": "Save a short, lasting note about the user's preferences or recurring facts (not task details) so "
                    "future chats know it, e.g. 'Prefers TypeScript and pnpm'. Use only when the user states a preference "
                    "or asks you to remember something.",
     "schema": _schema({"note": _S}, ["note"])},
    # Computer use (handled in gui.py; only offered when enabled in Settings). Every action asks first.
    {"name": "desktop_screenshot", "kind": "desktop", "description": "Take a screenshot of the user's whole screen.",
     "schema": _schema({}, [])},
    {"name": "desktop_click", "kind": "desktop",
     "description": "Click on the screen at x, y in screenshot coordinates. button: left/right; double for double-click.",
     "schema": _schema({"x": {"type": "integer"}, "y": {"type": "integer"},
                        "button": {"type": "string", "enum": ["left", "right"]}, "double": {"type": "boolean"}}, ["x", "y"])},
    {"name": "desktop_type", "kind": "desktop", "description": "Type text with the keyboard into the focused app.",
     "schema": _schema({"text": _S}, ["text"])},
    {"name": "desktop_key", "kind": "desktop", "description": "Press keys or a shortcut, e.g. 'enter', 'cmd+c', 'ctrl+shift+t'.",
     "schema": _schema({"keys": _S}, ["keys"])},
    {"name": "desktop_scroll", "kind": "desktop", "description": "Scroll by dy (positive = down), optionally at x, y.",
     "schema": _schema({"dy": {"type": "integer"}, "x": {"type": "integer"}, "y": {"type": "integer"}}, ["dy"])},
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
WORKSPACE_TOOLS = [t for t in TOOLS if t["kind"] not in ("session", "browser", "desktop")]
READONLY_TOOLS = [t for t in TOOLS if t["kind"] == "readonly"]
MUTATING = {t["name"] for t in TOOLS if t["kind"] == "mutating"}
FILE_CHANGING = {"write_file", "edit_file", "multi_edit", "notebook_edit", "notebook_run"}

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


def decode_text(raw: bytes) -> str | None:
    """Text from a file in any common encoding: UTF-8 (with or without BOM), UTF-16/32 (Windows Notepad "Unicode"),
    Windows-1252 / Latin-1 (older Windows and Mac files). None when it is really binary."""
    for bom, enc in ((b"\xef\xbb\xbf", "utf-8-sig"), (b"\xff\xfe\x00\x00", "utf-32"), (b"\x00\x00\xfe\xff", "utf-32"),
                     (b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16")):
        if raw.startswith(bom):
            return raw.decode(enc, errors="replace")
    head = raw[:4000]
    if head and head.count(b"\x00") > len(head) // 4:  # UTF-16 without a BOM: every other byte is zero
        zeros_odd = head[1::2].count(b"\x00") > head[0::2].count(b"\x00")
        try:
            return raw.decode("utf-16-le" if zeros_odd else "utf-16-be")
        except UnicodeDecodeError:
            return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    if b"\x00" in head:
        return None
    return raw.decode("cp1252", errors="replace")


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
    if name.startswith("desktop_"):
        d = args.get("text") or args.get("keys") or (f"({args['x']}, {args['y']})" if "x" in args else args.get("dy", ""))
        return f"computer {name[8:]} {d}".strip()
    if name == "notebook_edit":
        return f"notebook {args.get('mode', 'replace')} cell {args.get('cell')} in {args.get('path')}"
    if name == "remember":
        return f"remember: {args.get('note', '')[:60]}"
    if name == "read_document":
        return f"read {args.get('path')}"
    if name == "read_media":
        return f"watch/listen {args.get('path')}"
    if name == "geocode":
        return f"find on map: {args.get('query')}"
    if name == "places_nearby":
        return f"{args.get('kind')} near {args.get('lat')},{args.get('lon')}"
    if name == "generate_image":
        return f"image: {str(args.get('prompt', ''))[:60]}"
    if name == "upload_file":
        return f"upload {args.get('path')}"
    if name.startswith("chrome_"):
        what = args.get("url") or args.get("text") or args.get("selector") or (f"tab {args['tab']}" if args.get("tab") else "")
        return f"Chrome: {name[7:]} {what}".strip()
    if name == "github_get":
        return f"GitHub: GET {args.get('path')}"
    if name == "github_api":
        return f"GitHub: {str(args.get('method', '')).upper()} {args.get('path')}"
    if name == "web_search":
        return f"search the web: {args.get('query')}"
    if name == "code_symbols":
        return f"symbols {args.get('name') or ''} {args.get('path') or ''}".strip()
    if name == "find_references":
        return f"references to {args.get('name')}"
    if name == "code_intel":
        where = args.get("symbol") or (f"line {args.get('line')}" if args.get("line") else "")
        return f"{args.get('action')} {args.get('path')} {where}".strip()
    if name == "notebook_run":
        return f"run notebook {args.get('path')}"
    if name == "use_skill":
        return f"use skill {args.get('name')}"
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
        self.sandbox = "off"  # off | folder | folder_no_net (see platform_extras.wrap_command)
        self.jobs: dict[str, dict] = {}
        self._job_n = 0
        self.on_job = None  # called when a background job starts (live Tasks tab)
        self.services: dict = {}  # keys and choices from Settings > Services
        self.github_token = ""  # Settings > GitHub
        self.env_extra: dict = {}  # extra environment for commands (GitHub sign-in for git and gh)
        self.chrome = None  # the Chrome extension bridge

    def path(self, p: str) -> Path:
        path = Path(os.path.expanduser(p))
        return (path if path.is_absolute() else (self.cwd / path)).resolve()

    def run(self, name: str, args: dict):
        out = getattr(self, f"_{name}")(**args)
        return _truncate(out) if isinstance(out, str) else out  # lists are text+document parts

    # ---- diff preview (computed before approval; nothing is written) ----
    def proposed(self, name: str, args: dict) -> tuple[Path, str, str]:
        """(path, current text, text after this edit). Nothing is written."""
        p = self.path(args["path"])
        old = p.read_text(errors="replace") if p.is_file() else ""
        if name == "write_file":
            new = args["content"]
        elif name == "edit_file":
            new = _apply_edits(old, [args])
        elif name == "notebook_edit":
            from .extras import edit_notebook
            try:
                new = edit_notebook(old, args["cell"], args.get("source"), args.get("cell_type"), args.get("mode", "replace"))
            except (ValueError, KeyError) as e:
                raise ToolError(str(e))
        else:
            new = _apply_edits(old, args["edits"])
        return p, old, new

    def preview_diff(self, name: str, args: dict) -> str | None:
        if name not in FILE_CHANGING or name == "notebook_run":
            return None
        try:
            p, old, new = self.proposed(name, args)
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
        argv = self._sandboxed(command)
        try:
            proc = subprocess.run(argv or command, shell=argv is None, cwd=self.cwd, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=max(1, min(timeout, 1800)), stdin=subprocess.DEVNULL, env=self._env())
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
        argv = self._sandboxed(command)
        proc = subprocess.Popen(argv or command, shell=argv is None, cwd=self.cwd, stdout=log, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=(os.name != "nt"), env=self._env())
        log.close()
        self.jobs[job_id] = {"proc": proc, "log": log.name, "pos": 0, "command": command, "started": time.time()}
        if self.on_job:
            self.on_job()
        return f"Started background job {job_id} (pid {proc.pid}). Read its output with bash_output."

    def _env(self):
        return {**os.environ, **self.env_extra} if self.env_extra else None

    def _sandboxed(self, command: str) -> list[str] | None:
        if self.sandbox == "off":
            return None
        from .platform_extras import wrap_command
        argv = wrap_command(command, str(self.cwd), self.sandbox)
        if argv is None:
            raise ToolError("The command sandbox is on, but this computer has no sandbox support (Windows). "
                            "Turn it off in Settings > Permissions to run commands.")
        return argv

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

    def job_list(self) -> list[dict]:
        out = []
        for job_id, job in self.jobs.items():
            try:
                with open(job["log"], "rb") as f:
                    f.seek(0, 2)
                    size = f.tell()
                    f.seek(max(0, size - 6000))
                    tail = f.read().decode(errors="replace")
            except OSError:
                tail = ""
            code = job["proc"].poll()
            out.append({"id": job_id, "command": job["command"], "running": code is None, "code": code,
                        "started": job.get("started", 0), "pid": job["proc"].pid, "tail": tail})
        return out

    def find_files(self, q: str, limit: int = 30) -> list[str]:
        """Project files matching q (for @-mentions): best matches first."""
        q = q.lower().strip().lstrip("@")
        hits = []
        for i, f in enumerate(self._walk(self.cwd)):
            if i > 20000:
                break
            rel = f.relative_to(self.cwd).as_posix()
            low = rel.lower()
            if not q:
                score = len(rel)
            elif q in f.name.lower():
                score = (0 if f.name.lower().startswith(q) else 100) + len(rel)
            elif q in low:
                score = 300 + len(rel)
            else:
                it = iter(low)
                if not all(ch in it for ch in q):
                    continue
                score = 600 + len(rel)
            hits.append((score, rel))
        return [r for _, r in sorted(hits)[:limit]]

    def list_dir(self, rel: str) -> list[dict]:
        p = self.path(rel or ".")
        self._inside(p)
        if not p.is_dir():
            raise ToolError(f"Not a folder: {rel}")
        items = []
        for c in sorted(p.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower()))[:2000]:
            if c.name in (".git", ".DS_Store"):
                continue
            try:
                items.append({"name": c.name, "dir": c.is_dir(), "size": 0 if c.is_dir() else c.stat().st_size,
                              "path": c.relative_to(self.cwd).as_posix()})
            except OSError:
                pass
        return items

    def read_for_view(self, rel: str) -> dict:
        p = self.path(rel)
        self._inside(p)
        if not p.is_file():
            raise ToolError(f"No such file: {rel}")
        raw = p.read_bytes()[:400_000]
        if b"\0" in raw[:8000]:
            return {"binary": True, "size": p.stat().st_size}
        st = p.stat()
        return {"text": raw.decode(errors="replace"), "truncated": st.st_size > 400_000, "size": st.st_size, "mtime": str(st.st_mtime_ns)}  # text: too precise for JS numbers

    def write_from_view(self, rel: str, text: str, mtime: int | None) -> dict:
        """Save a file the user edited in the Files tab; refuses if it changed on disk since it was opened."""
        p = self.path(rel)
        self._inside(p)
        if not p.is_file():
            raise ToolError(f"No such file: {rel}")
        if mtime and str(p.stat().st_mtime_ns) != str(mtime):
            raise ToolError("This file changed since you opened it (maybe the agent edited it). Reopen it to see the latest version.")
        if p.stat().st_size > 400_000:
            raise ToolError("This file is too large to edit here.")
        before = p.stat().st_mtime_ns
        p.write_text(text, encoding="utf-8")
        st = p.stat()
        if st.st_mtime_ns <= before:  # coarse clocks (Windows) can keep the old time; a save must always look newer
            os.utime(p, ns=(st.st_atime_ns, before + 1_000_000))
            st = p.stat()
        return {"size": st.st_size, "mtime": str(st.st_mtime_ns)}

    def _inside(self, p: Path):
        try:
            p.resolve().relative_to(self.cwd)
        except ValueError:
            raise ToolError("That path is outside the project folder.")

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
        if p.suffix.lower() == ".ipynb":
            from .extras import render_notebook
            try:
                return render_notebook(p)
            except ValueError as e:
                raise ToolError(f"Not a valid notebook: {e}")
        if p.suffix.lower() == ".pdf":
            return pdf_extract(p.read_bytes())[0] + "\n\n(Tip: read_pdf shows the real pages, including tables and images.)"
        text = decode_text(p.read_bytes())
        if text is None:
            raise ToolError(f"{p.name} is a binary file, not text. (Images: use read_media; Word/Excel/PowerPoint: read_document.)")
        lines = text.splitlines()
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

    def _notebook_edit(self, path: str, cell: int, source: str | None = None, cell_type: str | None = None,
                       mode: str = "replace") -> str:
        from .extras import edit_notebook

        p = self.path(path)
        if not p.is_file():
            raise ToolError(f"No such notebook: {p}")
        try:
            new = edit_notebook(p.read_text(encoding="utf-8"), cell, source, cell_type, mode)
        except (ValueError, KeyError) as e:
            raise ToolError(str(e))
        self._before_change(p)
        p.write_text(new, encoding="utf-8")
        return f"Notebook updated ({mode} cell {cell})."

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

    # ---- code navigation ----
    _DEF = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:public\s+|private\s+|protected\s+|static\s+|async\s+|pub(?:\([^)]*\))?\s+)*"
                      r"(function\*?|class|interface|type|enum|const|let|var|def|fn|func|struct|trait|impl|module|object)\s+"
                      r"(?:\([^)]*\)\s*)?([A-Za-z_$][\w$]*)")
    _CODE_EXT = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".rs", ".java", ".kt", ".swift", ".rb", ".php",
                 ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".scala", ".lua", ".dart", ".vue", ".svelte"}

    def _code_files(self, path: str | None):
        root = self.path(path or ".")
        for f in self._walk(root):
            if f.suffix.lower() in self._CODE_EXT:
                yield f

    def _defs_in(self, f: Path):
        try:
            text = f.read_text(errors="replace")
        except OSError:
            return []
        if f.suffix == ".py":
            import ast
            try:
                tree = ast.parse(text)
            except SyntaxError:
                tree = None
            if tree is not None:
                out = []
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        out.append((node.lineno, "class" if isinstance(node, ast.ClassDef) else "def", node.name))
                    elif isinstance(node, ast.Assign) and node.col_offset == 0:
                        out += [(node.lineno, "var", t.id) for t in node.targets if isinstance(t, ast.Name)]
                return sorted(out)
        return [(i, m.group(1), m.group(2)) for i, line in enumerate(text.splitlines(), 1) if (m := self._DEF.match(line))]

    def _rel(self, f: Path) -> str:
        try:
            return f.relative_to(self.cwd).as_posix()
        except ValueError:
            return str(f)

    def _code_symbols(self, name: str | None = None, path: str | None = None) -> str:
        out = []
        files = [self.path(path)] if path and self.path(path).is_file() else self._code_files(path)
        for f in files:
            for line, kind, sym in self._defs_in(f):
                if not name or sym == name:
                    out.append(f"{self._rel(f)}:{line}  {kind} {sym}")
            if len(out) >= 400:
                break
        if not out:
            return f"No definition of {name} found." if name else "No definitions found."
        return "\n".join(out)

    def _find_references(self, name: str, path: str | None = None) -> str:
        rx = re.compile(rf"(?<![\w$]){re.escape(name)}(?![\w$])")
        out = []
        for f in self._code_files(path):
            defs = {line for line, _, sym in self._defs_in(f) if sym == name}
            try:
                lines = f.read_text(errors="replace").splitlines()
            except OSError:
                continue
            for i, line in enumerate(lines, 1):
                if rx.search(line):
                    out.append(f"{self._rel(f)}:{i}:{' [definition]' if i in defs else ''} {line.strip()[:200]}")
            if len(out) >= 300:
                break
        return "\n".join(out) or f"No uses of {name} found."

    def _code_intel(self, action: str, path: str, line: int | None = None, column: int | None = None,
                    symbol: str | None = None) -> str:
        from .lsp import MANAGER, LspError
        try:
            return MANAGER.run(self.cwd, self.path(path), action, line, column, symbol)
        except LspError as e:
            raise ToolError(str(e))
        except OSError as e:
            raise ToolError(f"The language server could not start: {e}")

    # ---- notebooks ----
    def _notebook_run(self, path: str, upto: int | None = None) -> str:
        import json as _json
        import sys as _sys
        p = self.path(path)
        if not p.is_file():
            raise ToolError(f"No such notebook: {p}")
        py = shutil.which("python3") or shutil.which("python") or (None if getattr(_sys, "frozen", False) else _sys.executable)
        if not py:
            raise ToolError("Python is not installed on this computer, so notebooks can't run.")
        runner = Path(tempfile.gettempdir()) / "herness_nbrun.py"
        runner.write_text(_NB_RUNNER, encoding="utf-8")
        before = p.read_text(encoding="utf-8")
        argv = [py, str(runner), str(p), str(-1 if upto is None else upto)]
        wrapped = self._sandboxed(" ".join(f"'{a}'" for a in argv)) if self.sandbox != "off" else None
        try:
            proc = subprocess.run(wrapped or argv, cwd=p.parent, capture_output=True, text=True, timeout=900,
                                  encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise ToolError("The notebook took longer than 15 minutes and was stopped.")
        try:
            result = _json.loads(proc.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise ToolError(f"The notebook runner failed: {proc.stderr[-2000:]}")
        if self.on_file_change:
            self.on_file_change(p, before)
        p.write_text(result.pop("notebook"), encoding="utf-8")
        lines = [f"[cell {c['cell']}] " + ("ERROR " + c["error"] if c.get("error") else (c.get("output") or "(no output)"))[:1500]
                 for c in result["cells"]]
        return f"Ran {len(result['cells'])} code cell(s){' — stopped at an error' if result.get('failed') else ''}:\n" + "\n".join(lines)

    # ---- web ----
    # ---- your own Chrome (LightCode4 Chrome extension) ----
    def _chrome(self, action: str, args: dict, timeout: float = 60):
        if self.chrome is None:
            raise ToolError("The Chrome extension bridge isn't running.")
        try:
            return self.chrome.call(action, {k: v for k, v in args.items() if v is not None}, timeout)
        except RuntimeError as e:
            raise ToolError(str(e))

    def _chrome_tabs(self) -> str:
        tabs = self._chrome("tabs", {})
        return "\n".join(f"[{t['id']}]{' (active)' if t.get('active') else ''} {t.get('title', '')} — {t.get('url', '')}" for t in tabs) or "(no tabs)"

    def _chrome_read(self, tab: int | None = None) -> str:
        r = self._chrome("read", {"tab": tab})
        return f"{r.get('title', '')}\n{r.get('url', '')}\n\n{r.get('text', '')}"[:60_000]

    def _chrome_screenshot(self):
        r = self._chrome("screenshot", {})
        data = str(r.get("dataUrl", "")).split(",", 1)[-1]
        return [{"type": "text", "text": f"Screenshot of {r.get('url', 'the active tab')}"},
                {"type": "image", "media_type": "image/png", "data": data}]

    def _chrome_open(self, url: str | None = None, new_tab: bool = True, tab: int | None = None) -> str:
        r = self._chrome("open", {"url": url, "new_tab": new_tab, "tab": tab})
        return f"Now at {r.get('url', '')} (tab {r.get('id')})"

    def _chrome_click(self, text: str | None = None, selector: str | None = None, tab: int | None = None) -> str:
        return str(self._chrome("click", {"text": text, "selector": selector, "tab": tab}))

    def _chrome_type(self, text: str, selector: str | None = None, submit: bool = False, tab: int | None = None) -> str:
        return str(self._chrome("type", {"text": text, "selector": selector, "submit": submit, "tab": tab}))

    # ---- GitHub (Settings > GitHub) ----
    def _gh_call(self, method: str, path: str, body=None) -> str:
        from .github import GitHubError, api
        path = str(path or "").strip()
        if path.startswith("http"):
            path = "/" + path.split("://", 1)[1].split("/", 1)[-1]
        try:
            out = api(self.github_token, method, path, body)
        except GitHubError as e:
            raise ToolError(str(e))
        return json.dumps(out, indent=1)[:60_000]

    def _github_get(self, path: str) -> str:
        return self._gh_call("GET", path)

    def _github_api(self, method: str, path: str, body: dict | None = None) -> str:
        m = str(method).upper()
        if m not in ("POST", "PATCH", "PUT", "DELETE"):
            raise ToolError("method must be POST, PATCH, PUT or DELETE (use github_get to read).")
        return self._gh_call(m, path, body)

    # ---- services (Settings > Services) ----
    def _svc(self, key, default=""):
        return (self.services or {}).get(key, default)

    def _read_document(self, path: str) -> str:
        from .services import ServiceError, read_office
        p = self.path(path)
        if not p.is_file():
            raise ToolError(f"No such file: {p}")
        try:
            return read_office(p.read_bytes(), p.name)
        except (ServiceError, KeyError, Exception) as e:
            raise ToolError(f"Could not read {p.name}: {e}")

    def _read_media(self, path: str, frames: int = 6):
        from .services import ServiceError, media_frames_and_audio, transcribe
        p = self.path(path)
        if not p.is_file():
            raise ToolError(f"No such file: {p}")
        try:
            shots, audio, dur = media_frames_and_audio(p, max(1, min(int(frames or 6), 12)))
        except ServiceError as e:
            raise ToolError(str(e))
        text = f"{p.name}: {dur:.1f} seconds, {len(shots)} frame(s)."
        if audio:
            try:
                text += "\n\nTranscript:\n" + (transcribe(audio, p.stem + ".m4a", self._svc("openai_key"), os.environ.get("LC4_BASE_OPENAI", "")) or "(no speech)")
            except ServiceError as e:
                text += f"\n\n(No transcript: {e})"
        parts = [{"type": "text", "text": text}]
        parts += [{"type": "image", "media_type": "image/jpeg", "data": base64.b64encode(b).decode()} for b in shots]
        return parts

    def _geocode(self, query: str) -> str:
        from .services import ServiceError, geocode
        try:
            rows = geocode(query, os.environ.get("LC4_BASE_NOMINATIM", ""))
        except ServiceError as e:
            raise ToolError(str(e))
        return "\n".join(f"{r['name']} — {r['lat']:.6f}, {r['lon']:.6f} ({r['type']})" for r in rows) or "Nothing found."

    def _places_nearby(self, lat: float, lon: float, kind: str, radius: int = 1000) -> str:
        from .services import ServiceError, places_nearby
        try:
            rows = places_nearby(lat, lon, kind, radius, os.environ.get("LC4_BASE_OVERPASS", ""))
        except ServiceError as e:
            raise ToolError(str(e))
        import json as _json
        return _json.dumps(rows, ensure_ascii=False, indent=1) if rows else f"No {kind} found within {radius} m."

    def _generate_image(self, prompt: str, path: str | None = None, size: str = "1024x1024", edit: str | None = None):
        from .services import ServiceError, generate_image
        prov = self._svc("image_provider", "openai")
        key = self._svc("image_key") or (self._svc("openai_key") if prov == "openai" else "")
        src = self.path(edit).read_bytes() if edit and self.path(edit).is_file() else None
        try:
            data = generate_image(prov, key, prompt, size or "1024x1024", src, os.environ.get("LC4_BASE_IMAGES", ""))
        except ServiceError as e:
            raise ToolError(str(e))
        rel = path or f"images/{re.sub(r'[^a-z0-9]+', '-', prompt.lower())[:40].strip('-') or 'image'}.png"
        p = self.path(rel)
        self._before_change(p)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        mt = "image/jpeg" if data[:3] == b"\xff\xd8\xff" else "image/webp" if data[8:12] == b"WEBP" else "image/png"
        return [{"type": "text", "text": f"Saved the image to {p} ({len(data) // 1024} KB)."},
                {"type": "image", "media_type": mt, "data": base64.b64encode(data).decode()}]

    def _upload_file(self, path: str) -> str:
        from .services import ServiceError, s3_upload
        import mimetypes
        p = self.path(path)
        if not p.is_file():
            raise ToolError(f"No such file: {p}")
        try:
            url = s3_upload(self._svc("s3", {}) or {}, p.name, p.read_bytes(), mimetypes.guess_type(p.name)[0] or "application/octet-stream")
        except ServiceError as e:
            raise ToolError(str(e))
        return f"Uploaded {p.name}. Link: {url}"

    def _web_search(self, query: str) -> str:
        from .services import ServiceError, search
        prov = self._svc("search_provider", "duckduckgo")
        if prov != "duckduckgo":
            try:
                rows = search(prov, query, self._svc("search_key"), self._svc("searxng"), os.environ.get("LC4_BASE_SEARCH", ""))
            except ServiceError as e:
                rows = None
                note = f"({prov} search failed: {e}; used the built-in search instead)\n"
            else:
                note = ""
            if rows is not None:
                return "\n".join(f"{i}. {r['title']}\n   {r['url']}\n   {r['snippet']}" for i, r in enumerate(rows, 1)) or "No results."
        else:
            note = ""
        return note + self._web_search_builtin(query)

    def _web_search_builtin(self, query: str) -> str:
        import urllib.parse as up
        url = "https://html.duckduckgo.com/html/?" + up.urlencode({"q": query})
        raw, _ = self._download(url)
        page = raw.decode(errors="replace")
        results = []
        for m in re.finditer(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>(.*?)(?=<a[^>]+class="result__a"|$)', page, re.S):
            href, title, rest = m.groups()
            q = up.parse_qs(up.urlparse(html.unescape(href)).query)
            link = q.get("uddg", [html.unescape(href)])[0]
            sm = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', rest, re.S)
            clean = lambda t: html.unescape(re.sub(r"<[^>]+>", "", t)).strip()
            results.append(f"{len(results) + 1}. {clean(title)}\n   {link}\n   {clean(sm.group(1)) if sm else ''}")
            if len(results) >= 8:
                break
        return "\n".join(results) or "No results (the search service may be busy). Try different words, or fetch a known page."

    def _download(self, url: str) -> tuple[bytes, str]:
        req = urllib.request.Request(url, headers={"User-Agent": "LightCode4/1.0"})
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


def hunks(old: str, new: str) -> list[dict]:
    """The separate changes between two texts, for reviewing them one by one."""
    a, b = old.splitlines(keepends=True), new.splitlines(keepends=True)
    out = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        body = "".join("-" + x if x.endswith("\n") else "-" + x + "\n" for x in a[i1:i2]) + \
               "".join("+" + x if x.endswith("\n") else "+" + x + "\n" for x in b[j1:j2])
        out.append({"i": len(out), "line": i1 + 1, "text": body[:4000]})
    return out


def apply_hunks(old: str, new: str, keep: set) -> str:
    """old with only the chosen changes (by index from hunks()) applied."""
    a, b = old.splitlines(keepends=True), new.splitlines(keepends=True)
    out, n = [], 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            out += a[i1:i2]
            continue
        out += b[j1:j2] if n in keep else a[i1:i2]
        n += 1
    return "".join(out)


_NB_RUNNER = r'''
import contextlib, io, json, sys, traceback
path, upto = sys.argv[1], int(sys.argv[2])
nb = json.load(open(path, encoding="utf-8"))
g = {"__name__": "__main__"}
cells, failed = [], False
for i, c in enumerate(nb.get("cells", [])):
    if upto >= 0 and i > upto:
        break
    if c.get("cell_type") != "code":
        continue
    src = "".join(c.get("source", []))
    out, err = io.StringIO(), None
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = compile(src, f"<cell {i}>", "exec")
            exec(code, g)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    text = out.getvalue()
    c["outputs"] = ([{"output_type": "stream", "name": "stdout", "text": text.splitlines(True)}] if text else []) + (
        [{"output_type": "error", "ename": err.split(":")[0], "evalue": err, "traceback": []}] if err else [])
    c["execution_count"] = len(cells) + 1
    cells.append({"cell": i, "output": text[-2000:], "error": err})
    if err:
        failed = True
        break
print(json.dumps({"cells": cells, "failed": failed, "notebook": json.dumps(nb, indent=1, ensure_ascii=False) + "\n"}))
'''


def project_instructions(cwd: str) -> tuple[str, list[str]]:
    """Text of the folder's instruction files (HERNESS.md, CLAUDE.md, AGENTS.md) and their names."""
    names, parts = [], []
    for name in ("LIGHTCODE.md", "HERNESS.md", "CLAUDE.md", "AGENTS.md", ".herness/instructions.md"):
        p = Path(cwd) / name
        if p.is_file():
            try:
                parts.append(f"## {name}\n{p.read_text(errors='replace')[:20000]}")
                names.append(name)
            except OSError:
                pass
    return "\n\n".join(parts), names
