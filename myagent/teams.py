"""Teams: several people working on the same projects, through one private GitHub repository per team.

<owner>/lc4-team-<name> holds:
  team.json                    name, roles {login: "owner" | "member"}
  members/<login>.json         each member's presence: name, avatar, last active, what they're working on
  projects/<id>/project.json   team projects (name, instructions) ...
  projects/<id>/files/          ... and their reference files
  chats/<id>.json              chats a member shared with the team (only the author writes their own)
  activity/<login>.jsonl       each member's activity lines (one file each, so pushes never clash)
Invitations are GitHub collaborator invitations; the app lists them and accepts them for you."""
from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path

from . import github, sync

PREFIX = "lc4-team-"


class TeamError(Exception):
    pass


def _home() -> Path:
    return sync.HOME / "teams"


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "team"


def _j(p: Path, default):
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def _need(token: str, user: dict) -> str:
    login = (user or {}).get("login")
    if not token or not login:
        raise TeamError("Teams use GitHub. Connect it in Settings > GitHub first.")
    return login


def _api(token, method, path, body=None):
    try:
        return github.api(token, method, path, body)
    except github.GitHubError as e:
        raise TeamError(str(e))


# ---------------------------------------------------------------- creating, inviting, joining
def create(token: str, user: dict, name: str) -> str:
    login = _need(token, user)
    full = f"{login}/{PREFIX}{_slug(name)}"
    sync.ensure_repo(token, full, f"LightCode4 team: {name}")
    where, env = _checkout(token, user, full)
    (where / "team.json").write_text(json.dumps({"name": name.strip() or "Team", "roles": {login: "owner"}, "created": time.time()}, indent=1))
    _log(where, login, f"created the team {name}")
    _presence(where, user, "")
    sync.publish(where, env, f"Create team {name}")
    return full


def invite(token: str, user: dict, full: str, who: str, role: str = "member") -> None:
    who = who.strip().lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9-]{1,39}", who):
        raise TeamError("Use a GitHub username, e.g. ada-lovelace.")
    login = _need(token, user)
    where, env = _checkout(token, user, full)
    team = _j(where / "team.json", {})
    if team.get("roles", {}).get(login) != "owner":
        raise TeamError("Only a team owner can invite people.")
    _api(token, "PUT", f"/repos/{full}/collaborators/{who}", {"permission": "push"})
    team.setdefault("roles", {})[who] = "owner" if role == "owner" else "member"
    (where / "team.json").write_text(json.dumps(team, indent=1))
    _log(where, login, f"invited @{who} as {team['roles'][who]}")
    sync.publish(where, env, f"Invite {who}")


def remove(token: str, user: dict, full: str, who: str) -> None:
    login = _need(token, user)
    where, env = _checkout(token, user, full)
    team = _j(where / "team.json", {})
    if team.get("roles", {}).get(login) != "owner" and who != login:
        raise TeamError("Only a team owner can remove people.")
    if who == full.split("/")[0]:
        raise TeamError("The person who created the team can't be removed.")
    try:
        _api(token, "DELETE", f"/repos/{full}/collaborators/{who}")
    except TeamError:
        pass
    team.get("roles", {}).pop(who, None)
    (where / "team.json").write_text(json.dumps(team, indent=1))
    (where / "members" / f"{who}.json").unlink(missing_ok=True)
    _log(where, login, f"removed @{who}" if who != login else "left the team")
    sync.publish(where, env, f"Remove {who}")


def invitations(token: str) -> list[dict]:
    """Team invitations waiting for you (GitHub repository invitations for lc4-team-* repositories)."""
    out = []
    for inv in _api(token, "GET", "/user/repository_invitations") or []:
        repo = inv.get("repository") or {}
        if str(repo.get("name", "")).startswith(PREFIX):
            out.append({"id": inv.get("id"), "repo": repo.get("full_name"), "from": (inv.get("inviter") or {}).get("login", ""),
                        "name": repo.get("name", "")[len(PREFIX):].replace("-", " ").title()})
    return out


def accept(token: str, user: dict, inv_id: int, full: str) -> str:
    _api(token, "PATCH", f"/user/repository_invitations/{int(inv_id)}")
    where, env = _checkout(token, user, full)
    _log(where, _need(token, user), "joined the team")
    _presence(where, user, "")
    sync.publish(where, env, "Join")
    return full


# ---------------------------------------------------------------- reading and syncing a team
def _checkout(token: str, user: dict, full: str) -> tuple[Path, dict]:
    where = _home() / full.replace("/", "__")
    try:
        env = sync.checkout(token, user, full, where)
    except sync.SyncError as e:
        raise TeamError(str(e))
    return where, env


