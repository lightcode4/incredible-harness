"""Git for the Git panel and worktree chats: status, diffs, commit, branches, push/pull, pull requests."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class GitError(Exception):
    pass


def git(cwd: str, *args: str, timeout: int = 60, check: bool = True) -> str:
    if not shutil.which("git"):
        raise GitError("Git is not installed on this computer.")
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout, stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if check and p.returncode != 0:
        raise GitError((p.stderr or p.stdout).strip() or f"git {' '.join(args)} failed")
    return p.stdout


def root(cwd: str) -> str | None:
    try:
        return git(cwd, "rev-parse", "--show-toplevel").strip()
    except (GitError, OSError):
        return None


def status(cwd: str) -> dict:
    top = root(cwd)
    if not top:
        return {"repo": False}
    branch = git(cwd, "branch", "--show-current").strip() or "(detached)"
    files = []
    for line in git(cwd, "status", "--porcelain=v1", "-uall").splitlines():
        code, path = line[:2], line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        kind = "added" if "?" in code or "A" in code else "deleted" if "D" in code else "renamed" if "R" in code else "modified"
        files.append({"path": path.strip('"'), "status": kind, "code": code})
    upstream = git(cwd, "rev-parse", "--abbrev-ref", "@{u}", check=False).strip()
    ahead = behind = 0
    if upstream and "fatal" not in upstream:
        counts = git(cwd, "rev-list", "--left-right", "--count", "HEAD...@{u}", check=False).split()
        if len(counts) == 2:
            ahead, behind = int(counts[0]), int(counts[1])
    else:
        upstream = ""
    branches = [b.strip().lstrip("* ").strip() for b in git(cwd, "branch", "--format=%(refname:short)").splitlines() if b.strip()]
    last = git(cwd, "log", "-1", "--format=%h %s", check=False).strip()
    return {"repo": True, "root": top, "branch": branch, "files": files, "upstream": upstream, "ahead": ahead,
            "behind": behind, "branches": branches, "last_commit": last, "gh": bool(shutil.which("gh"))}


def diff(cwd: str, path: str | None = None) -> str:
    args = ["diff", "HEAD", "--"] + ([path] if path else [])
    out = git(cwd, *args, check=False)
    if path and not out:  # untracked file: show it as all-new
        p = Path(cwd) / path
        if p.is_file():
            try:
                lines = p.read_text(errors="replace").splitlines()
            except OSError:
                lines = []
            out = f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n" + "\n".join("+" + l for l in lines[:3000])
    return out[:200_000]


def commit(cwd: str, message: str, files: list[str] | None = None) -> str:
    if not message.strip():
        raise GitError("Write a commit message first.")
    if files:
        git(cwd, "add", "--", *files)
    else:
        git(cwd, "add", "-A")
    return git(cwd, "commit", "-m", message).strip().splitlines()[0]


def checkout(cwd: str, branch: str, create: bool = False) -> str:
    return git(cwd, "checkout", *(["-b"] if create else []), branch).strip() or f"On {branch}"


def push(cwd: str) -> str:
    branch = git(cwd, "branch", "--show-current").strip()
    p = subprocess.run(["git", "push", "-u", "origin", branch], cwd=cwd, capture_output=True, text=True, timeout=180,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if p.returncode != 0:
        raise GitError(p.stderr.strip() or "Push failed.")
    return f"Pushed {branch}."


def pull(cwd: str) -> str:
    return git(cwd, "pull", "--ff-only", timeout=180).strip() or "Up to date."


def create_pr(cwd: str) -> str:
    if not shutil.which("gh"):
        raise GitError("Install the GitHub CLI (gh) and run 'gh auth login' to create pull requests.")
    p = subprocess.run(["gh", "pr", "create", "--fill"], cwd=cwd, capture_output=True, text=True, timeout=120,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if p.returncode != 0:
        raise GitError(p.stderr.strip() or "Could not create the pull request.")
    return p.stdout.strip().splitlines()[-1]


# ---- worktrees: run a chat on its own branch in its own folder (parallel agents) ----
def add_worktree(cwd: str, name: str) -> tuple[str, str]:
    top = root(cwd)
    if not top:
        raise GitError("Parallel worktree chats need the folder to be a git repository.")
    if not git(top, "rev-parse", "--verify", "HEAD", check=False).strip():
        raise GitError("The repository has no commits yet. Make a first commit, then try again.")
    branch = f"herness/{name}"
    path = str(Path(top).parent / f"{Path(top).name}-herness-{name}")
    git(top, "worktree", "add", "-b", branch, path)
    return path, branch


def finish_worktree(path: str, merge: bool) -> str:
    branch = git(path, "branch", "--show-current").strip()
    main_top = Path(git(path, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()).parent
    msg = ""
    if merge:
        if git(path, "status", "--porcelain").strip():
            git(path, "add", "-A")
            git(path, "commit", "-m", f"Work from {branch}")
        out = subprocess.run(["git", "merge", "--no-edit", branch], cwd=main_top, capture_output=True, text=True)
        if out.returncode != 0:
            subprocess.run(["git", "merge", "--abort"], cwd=main_top, capture_output=True)
            raise GitError(f"Merging {branch} hit conflicts; nothing was changed. Resolve it in the chat or in a terminal.")
        msg = f"Merged {branch} into {git(str(main_top), 'branch', '--show-current').strip()}. "
    git(str(main_top), "worktree", "remove", "--force", path)
    git(str(main_top), "branch", "-D", branch, check=False)
    return msg + "Removed the worktree."
