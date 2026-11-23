"""Smaller features: code diagnostics after edits, hooks, memory, slash commands, notebooks, export, notifications."""

from __future__ import annotations

import json
import os
import py_compile
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HOME = Path.home() / ".incredible-herness"
MEMORY_FILE = HOME / "memory.md"
_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ---------------------------------------------------------------- diagnostics after edits
def _run(cmd: list[str], cwd: str, timeout: int = 60) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, creationflags=_NOWIN)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 0, f"(check skipped: {e})"


def diagnose(path: Path, cwd: str) -> str | None:
    """Quick checks for the file just changed. Returns problems as text, or None when clean."""
    ext = path.suffix.lower()
    problems = []
    if ext == ".py":
        try:
            py_compile.compile(str(path), doraise=True, cfile=os.path.join(tempfile.gettempdir(), "herness_check.pyc"))
        except py_compile.PyCompileError as e:
            problems.append(str(e.msg).strip())
        if not problems and shutil.which("ruff"):
            code, out = _run(["ruff", "check", "--select", "F,E9", "--output-format", "concise", str(path)], cwd)
            if code:
                problems.append(out)
    elif ext in (".js", ".mjs", ".cjs") and shutil.which("node"):
        code, out = _run(["node", "--check", str(path)], cwd)
        if code:
            problems.append(out)
    elif ext in (".ts", ".tsx") and (Path(cwd) / "node_modules" / ".bin").is_dir():
        tsc = Path(cwd) / "node_modules" / ".bin" / ("tsc.cmd" if os.name == "nt" else "tsc")
        if tsc.exists():
            code, out = _run([str(tsc), "--noEmit", "--pretty", "false"], cwd, timeout=120)
            rel = path.name
            lines = [l for l in out.splitlines() if rel in l][:20]
            if code and lines:
                problems.append("\n".join(lines))
    elif ext in (".json", ".ipynb"):
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as e:
            problems.append(f"Invalid JSON: {e}")
    elif ext in (".yml", ".yaml"):
        try:
            import yaml  # optional
            yaml.safe_load(path.read_text(encoding="utf-8"))
        except ImportError:
            pass
        except Exception as e:
            problems.append(f"Invalid YAML: {e}")
    return "\n".join(problems)[:4000] if problems else None


# ---------------------------------------------------------------- hooks
def run_hook(cmd: str, cwd: str, env_extra: dict | None = None, timeout: int = 300) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, env={**os.environ, **(env_extra or {})}, stdin=subprocess.DEVNULL,
                           creationflags=_NOWIN)
        return p.returncode, (p.stdout + ("\n" + p.stderr if p.stderr else "")).strip()[-6000:]
    except subprocess.TimeoutExpired:
        return 1, f"Timed out after {timeout}s"


# ---------------------------------------------------------------- memory
def read_memory() -> str:
    try:
        return MEMORY_FILE.read_text(encoding="utf-8")[:12000]
    except OSError:
        return ""


def write_memory(text: str) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    MEMORY_FILE.write_text(text, encoding="utf-8")


def remember(note: str) -> str:
    note = " ".join(note.split())[:500]
    current = read_memory()
    if note.lower() in current.lower():
        return "Already remembered."
    write_memory((current.rstrip() + "\n" if current.strip() else "# Things to remember\n") + f"- {note}\n")
    return "Saved to memory."


