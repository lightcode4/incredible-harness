"""Marketplace: skills, agents, slash commands and Claude Code plugins from GitHub repositories.

A source is a GitHub repository (owner/repo) or a folder in one (owner/repo/path). It is read in two ways at once:
- a Claude Code plugin marketplace (.claude-plugin/marketplace.json): each plugin can be installed as a whole,
  including plugins that live in other repositories
- every SKILL.md folder, agents/*.md and commands/*.md file, installable one by one
What a source offers is cached on disk and refreshed in the background (paced to GitHub's limits), so the page
opens instantly. Installed files go to ~/.incredible-herness/{skills,agents,commands}; Remove takes away exactly those."""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from .market_sources import DEFAULT_SOURCES

HOME = Path.home() / ".incredible-herness"
STATE = HOME / "market.json"
MAX_FILES, MAX_BYTES, MAX_ITEMS, MAX_DESCRIBE = 2000, 40_000_000, 400, 150
STALE = 12 * 3600


class MarketError(Exception):
    pass


class RateLimited(MarketError):
    pass


def _api_base() -> str:
    return os.environ.get("LC4_MARKET_API", "https://api.github.com")


def _raw_base() -> str:
    return os.environ.get("LC4_MARKET_RAW", "https://raw.githubusercontent.com")


def _cache_dir() -> Path:
    return HOME / "market-cache"


def _get(url: str, token: str = "", raw: bool = False):
    headers = {"User-Agent": "LightCode4", "Accept": "application/vnd.github+json"}
    if token and url.startswith(_api_base()):
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=40) as r:
            data = r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise MarketError("Repository not found (or it's private and GitHub isn't connected).")
        if e.code in (403, 429):
            raise RateLimited("GitHub is limiting requests right now. Connect GitHub in Settings for a much higher limit.")
        raise MarketError(f"GitHub said {e.code}.")
    except urllib.error.URLError as e:
        raise MarketError(f"Could not reach GitHub ({e.reason}).")
    return data if raw else json.loads(data)


def parse_source(src: str) -> tuple[str, str]:
    """'owner/repo', 'owner/repo/sub/path' or a github.com URL (also /tree/<branch>/path) -> (owner/repo, sub/path)."""
    s = src.strip().strip("\\").removeprefix("https://").removeprefix("http://").removeprefix("github.com/").strip("/")
    s = re.sub(r"\.git$", "", s)
    parts = s.split("/")
    if len(parts) < 2 or not all(re.fullmatch(r"[\w.-]+", p) for p in parts[:2]):
        raise MarketError("Use the form owner/repo (or paste the GitHub link).")
    rest = parts[2:]
    if rest[:1] in (["tree"], ["blob"]):
        rest = rest[2:]  # drop tree/<branch>
    return "/".join(parts[:2]), "/".join(rest)


def normalize(src: str) -> str:
    repo, sub = parse_source(src)
    return f"{repo}/{sub}" if sub else repo


def _front(text: str) -> tuple[dict, str]:
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?", text, re.S)
    meta = {}
    if m:
        for line in m.group(1).splitlines():
            if ":" in line and not line.startswith((" ", "\t")):
                k, v = line.split(":", 1)
                meta[k.strip().lower()] = v.strip().strip("\"'")
    return meta, (text[m.end():] if m else text)


def _safe(name: str) -> str:
    return re.sub(r"[^\w.-]+", "-", name).strip(".-")[:80] or "item"


# ---------------------------------------------------------------- sources
def load_state() -> dict:
    try:
        st = json.loads(STATE.read_text())
    except Exception:
        st = {}
    st.setdefault("sources", [])
    st.setdefault("removed", [])
    st.setdefault("installed", {})
    for d in DEFAULT_SOURCES:  # new built-in marketplaces appear by themselves, unless you removed them
        if d not in st["sources"] and d not in st["removed"]:
            st["sources"].append(d)
    return st


def save_state(st: dict) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, indent=1))


def add_source(src: str, token: str = "") -> list[str]:
    src = normalize(src)
    browse(src, token, refresh=True)  # make sure it can be read before saving it
    st = load_state()
    if src not in st["sources"]:
        st["sources"].insert(0, src)
    st["removed"] = [r for r in st["removed"] if r != src]
    save_state(st)
    return st["sources"]


def remove_source(src: str) -> list[str]:
    st = load_state()
    st["sources"] = [s for s in st["sources"] if s != src]
    if src not in st["removed"]:
        st["removed"].append(src)
    save_state(st)
    return st["sources"]


# ---------------------------------------------------------------- reading a source
_trees: dict = {}


def _tree(repo: str, token: str) -> list[str]:
    hit = _trees.get(repo)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    t = _get(f"{_api_base()}/repos/{repo}/git/trees/HEAD?recursive=1", token)
    files = [x["path"] for x in t.get("tree", []) if x.get("type") == "blob"]
    _trees[repo] = (time.time(), files)
    return files