def _log(where: Path, login: str, text: str) -> None:
    d = where / "activity"
    d.mkdir(exist_ok=True)
    with open(d / f"{login}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"t": time.time(), "who": login, "text": text}) + "\n")


def _presence(where: Path, user: dict, working_on: str) -> None:
    d = where / "members"
    d.mkdir(exist_ok=True)
    (d / f"{user['login']}.json").write_text(json.dumps({"login": user["login"], "name": user.get("name") or user["login"],
                                                         "avatar": user.get("avatar", ""), "t": time.time(), "working_on": working_on[:120]}))


def sync_team(token: str, user: dict, full: str, team_projects: list[dict], folder_for, shared_chats: dict,
              chats_dir: Path, working_on: str = "") -> dict:
    """One round with the team: my presence, my shared chats and the team projects go up; everyone else's come down.
    team_projects: local projects of this team (dicts, updated in place). folder_for(pid) -> local files folder.
    shared_chats: {chat_id: title} of my chats shared with this team."""
    login = _need(token, user)
    last_err = None
    for _ in range(3):  # if a teammate pushed at the same moment, start again from their version
        where, env = _checkout(token, user, full)
        _presence(where, user, working_on)
        cdir = where / "chats"
        cdir.mkdir(exist_ok=True)
        for cid in shared_chats:
            src = chats_dir / f"{cid}.json"
            if src.is_file():
                data = _j(src, {})
                data["shared_by"] = login
                (cdir / f"{cid}.json").write_text(json.dumps(data))
        for p in cdir.glob("*.json"):  # a chat I stopped sharing
            if _j(p, {}).get("shared_by") == login and p.stem not in shared_chats:
                p.unlink()
        new_projects = []
        known = {p["id"] for p in team_projects}
        for d in sorted((where / "projects").glob("*")) if (where / "projects").is_dir() else []:
            if d.is_dir() and d.name not in known:
                new_projects.append({"id": d.name, **{k: v for k, v in _j(d / "project.json", {}).items() if k in ("name", "instructions", "color")}})
        for proj in team_projects + new_projects:
            sync.merge_project(where / "projects" / proj["id"], proj, folder_for(proj["id"]), user, joining=proj in new_projects)
        try:
            sync.publish(where, env, f"{login}: sync {time.strftime('%Y-%m-%d %H:%M')}")
            return {"new_projects": new_projects, "summary": summary(where)}
        except sync.SyncError as e:
            last_err = e
            time.sleep(1)
    raise TeamError(f"Couldn't save to the team just now ({last_err}). It will try again shortly.")


def summary(where: Path) -> dict:
    team = _j(where / "team.json", {})
    roles = team.get("roles", {})
    members = []
    for login, role in roles.items():
        pr = _j(where / "members" / f"{login}.json", {})
        members.append({"login": login, "role": role, "name": pr.get("name", login), "avatar": pr.get("avatar", ""),
                        "t": pr.get("t", 0), "working_on": pr.get("working_on", ""), "joined": bool(pr)})
    chats = []
    for p in (where / "chats").glob("*.json") if (where / "chats").is_dir() else []:
        d = _j(p, {})
        chats.append({"id": d.get("id", p.stem), "title": d.get("title", "Chat"), "by": d.get("shared_by", ""), "updated": d.get("updated", 0)})
    activity = []
    for p in (where / "activity").glob("*.jsonl") if (where / "activity").is_dir() else []:
        for line in p.read_text(errors="replace").splitlines()[-200:]:
            try:
                activity.append(json.loads(line))
            except ValueError:
                pass
    projects = [{"id": d.name, **_j(d / "project.json", {})} for d in sorted((where / "projects").glob("*"))
                if d.is_dir()] if (where / "projects").is_dir() else []
    return {"name": team.get("name", "Team"), "members": members, "chats": sorted(chats, key=lambda c: -c["updated"]),
            "activity": sorted(activity, key=lambda a: -a["t"])[:60], "projects": projects}


def read_shared_chat(full: str, chat_id: str) -> dict:
    p = _home() / full.replace("/", "__") / "chats" / f"{Path(chat_id).name}.json"
    if not p.is_file():
        raise TeamError("That chat is no longer shared.")
    return _j(p, {})


def note(token: str, user: dict, full: str, text: str) -> None:
    """Add a line to the team's activity feed (e.g. 'updated brief.md')."""
    where, env = _checkout(token, user, full)
    _log(where, _need(token, user), text)
    sync.publish(where, env, "Activity")


def forget(full: str) -> None:
    shutil.rmtree(_home() / full.replace("/", "__"), ignore_errors=True)
