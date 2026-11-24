"""Sync through private GitHub repositories, with git.

- Chats: ~/.incredible-herness/chats/*.json <-> <you>/lightcode4-sync (private). Each chat is one file; when the same
  chat changed on two computers, the newer one wins. Deleted chats are remembered so they stay deleted everywhere.
- Team projects: a project's name, instructions and reference files <-> <owner>/lc4-project-<name> (private).
  Teammates are invited as collaborators and join with the repository name. When both sides changed the same file,
  both versions are kept."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from . import github

HOME = Path.home() / ".incredible-herness"
SYNC_REPO = "lightcode4-sync"


class SyncError(Exception):
    pass


def _git(args: list[str], cwd: Path, env: dict, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", *args], cwd=cwd, env={**os.environ, **env}, capture_output=True, text=True, timeout=300,
                       stdin=subprocess.DEVNULL)
    if check and r.returncode != 0:
        token = env.get("LC4_GH_TOKEN") or "\0"
        raise SyncError(f"git {args[0]} failed: " + (r.stderr or r.stdout).strip()[-300:].replace(token, "***"))
    return r


def _web() -> str:
    return os.environ.get("LC4_GITHUB_WEB", "https://github.com")


def ensure_repo(token: str, full: str, description: str) -> None:
    owner, name = full.split("/")
    try:
        github.api(token, "GET", f"/repos/{full}")
    except github.GitHubError:
        github.api(token, "POST", "/user/repos", {"name": name, "private": True, "auto_init": True, "description": description})


def checkout(token: str, user: dict, full: str, where: Path) -> dict:
    """A local copy of the repository, exactly as it is on GitHub now."""
    env = github.git_env(token, user)
    if not (where / ".git").is_dir():
        where.parent.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(where, ignore_errors=True)
        _git(["clone", "-q", f"{_web()}/{full}.git", str(where)], where.parent, env)
    else:
        _git(["fetch", "-q", "origin"], where, env)
        head = _git(["rev-parse", "--abbrev-ref", "origin/HEAD"], where, env, check=False).stdout.strip() or "origin/main"
        if _git(["rev-parse", "--verify", "-q", head], where, env, check=False).returncode == 0:
            _git(["reset", "-q", "--hard", head], where, env)
    return env


def publish(where: Path, env: dict, message: str) -> bool:
    _git(["add", "-A"], where, env)
    if not _git(["status", "--porcelain"], where, env).stdout.strip():
        return False
    _git(["commit", "-q", "-m", message], where, env)
    _git(["push", "-q", "origin", "HEAD"], where, env)
    return True


def _updated(p: Path) -> float:
    try:
        return float(json.loads(p.read_text()).get("updated") or 0)
    except Exception:
        return 0.0


def _json(p: Path, default):
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


# ---------------------------------------------------------------- chats
def sync_chats(token: str, user: dict, chats_dir: Path, busy: set[str] = frozenset()) -> dict:
    """Two-way sync of all chats. Returns {"pulled": [ids], "pushed": n}. Chats that are working right now are left alone."""
    login = (user or {}).get("login")
    if not token or not login:
        raise SyncError("Connect GitHub first (Settings > GitHub).")
    full = f"{login}/{SYNC_REPO}"
    ensure_repo(token, full, "LightCode4 chats synced between your computers (private)")
    repo = HOME / "sync" / "chats"
    env = checkout(token, user, full, repo)
    rchats = repo / "chats"
    rchats.mkdir(exist_ok=True)
    chats_dir.mkdir(parents=True, exist_ok=True)
    deleted = set(_json(repo / "deleted.json", [])) | set(_json(HOME / "deleted-chats.json", []))
    pulled, pushed = [], 0
    for cid in deleted:
        for p in (rchats / f"{cid}.json", chats_dir / f"{cid}.json"):
            if p.exists() and cid not in busy:
                p.unlink()
    ids = {p.stem for p in rchats.glob("*.json")} | {p.stem for p in chats_dir.glob("*.json")}
    for cid in sorted(ids - deleted):
        if cid in busy:
            continue
        local, remote = chats_dir / f"{cid}.json", rchats / f"{cid}.json"
        lu, ru = _updated(local) if local.exists() else -1, _updated(remote) if remote.exists() else -1
        if lu > ru:
            shutil.copyfile(local, remote)
            pushed += 1
        elif ru > lu:
            shutil.copyfile(remote, local)
            os.chmod(local, 0o600)
            pulled.append(cid)
    (repo / "deleted.json").write_text(json.dumps(sorted(deleted)))
    # memory: keep every line either computer remembered
    mem_l, mem_r = HOME / "memory.md", repo / "memory.md"
    lines = []
    for p in (mem_r, mem_l):
        for ln in (p.read_text(errors="replace").splitlines() if p.exists() else []):
            if ln.strip() and ln not in lines:
                lines.append(ln)
    if lines:
        text = "\n".join(lines) + "\n"
        mem_r.write_text(text)
        if not mem_l.exists() or mem_l.read_text(errors="replace") != text:
            mem_l.write_text(text)
    publish(repo, env, f"Sync {time.strftime('%Y-%m-%d %H:%M')}")
    return {"pulled": pulled, "pushed": pushed}


def forget_chat(cid: str) -> None:
    """Remember a deleted chat so sync removes it on your other computers too."""
    p = HOME / "deleted-chats.json"
    ids = _json(p, [])
    if cid not in ids:
        ids.append(cid)
        HOME.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(ids[-5000:]))


# ---------------------------------------------------------------- team projects
def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "project"


def _hash(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def share_project(token: str, user: dict, proj: dict, files_dir: Path, invite: list[str]) -> str:
    login = (user or {}).get("login")
    if not token or not login:
        raise SyncError("Connect GitHub first (Settings > GitHub).")
    full = (proj.get("shared") or {}).get("repo") or f"{login}/lc4-project-{_slug(proj.get('name', 'project'))}"
    ensure_repo(token, full, f"LightCode4 team project: {proj.get('name', '')}")
    for name in invite:
        name = name.strip().lstrip("@")
        if re.fullmatch(r"[A-Za-z0-9-]{1,39}", name):
            github.api(token, "PUT", f"/repos/{full}/collaborators/{name}", {"permission": "push"})
    proj["shared"] = {"repo": full}
    sync_project(token, user, proj, files_dir)
    return full


def join_project(token: str, user: dict, full: str, proj: dict, files_dir: Path) -> dict:
    full = full.strip().removeprefix("https://github.com/").strip("/")
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", full):
        raise SyncError("Use the form owner/repo for the shared project.")
    proj["shared"] = {"repo": full}
    sync_project(token, user, proj, files_dir, joining=True)
    return proj


def sync_project(token: str, user: dict, proj: dict, files_dir: Path, joining: bool = False) -> dict:
    """Two-way sync of a project's name, instructions and files with its shared repository."""
    full = (proj.get("shared") or {}).get("repo")
    if not full:
        raise SyncError("This project isn't shared.")
    repo = HOME / "sync" / "projects" / full.replace("/", "__")
    env = checkout(token, user, full, repo)
    changed = merge_project(repo, proj, files_dir, user, joining)
    publish(repo, env, f"Update {proj.get('name', 'project')} {time.strftime('%Y-%m-%d %H:%M')}")
    return {"changed": changed}