def _raw(repo: str, path: str, token: str = "") -> bytes:
    return _get(f"{_raw_base()}/{repo}/HEAD/{path}", token, raw=True)


def _describe(repo: str, path: str, token: str) -> dict:
    try:
        meta, body = _front(_raw(repo, path, token).decode("utf-8", "replace"))
    except MarketError:
        return {"name": "", "description": ""}
    first = next((ln.strip("# ").strip() for ln in body.splitlines() if ln.strip()), "")
    return {"name": meta.get("name", ""), "description": (meta.get("description") or first)[:240]}


def _marketplace(repo: str, files: list[str], token: str) -> list[dict]:
    if ".claude-plugin/marketplace.json" not in files:
        return []
    try:
        return json.loads(_raw(repo, ".claude-plugin/marketplace.json", token)).get("plugins", []) or []
    except (MarketError, ValueError):
        return []


def _plugin_home(repo: str, p: dict) -> tuple[str, str] | None:
    """Where a marketplace plugin's files live: (owner/repo, folder). None if not on GitHub."""
    src = p.get("source")
    if isinstance(src, str):
        return repo, src.strip().removeprefix("./").strip("/")
    if isinstance(src, dict):
        if src.get("source") == "github" and src.get("repo"):
            return src["repo"], (src.get("path") or "").strip("/")
        url = src.get("url") or ""
        m = re.match(r"^https://github\.com/([\w.-]+/[\w.-]+?)(?:\.git)?/?$", url)
        if m:
            return m.group(1), (src.get("path") or "").strip("/")
    return None


def scan(src: str, token: str = "") -> list[dict]:
    """Read what a source offers, straight from GitHub."""
    repo, sub = parse_source(src)
    files = _tree(repo, token)
    scope = [f for f in files if not sub or f.startswith(sub + "/")]
    items = []
    if not sub:
        for p in _marketplace(repo, files, token):
            if _plugin_home(repo, p) is None:
                continue
            items.append({"id": f"plugin:{p.get('name')}", "kind": "plugin", "name": p.get("name", "plugin"),
                          "description": (p.get("description") or "")[:240]})
    elif f"{sub}/.claude-plugin/plugin.json" in files:
        try:
            pj = json.loads(_raw(repo, f"{sub}/.claude-plugin/plugin.json", token))
        except (MarketError, ValueError):
            pj = {}
        items.append({"id": f"plugin:@{sub}", "kind": "plugin", "name": pj.get("name") or sub.rsplit("/", 1)[-1],
                      "description": (pj.get("description") or "")[:240]})
    found = []
    for f in scope:
        if len(items) + len(found) >= MAX_ITEMS:
            break
        if f.endswith("/SKILL.md") or f == "SKILL.md":
            found.append(("skill", (f.rsplit("/", 1)[0] if "/" in f else ""), f))
        elif re.search(r"(^|/)agents/[^/]+\.md$", f) and not f.lower().endswith("readme.md"):
            found.append(("agent", f, f))
        elif re.search(r"(^|/)commands/[^/]+\.md$", f) and not f.lower().endswith("readme.md"):
            found.append(("command", f, f))
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(8) as pool:  # descriptions read in parallel
        descs = list(pool.map(lambda x: _describe(repo, x[2], token), found[:MAX_DESCRIBE]))
    descs += [{"name": "", "description": ""}] * (len(found) - len(descs))
    for (kind, ref, f), d in zip(found, descs):
        fallback = (ref.rsplit("/", 1)[-1] or repo.split("/")[1]) if kind == "skill" else Path(f).stem
        items.append({"id": f"{kind}:{ref}", "kind": kind, "name": d["name"] or fallback, "description": d["description"]})
    return items


def _cache_file(src: str) -> Path:
    return _cache_dir() / (_safe(src.replace("/", "__")) + ".json")


def cached(src: str) -> dict | None:
    try:
        return json.loads(_cache_file(src).read_text())
    except Exception:
        return None


def browse(src: str, token: str = "", refresh: bool = False) -> list[dict]:
    src = normalize(src)
    c = None if refresh else cached(src)
    if c is None:
        failed = None
        try:
            c = {"t": time.time(), "items": scan(src, token), "error": ""}
        except RateLimited:
            raise
        except MarketError as e:  # remembered with its error, so it isn't retried until it is due again
            c, failed = {"t": time.time(), "items": [], "error": str(e)}, e
        _cache_dir().mkdir(parents=True, exist_ok=True)
        _cache_file(src).write_text(json.dumps(c))
        if failed and refresh:
            raise failed
    installed = load_state()["installed"]
    return [{**it, "source": src, "installed": f"{src}|{it['id']}" in installed} for it in c["items"]]


def overview() -> list[dict]:
    st = load_state()
    out = []
    for s in st["sources"]:
        c = cached(s)
        out.append({"repo": s, "count": len(c["items"]) if c else None, "t": c["t"] if c else 0, "error": (c or {}).get("error", "")})
    return out