# ---------------------------------------------------------------- slash commands
BUILTIN_COMMANDS = {
    "review": ("Review uncommitted changes for bugs and security issues",
               "Review my code changes. Run `git status` and `git diff HEAD` (if there are no changes, review the last "
               "commit with `git show`). Look for bugs, security problems, missed edge cases and broken behaviour, and "
               "read surrounding code where needed to confirm each issue. Do not change any files. Report findings as a "
               "numbered list, most severe first, each with: file:line, what is wrong, a concrete scenario where it "
               "fails, and a suggested fix. If you find nothing important, say so plainly. $ARGUMENTS"),
    "init": ("Study this project and write HERNESS.md",
             "Explore this project: list its files, read the README and the key config, entry and test files. Then "
             "write HERNESS.md in the project root (update it if it exists) with: what the project is, how to install, "
             "run, test and build it (exact commands), the folder structure, and important conventions. Keep it under "
             "80 lines and specific to this project. $ARGUMENTS"),
    "test": ("Run the tests and fix failures", "Find how this project runs its tests, run them, and fix any failures "
             "you find. Re-run until they pass, then summarize what was wrong. $ARGUMENTS"),
    "explain": ("Explain how this project or a file works", "Explain how this works, clearly and concisely, with the "
                "important files and the flow between them: $ARGUMENTS"),
    "commit": ("Write a commit message and commit", "Look at `git status` and `git diff HEAD`, then commit the changes "
               "with a clear, specific commit message. Do not push. $ARGUMENTS"),
    "fix": ("Fix a bug or error", "Find and fix this problem. Reproduce it first if you can, then verify the fix: $ARGUMENTS"),
}
UI_COMMANDS = {
    "clear": "Start a new chat", "plan": "Toggle plan mode", "model": "Change model or provider",
    "cost": "Show this chat's cost", "export": "Export this chat", "help": "List commands",
    "parallel": "Start a parallel chat in its own worktree", "memory": "Edit what LightCode⁴ remembers",
    "compact": "Summarize the conversation to free up context", "fork": "Copy this chat to try another direction",
}


def _load_dir(d: Path) -> dict:
    out = {}
    if d.is_dir():
        for f in sorted(d.glob("*.md")):
            text = f.read_text(errors="replace")
            desc = ""
            m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
            if m:
                dm = re.search(r"^description:\s*(.+)$", m.group(1), re.M)
                desc = dm.group(1).strip() if dm else ""
                text = text[m.end():]
            out[f.stem.lower()] = (desc or f"Custom command from {f.parent}", text.strip())
    return out


def commands(cwd: str) -> dict:
    """name -> (description, prompt template). Project commands override personal ones, which override built-ins."""
    return {**BUILTIN_COMMANDS, **_load_dir(HOME / "commands"), **_load_dir(Path(cwd) / ".herness" / "commands")}


def skills(cwd: str) -> dict:
    """name -> (description, folder). A skill is a folder with SKILL.md (optional front matter: name, description).
    Project skills (<project>/.herness/skills) override personal ones (~/.incredible-herness/skills)."""
    out = {}
    for base in (HOME / "skills", Path(cwd) / ".herness" / "skills"):
        if not base.is_dir():
            continue
        for d in sorted(base.iterdir()):
            f = d / "SKILL.md"
            if not f.is_file():
                continue
            text = f.read_text(errors="replace")
            name, desc = d.name, ""
            m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
            if m:
                nm = re.search(r"^name:\s*(.+)$", m.group(1), re.M)
                dm = re.search(r"^description:\s*(.+)$", m.group(1), re.M)
                name = nm.group(1).strip().strip("\"'") if nm else name
                desc = dm.group(1).strip().strip("\"'") if dm else ""
            if not desc:
                body = text[m.end():] if m else text
                desc = next((ln.strip("# ").strip() for ln in body.splitlines() if ln.strip()), "")[:200]
            out[name.lower()] = (desc, d)
    return out


def load_skill(cwd: str, name: str) -> str:
    sk = skills(cwd).get((name or "").lower().strip())
    if not sk:
        have = ", ".join(skills(cwd)) or "none"
        raise ValueError(f"No skill named {name}. Available skills: {have}.")
    desc, d = sk
    text = (d / "SKILL.md").read_text(errors="replace")
    files = [p.relative_to(d).as_posix() for p in sorted(d.rglob("*")) if p.is_file() and p.name != "SKILL.md"][:100]
    extra = ("\n\nFiles in this skill (folder " + str(d) + "):\n" + "\n".join("- " + f for f in files)) if files else ""
    return f"Skill folder: {d}\n\n{text}{extra}"


def expand_command(text: str, cwd: str) -> str | None:
    m = re.match(r"^/([\w-]+)\s*(.*)$", text.strip(), re.S)
    if not m:
        return None
    cmd = commands(cwd).get(m.group(1).lower())
    if not cmd:
        return None
    args = m.group(2).strip()
    body = cmd[1]
    return body.replace("$ARGUMENTS", args) if "$ARGUMENTS" in body else (body + ("\n\n" + args if args else ""))


