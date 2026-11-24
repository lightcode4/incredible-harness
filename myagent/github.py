"""GitHub connection (Settings > GitHub): your account, your repositories, one-click clone, and sign-in for git and gh.

The token stays in the settings file. Commands the agent runs get it through environment variables and a small
askpass helper, so `git push`/`git pull` over https and the `gh` command line work without the token ever
appearing in a command or in the chat."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

API = os.environ.get("LC4_GITHUB_API", "https://api.github.com")
HOME = Path.home() / ".incredible-herness"
TOKEN_URL = ("https://github.com/settings/tokens/new?description=LightCode4"
             "&scopes=repo,workflow,read:org,gist")


class GitHubError(Exception):
    pass


def api(token: str, method: str, path: str, body=None, timeout: int = 30):
    if not token:
        raise GitHubError("GitHub isn't connected. Connect it in Settings > GitHub.")
    if not path.startswith("/"):
        path = "/" + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method.upper(), headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "User-Agent": "LightCode4",
        "X-GitHub-Api-Version": "2022-11-28", **({"Content-Type": "application/json"} if data else {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read()).get("message", "")
        except Exception:
            pass
        if e.code == 401:
            raise GitHubError("GitHub rejected the token (it may have expired). Reconnect in Settings > GitHub.")
        raise GitHubError(f"GitHub said {e.code}: {detail or e.reason}")
    except urllib.error.URLError as e:
        raise GitHubError(f"Could not reach GitHub ({e.reason}).")


def whoami(token: str) -> dict:
    u = api(token, "GET", "/user")
    return {"login": u.get("login", ""), "name": u.get("name") or "", "avatar": u.get("avatar_url", ""),
            "url": u.get("html_url", "")}


def repos(token: str) -> list[dict]:
    out = []
    for page in (1, 2, 3):
        batch = api(token, "GET", f"/user/repos?sort=updated&per_page=100&page={page}"
                                  "&affiliation=owner,collaborator,organization_member")
        out += [{"full_name": r["full_name"], "private": r.get("private", False), "description": r.get("description") or "",
                 "updated": r.get("pushed_at") or r.get("updated_at") or "", "url": r.get("html_url", ""),
                 "branch": r.get("default_branch", "main")} for r in batch]
        if len(batch) < 100:
            break
    return out


def askpass_path() -> Path:
    """A tiny helper git calls for the username/password: it answers with the token from the environment."""
    HOME.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        p = HOME / "git-askpass.bat"
        text = '@echo off\r\necho %1 | findstr /i "Username" >nul && (echo x-access-token) || (echo %LC4_GH_TOKEN%)\r\n'
    else:
        p = HOME / "git-askpass.sh"
        text = '#!/bin/sh\ncase "$1" in\n  Username*) echo x-access-token ;;\n  *) echo "$LC4_GH_TOKEN" ;;\nesac\n'
    if not p.is_file() or p.read_text() != text:
        p.write_text(text)
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IRUSR | stat.S_IWUSR)
    return p


def git_env(token: str) -> dict:
    """Environment for commands: git over https and the gh command line sign in with the connected account."""
    if not token:
        return {}
    return {"LC4_GH_TOKEN": token, "GH_TOKEN": token, "GITHUB_TOKEN": token, "GIT_ASKPASS": str(askpass_path()),
            "GIT_TERMINAL_PROMPT": "0"}


def clone(token: str, full_name: str, parent: Path) -> Path:
    """Clone owner/repo into parent/repo (or reuse it if it is already there)."""
    name = full_name.split("/")[-1]
    if not full_name.count("/") == 1 or not all(part.replace("-", "").replace("_", "").replace(".", "").isalnum() for part in full_name.split("/")):
        raise GitHubError("That doesn't look like a repository name (owner/repo).")
    dest = parent / name
    if (dest / ".git").is_dir():
        return dest
    if dest.exists() and any(dest.iterdir()):
        raise GitHubError(f"{dest} already exists and isn't this repository. Move it, then try again.")
    parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, **git_env(token)}
    base = os.environ.get("LC4_GITHUB_WEB", "https://github.com")
    r = subprocess.run(["git", "clone", f"{base}/{full_name}.git", str(dest)], capture_output=True, text=True, env=env,
                       timeout=900, stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        raise GitHubError("Clone failed: " + (r.stderr or r.stdout).strip()[-400:].replace(token, "***"))
    return dest