def merge_project(repo: Path, proj: dict, files_dir: Path, user: dict, joining: bool = False) -> list[str]:
    """Three-way merge of a project's files and settings between files_dir and repo (a folder in a checkout).
    The state of the last sync is kept next to files_dir, so we know who changed what."""
    repo.mkdir(parents=True, exist_ok=True)
    rfiles = repo / "files"
    rfiles.mkdir(exist_ok=True)
    files_dir.mkdir(parents=True, exist_ok=True)
    state_p = files_dir.parent / "sync.json"
    last = {} if joining else _json(state_p, {})
    seen = last.get("files", {})
    names = {p.name for p in rfiles.iterdir() if p.is_file()} | {p.name for p in files_dir.iterdir() if p.is_file()} | set(seen)
    changed = []
    for n in sorted(names):
        lp, rp = files_dir / n, rfiles / n
        lh = _hash(lp) if lp.is_file() else None
        rh = _hash(rp) if rp.is_file() else None
        base = seen.get(n)
        if lh == rh:
            continue
        if lh != base and rh == base:  # only changed here
            (shutil.copyfile(lp, rp) if lh else rp.unlink(missing_ok=True))
        elif rh != base and lh == base:  # only changed there
            (shutil.copyfile(rp, lp) if rh else lp.unlink(missing_ok=True))
            changed.append(n)
        else:  # changed on both sides: keep both
            if lh and rh:
                stem, ext = os.path.splitext(n)
                keep = f"{stem} (from {(user or {}).get('login', 'me')}){ext}"
                shutil.copyfile(lp, rfiles / keep)
                shutil.copyfile(rp, lp)
                shutil.copyfile(rfiles / keep, files_dir / keep)
            elif lh:
                shutil.copyfile(lp, rp)
            else:
                shutil.copyfile(rp, lp)
            changed.append(n)
    meta_p = repo / "project.json"
    remote_meta = _json(meta_p, {})
    mine = {k: proj.get(k, "") for k in ("name", "instructions", "color")}
    base_meta = last.get("meta", {})
    if joining or (remote_meta and remote_meta != base_meta and mine == base_meta):
        proj.update({k: v for k, v in remote_meta.items() if k in mine})
    elif mine != remote_meta:
        meta_p.write_text(json.dumps(mine, indent=1))
    meta_now = {k: proj.get(k, "") for k in ("name", "instructions", "color")}
    state_p.write_text(json.dumps({"files": {p.name: _hash(p) for p in files_dir.iterdir() if p.is_file()}, "meta": meta_now,
                                   "t": time.time()}))
    return changed
