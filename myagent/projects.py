"""Projects: a group of chats that share a folder, instructions and reference files ("knowledge").

Files live in ~/.incredible-herness/projects/<id>/files. Their text is given to every chat in the project
(text files and PDFs; other files are listed by name so the agent can open them from that folder).
"""
from __future__ import annotations

import base64
import re
import shutil
from pathlib import Path

HOME = Path.home() / ".incredible-herness"
MAX_FILE = 20_000_000
MAX_CONTEXT = 120_000  # characters of reference text added to the instructions
TEXT_EXT = {".txt", ".md", ".markdown", ".csv", ".json", ".yaml", ".yml", ".toml", ".xml", ".html", ".css", ".js", ".ts",
            ".tsx", ".jsx", ".py", ".go", ".rs", ".java", ".kt", ".swift", ".rb", ".php", ".c", ".h", ".cpp", ".cs", ".sql",
            ".sh", ".ini", ".cfg", ".log", ".rst", ".tex", ".svg"}


def folder(pid: str) -> Path:
    if not re.fullmatch(r"p[0-9a-f]{8}", pid or ""):
        raise ValueError("Unknown project.")
    return HOME / "projects" / pid / "files"


def _safe_name(name: str) -> str:
    name = Path(name or "file").name.strip().replace("\x00", "")
    if not name or name in (".", ".."):
        raise ValueError("That file name is not allowed.")
    return name[:180]


def list_files(pid: str) -> list[dict]:
    d = folder(pid)
    if not d.is_dir():
        return []
    return [{"name": f.name, "size": f.stat().st_size} for f in sorted(d.iterdir()) if f.is_file()]


def add_file(pid: str, name: str, data_b64: str) -> None:
    raw = base64.b64decode(data_b64 or "")
    if len(raw) > MAX_FILE:
        raise ValueError(f"{name} is larger than 20 MB.")
    d = folder(pid)
    d.mkdir(parents=True, exist_ok=True)
    (d / _safe_name(name)).write_bytes(raw)


def remove_file(pid: str, name: str) -> None:
    (folder(pid) / _safe_name(name)).unlink(missing_ok=True)


def delete_files(pid: str) -> None:
    try:
        shutil.rmtree(folder(pid).parent, ignore_errors=True)
    except ValueError:
        pass


def _text_of(f: Path) -> str | None:
    if f.suffix.lower() in TEXT_EXT:
        return f.read_text(errors="replace")
    if f.suffix.lower() == ".pdf":
        try:
            from pypdf import PdfReader
            return "\n".join((p.extract_text() or "") for p in PdfReader(str(f)).pages[:200])
        except Exception:
            return None
    return None


def context(proj: dict) -> str:
    """Instructions text for chats in this project."""
    parts = [f"# Project: {proj.get('name', 'Untitled')}"]
    if proj.get("instructions", "").strip():
        parts.append("Project instructions from the user (follow them):\n" + proj["instructions"].strip())
    try:
        files = list_files(proj["id"])
    except ValueError:
        files = []
    if files:
        d = folder(proj["id"])
        budget, listed, shown = MAX_CONTEXT, [], []
        for f in files:
            p = d / f["name"]
            text = _text_of(p)
            if text is not None and budget > 0:
                chunk = text[:budget]
                budget -= len(chunk)
                shown.append(f"## {f['name']}\n{chunk}" + ("\n[… truncated]" if len(chunk) < len(text) else ""))
            else:
                listed.append(f"- {p}")
        parts.append(f"Project reference files (also on disk in {d}):")
        if shown:
            parts.append("\n\n".join(shown))
        if listed:
            parts.append("Other project files (open them with read_file / read_pdf when useful):\n" + "\n".join(listed))
    return "\n\n".join(parts)