# ---------------------------------------------------------------- notebooks
def render_notebook(path: Path) -> str:
    nb = json.loads(path.read_text(encoding="utf-8"))
    out = [f"Notebook {path.name}: {len(nb.get('cells', []))} cells"]
    for i, c in enumerate(nb.get("cells", [])):
        src = "".join(c.get("source", []))
        out.append(f"\n[cell {i}] {c.get('cell_type')}\n{src}")
        for o in c.get("outputs", [])[:3]:
            t = "".join(o.get("text", [])) or "".join(o.get("data", {}).get("text/plain", []))
            if t:
                out.append("  [output] " + t[:1500])
            elif o.get("output_type") == "error":
                out.append(f"  [error] {o.get('ename')}: {o.get('evalue')}")
    return "\n".join(out)


def edit_notebook(text: str, cell: int, source: str | None, cell_type: str | None, mode: str) -> str:
    nb = json.loads(text)
    cells = nb.setdefault("cells", [])
    lines = (source or "").splitlines(keepends=True)
    if mode == "insert":
        if not 0 <= cell <= len(cells):
            raise ValueError(f"cell must be 0-{len(cells)} for insert")
        new = {"cell_type": cell_type or "code", "metadata": {}, "source": lines}
        if new["cell_type"] == "code":
            new.update(outputs=[], execution_count=None)
        cells.insert(cell, new)
    elif mode == "delete":
        if not 0 <= cell < len(cells):
            raise ValueError(f"No cell {cell}; the notebook has {len(cells)} cells.")
        cells.pop(cell)
    else:
        if not 0 <= cell < len(cells):
            raise ValueError(f"No cell {cell}; the notebook has {len(cells)} cells.")
        cells[cell]["source"] = lines
        if cell_type and cell_type != cells[cell].get("cell_type"):
            cells[cell]["cell_type"] = cell_type
        if cells[cell].get("cell_type") == "code":
            cells[cell]["outputs"], cells[cell]["execution_count"] = [], None
    return json.dumps(nb, indent=1, ensure_ascii=False) + "\n"


# ---------------------------------------------------------------- export
def export_markdown(title: str, transcript: list[dict]) -> str:
    out = [f"# {title}\n"]
    for ev in transcript:
        k = ev["kind"]
        if k == "user":
            out.append(f"\n## You\n\n{ev['text']}\n")
        elif k == "text":
            out.append(f"\n{ev['text']}\n")
        elif k == "tool":
            out.append(f"\n> 🔧 `{ev.get('summary', '')}`" + (f" — {ev['blocked']}" if ev.get("blocked") else ""))
        elif k == "tool_result" and ev.get("error"):
            out.append(f"\n> ⚠️ {ev.get('output', '')[:300]}")
        elif k == "server_tool":
            out.append(f"\n> 🌐 {ev['text']}")
        elif k == "notice":
            out.append(f"\n> {ev['text']}")
    return "\n".join(out).strip() + "\n"


def export_html(title: str, md: str) -> str:
    import html as h

    body = h.escape(md)
    return (f"<!doctype html><meta charset=utf-8><title>{h.escape(title)}</title>"
            "<style>body{font:15px/1.6 -apple-system,Segoe UI,sans-serif;max-width:780px;margin:40px auto;padding:0 20px;"
            "color:#29261b}pre{white-space:pre-wrap;font:inherit}</style>"
            f"<pre>{body}</pre>")


def downloads_dir() -> Path:
    d = Path.home() / "Downloads"
    return d if d.is_dir() else Path.home()


# ---------------------------------------------------------------- notifications
def notify(title: str, message: str) -> None:
    try:
        if sys.platform == "darwin":
            script = f'display notification {json.dumps(message[:200])} with title {json.dumps(title)}'
            subprocess.Popen(["osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif os.name == "nt":
            ps = ("[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime]>$null;"
                  "$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(1);"
                  f"$t.GetElementsByTagName('text')[0].AppendChild($t.CreateTextNode({json.dumps(title + ': ' + message[:180])}))>$null;"
                  "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('LightCode⁴')"
                  ".Show([Windows.UI.Notifications.ToastNotification]::new($t))")
            subprocess.Popen(["powershell", "-NoProfile", "-Command", ps], creationflags=_NOWIN,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
