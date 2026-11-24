"""Marketplace: browse and install skills, agents, slash commands and Claude Code plugins from GitHub repositories.

A source is any GitHub repository (owner/repo). It is read in one of two ways:
- a Claude Code plugin marketplace (.claude-plugin/marketplace.json listing plugins, each with skills/, agents/, commands/)
- any repository with SKILL.md folders, agents/*.md or commands/*.md files
Installed files go to ~/.incredible-herness/{skills,agents,commands}; what came from where is remembered so Remove
takes away exactly those files."""
from __future__ import annotations

import json
import os
import re
import shutil
import urllib.error
import urllib.request
from pathlib import Path

HOME = Path.home() / ".incredible-herness"
STATE = HOME / "market.json"
DEFAULT_SOURCES = ["anthropics/skills"]
MAX_FILES, MAX_BYTES = 300, 8_000_000


class MarketError(Exception):
    pass


def _api_base() -> str:
    return os.environ.get("LC4_MARKET_API", "https://api.github.com")


def _raw_base() -> str:
    return os.environ.get("LC4_MARKET_RAW", "https://raw.githubusercontent.com")


def _get(url: str, token: str = "", raw: bool = False):
    headers = {"User-Agent": "LightCode4", "Accept": "application/vnd.github+json"}
    if token and url.startswith(_api_base()):
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
            data = r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise MarketError("Repository not found (or it's private and GitHub isn't connected).")
        if e.code == 403:
            raise MarketError("GitHub is limiting requests right now. Connect GitHub in Settings for a higher limit, or try later.")
        raise MarketError(f"GitHub said {e.code}.")
    except urllib.error.URLError as e:
        raise MarketError(f"Could not reach GitHub ({e.reason}).")
    return data if raw else json.loads(data)


def _check_repo(repo: str) -> str:
    repo = repo.strip().removeprefix("https://github.com/").strip("/")
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        raise MarketError("Use the form owner/repo, e.g. anthropics/skills.")
    return repo


def _front(text: str) -> tuple[dict, str]:
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?", text, re.S)
    meta = {}
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip().lower()] = v.strip().strip("\"'")
    return meta, (text[m.end():] if m else text)


def _safe(name: str) -> str:
    return re.sub(r"[^\w.-]+", "-", name).strip(".-")[:80] or "item"


def load_state() -> dict:
    try:
        st = json.loads(STATE.read_text())
    except Exception:
        st = {}
    st.setdefault("sources", list(DEFAULT_SOURCES))
    st.setdefault("installed", {})
    return st


def save_state(st: dict) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, indent=1))


def add_source(repo: str, token: str = "") -> list[str]:
    repo = _check_repo(repo)
    browse(repo, token)  # make sure it can be read before saving it
    st = load_state()
    if repo not in st["sources"]:
        st["sources"].append(repo)
    save_state(st)
    return st["sources"]


def remove_source(repo: str) -> list[str]:
    st = load_state()
    st["sources"] = [s for s in st["sources"] if s != repo]
    save_state(st)
    return st["sources"]


def _tree(repo: str, token: str) -> list[str]:
    t = _get(f"{_api_base()}/repos/{repo}/git/trees/HEAD?recursive=1", token)
    return [x["path"] for x in t.get("tree", []) if x.get("type") == "blob"]


def _describe(repo: str, path: str, token: str) -> dict:
    try:
        meta, body = _front(_get(f"{_raw_base()}/{repo}/HEAD/{path}", token, raw=True).decode("utf-8", "replace"))
    except MarketError:
        meta, body = {}, ""
    first = next((ln.strip("# ").strip() for ln in body.splitlines() if ln.strip()), "")
    return {"name": meta.get("name", ""), "description": (meta.get("description") or first)[:240]}