def all_items() -> list[dict]:
    installed = load_state()["installed"]
    out = []
    for s in load_state()["sources"]:
        c = cached(s)
        for it in (c or {}).get("items", []):
            out.append({**it, "source": s, "installed": f"{s}|{it['id']}" in installed})
    return out


def refresh_next(token: str = "") -> str | None:
    """Refresh the source that was checked longest ago (if older than 12 hours). Returns it, or None if all are fresh."""
    st = load_state()
    ages = [(cached(s) or {}).get("t", 0) for s in st["sources"]]
    if not ages:
        return None
    i = min(range(len(ages)), key=lambda k: ages[k])
    if time.time() - ages[i] < STALE:
        return None
    _refresh(st["sources"][i], token)
    return st["sources"][i]


def _refresh(src: str, token: str) -> None:
    try:
        browse(src, token, refresh=True)
    except RateLimited:
        raise
    except MarketError:
        pass  # kept with its error; tried again later


# ---------------------------------------------------------------- installing
def _download(repo: str, paths: list[str], token: str, dest_for) -> list[str]:
    """Fetch files in parallel (skills like Anthropic's Word/Excel skills hold hundreds of small files)."""
    jobs = [(p, d) for p in paths[:MAX_FILES] if (d := dest_for(p)) is not None]
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(12) as pool:
        blobs = list(pool.map(lambda j: _raw(repo, j[0], token), jobs))
    if sum(len(b) for b in blobs) > MAX_BYTES:
        raise MarketError("That item is too large to install (over 40 MB).")
    written = []
    for (p, dest), data in zip(jobs, blobs):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        written.append(str(dest))
    return written


def _inside(base: Path, rel: str) -> Path | None:
    dest = (base / rel).resolve()
    return dest if str(dest).startswith(str(base.resolve()) + os.sep) or dest == base.resolve() else None


def _roots_in(files: list[str], base: str, p: dict | None = None) -> list[tuple[str, str]]:
    """(kind, path) for the skills, agents and commands of a plugin folder."""
    pre = (base + "/") if base else ""
    roots = []
    lists = {k: (p or {}).get(k) for k in ("skills", "agents", "commands")}
    if any(isinstance(v, list) for v in lists.values()):
        for k, v in lists.items():
            for rel in v if isinstance(v, list) else []:
                path = (pre + str(rel).removeprefix("./")).strip("/")
                roots.append((k[:-1], path))
        return roots
    for f in files:
        if not f.startswith(pre):
            continue
        rel = f[len(pre):]
        if rel.endswith("SKILL.md") and rel.startswith("skills/"):
            roots.append(("skill", f.rsplit("/", 1)[0]))
        elif re.match(r"^agents/[^/]+\.md$", rel):
            roots.append(("agent", f))
        elif re.match(r"^commands/[^/]+\.md$", rel):
            roots.append(("command", f))
    return roots


def install(src: str, item_id: str, token: str = "") -> dict:
    src = normalize(src)
    repo, sub = parse_source(src)
    kind, _, ref = item_id.partition(":")
    home_repo, files = repo, _tree(repo, token)
    if kind == "plugin":
        if ref.startswith("@"):
            roots = _roots_in(files, ref[1:])
        else:
            p = next((x for x in _marketplace(repo, files, token) if x.get("name") == ref), None)
            where = _plugin_home(repo, p) if p else None
            if not where:
                raise MarketError("That plugin isn't in this marketplace any more.")
            home_repo, base = where
            if home_repo != repo:
                files = _tree(home_repo, token)
            roots = _roots_in(files, base, p)
        if not roots:
            raise MarketError("That plugin has no skills, agents or commands LightCode4 can use "
                              "(its hooks or MCP servers can be added in Settings > Connectors).")
    else:
        roots = [(kind, ref)]
    written = []
    for k, root in roots:
        if k == "skill":
            target = HOME / "skills" / _safe(root.rsplit("/", 1)[-1] or repo.split("/")[1])
            pre = (root + "/") if root else ""
            mine = [f for f in files if f.startswith(pre)] if root else [f for f in files if "/" not in f]
            written += _download(home_repo, mine, token, lambda f, t=target, pre=pre: _inside(t, f[len(pre):]))
        elif k in ("agent", "command"):
            folder = HOME / ("agents" if k == "agent" else "commands")
            written += _download(home_repo, [root], token, lambda f, folder=folder: _inside(folder, _safe(Path(f).stem) + ".md"))
        else:
            raise MarketError("Unknown item.")
    st = load_state()
    st["installed"][f"{src}|{item_id}"] = written
    save_state(st)
    return {"installed": len(written)}


def uninstall(src: str, item_id: str) -> None:
    src = normalize(src)
    st = load_state()
    for f in st["installed"].pop(f"{src}|{item_id}", []):
        p = Path(f)
        if p.is_file() and str(p.resolve()).startswith(str(HOME.resolve())):
            p.unlink()
            parent = p.parent
            while parent != HOME and parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent
    save_state(st)