def browse(repo: str, token: str = "") -> list[dict]:
    """Items a source offers: {id, kind (skill|agent|command|plugin), name, description, installed}."""
    repo = _check_repo(repo)
    files = _tree(repo, token)
    installed = load_state()["installed"]
    items = []
    if ".claude-plugin/marketplace.json" in files:
        mk = json.loads(_get(f"{_raw_base()}/{repo}/HEAD/.claude-plugin/marketplace.json", token, raw=True))
        for p in mk.get("plugins", []):
            src = p.get("source")
            if not isinstance(src, str):
                continue  # plugins that live in another repository are skipped
            base = src.strip("./").strip("/")
            items.append({"id": f"plugin:{base}", "kind": "plugin", "name": p.get("name", base), "description": (p.get("description") or "")[:240]})
    else:
        for f in files:
            if f.endswith("/SKILL.md") or f == "SKILL.md":
                folder = f.rsplit("/", 1)[0] if "/" in f else ""
                d = _describe(repo, f, token)
                items.append({"id": f"skill:{folder}", "kind": "skill", "name": d["name"] or folder.rsplit("/", 1)[-1] or repo.split("/")[1],
                              "description": d["description"]})
            elif re.match(r"^(?:.*/)?agents/[^/]+\.md$", f) or re.match(r"^(?:.*/)?commands/[^/]+\.md$", f):
                kind = "agent" if "/agents/" in "/" + f else "command"
                d = _describe(repo, f, token)
                items.append({"id": f"{kind}:{f}", "kind": kind, "name": d["name"] or Path(f).stem, "description": d["description"]})
            if len(items) >= 200:
                break
    for it in items:
        it["installed"] = f"{repo}|{it['id']}" in installed
    return items


def _download(repo: str, paths: list[str], token: str, dest_for) -> list[str]:
    written, total = [], 0
    for p in paths[:MAX_FILES]:
        data = _get(f"{_raw_base()}/{repo}/HEAD/{p}", token, raw=True)
        total += len(data)
        if total > MAX_BYTES:
            raise MarketError("That item is too large to install (over 8 MB).")
        dest = dest_for(p)
        if dest is None:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        written.append(str(dest))
    return written


def install(repo: str, item_id: str, token: str = "") -> dict:
    repo = _check_repo(repo)
    kind, _, path = item_id.partition(":")
    files = _tree(repo, token)
    roots = []  # (kind, repo path) pairs to install
    if kind == "plugin":
        pre = path.rstrip("/") + "/"
        mine = [f for f in files if f.startswith(pre)]
        for f in mine:
            rel = f[len(pre):]
            if rel.endswith("SKILL.md") and rel.startswith("skills/"):
                roots.append(("skill", f.rsplit("/", 1)[0]))
            elif re.match(r"^agents/[^/]+\.md$", rel):
                roots.append(("agent", f))
            elif re.match(r"^commands/[^/]+\.md$", rel):
                roots.append(("command", f))
        if not roots:
            raise MarketError("That plugin has no skills, agents or commands LightCode4 can use (hooks and MCP servers are added in Settings).")
    else:
        roots.append((kind, path))
    written = []
    for k, root in roots:
        if k == "skill":
            name = _safe(root.rsplit("/", 1)[-1] or repo.split("/")[1])
            target = HOME / "skills" / name
            pre = (root + "/") if root else ""
            mine = [f for f in files if f.startswith(pre)] if root else [f for f in files if "/" not in f]
            written += _download(repo, mine, token, lambda f, t=target, pre=pre: _inside(t, f[len(pre):]))
        elif k in ("agent", "command"):
            folder = HOME / ("agents" if k == "agent" else "commands")
            written += _download(repo, [root], token, lambda f, folder=folder: _inside(folder, _safe(Path(f).stem) + ".md"))
        else:
            raise MarketError("Unknown item.")
    st = load_state()
    st["installed"][f"{repo}|{item_id}"] = written
    save_state(st)
    return {"installed": len(written)}


def _inside(base: Path, rel: str) -> Path | None:
    dest = (base / rel).resolve()
    return dest if str(dest).startswith(str(base.resolve())) else None  # never write outside the target folder


def uninstall(repo: str, item_id: str) -> None:
    st = load_state()
    for f in st["installed"].pop(f"{repo}|{item_id}", []):
        p = Path(f)
        if p.is_file() and str(p.resolve()).startswith(str(HOME.resolve())):
            p.unlink()
            parent = p.parent
            while parent != HOME and parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent
    save_state(st)
    shutil.rmtree(HOME / "market-cache", ignore_errors=True)
