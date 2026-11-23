"""Sandbox for agent commands, auto-install updates, logging and problem reports, gist sharing, settings transfer."""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

HOME = Path.home() / ".incredible-herness"
LOG_DIR = HOME / "logs"
log = logging.getLogger("herness")


# ---------------------------------------------------------------- sandbox
def sandbox_available() -> str | None:
    """Name of the sandbox technology on this computer, or None."""
    if sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").exists():
        return "macOS sandbox"
    if sys.platform.startswith("linux") and shutil.which("bwrap"):
        return "bubblewrap"
    return None


def _mac_profile(folder: str, allow_network: bool) -> str:
    def q(p):
        return '"' + str(Path(p).resolve()).replace('"', '') + '"'
    writable = [folder, tempfile.gettempdir(), "/private/tmp", "/private/var/folders", "/dev",
                str(Path.home() / "Library" / "Caches"), str(Path.home() / ".cache"), str(Path.home() / ".npm")]
    rules = ["(version 1)", "(allow default)", "(deny file-write*)",
             "(allow file-write* " + " ".join(f"(subpath {q(w)})" for w in writable if Path(w).exists() or w == folder) + ")"]
    if not allow_network:
        rules.append('(deny network-outbound (remote ip "*:*"))')
        rules.append('(allow network-outbound (remote unix-socket))')
    return "\n".join(rules)


def wrap_command(command: str, folder: str, mode: str) -> list[str] | None:
    """Return argv that runs `command` sandboxed, or None to run it normally.
    mode: off | folder (writes only inside the project and temp folders) | folder_no_net (also no network)."""
    if mode not in ("folder", "folder_no_net"):
        return None
    net = mode == "folder"
    shell = os.environ.get("SHELL") or "/bin/sh"
    if sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").exists():
        return ["/usr/bin/sandbox-exec", "-p", _mac_profile(folder, net), shell, "-c", command]
    if sys.platform.startswith("linux") and shutil.which("bwrap"):
        args = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
                "--bind", folder, folder, "--die-with-parent"]
        if not net:
            args.append("--unshare-net")
        return args + ["/bin/sh", "-c", command]
    return None


# ---------------------------------------------------------------- logging & problem reports
_SECRET = re.compile(r"(sk-ant-[\w-]+|sk-[\w-]{20,}|oc_sk_[\w-]+|gh[pousr]_[\w]{20,}|github_pat_[\w]+|"
                     r"(?i:(api[_-]?key|token|authorization|password)\s*[:=]\s*)\S+)")


def scrub(text: str) -> str:
    return _SECRET.sub("[hidden]", text)


class _ScrubFilter(logging.Filter):
    def filter(self, record):
        record.msg = scrub(str(record.msg))
        if record.args:
            record.args = tuple(scrub(str(a)) for a in record.args)
        return True


def setup_logging():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(LOG_DIR / "herness.log", maxBytes=2_000_000, backupCount=3,
                                                   encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(message)s"))
    handler.addFilter(_ScrubFilter())
    log.addHandler(handler)
    log.setLevel(logging.INFO)

    def hook(exc_type, exc, tb):
        log.error("Uncaught error", exc_info=(exc_type, exc, tb))
    sys.excepthook = hook
    import threading
    threading.excepthook = lambda a: log.error("Uncaught error in thread %s", a.thread.name if a.thread else "?",
                                               exc_info=(a.exc_type, a.exc_value, a.exc_traceback))


def recent_log(lines: int = 60) -> str:
    try:
        return scrub("\n".join((LOG_DIR / "herness.log").read_text(errors="replace").splitlines()[-lines:]))
    except OSError:
        return "(no log yet)"


def problem_report_url(repo: str, version: str, build: int, description: str = "") -> str:
    import platform
    body = (f"**What happened**\n{description or '(describe what you were doing)'}\n\n"
            f"**Version** {version} (build {build}) on {platform.system()} {platform.release()} {platform.machine()}\n\n"
            f"<details><summary>Recent log (secrets removed)</summary>\n\n```\n{recent_log(40)[-5000:]}\n```\n</details>")
    return f"https://github.com/{repo}/issues/new?" + urllib.parse.urlencode({"title": "Problem report", "body": body})


def open_path(path: Path):
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    elif os.name == "nt":
        os.startfile(str(path))  # noqa: S606
    else:
        subprocess.Popen(["xdg-open", str(path)])


# ---------------------------------------------------------------- auto-install updates
def asset_name() -> str:
    return "Incredible-Herness-mac.dmg" if sys.platform == "darwin" else "Incredible-Herness-Setup.exe"


def app_bundle() -> Path | None:
    """The installed .app (macOS) when running as a packaged app."""
    if not getattr(sys, "frozen", False) or sys.platform != "darwin":
        return None
    p = Path(sys.executable).resolve()
    for parent in p.parents:
        if parent.suffix == ".app":
            return parent
    return None


def updater_script(downloaded: str, pid: int) -> tuple[list[str], str]:
    """A small script that waits for this app to quit, installs the new version, and starts it again."""
    if sys.platform == "darwin":
        bundle = app_bundle() or Path("/Applications/Incredible Herness.app")
        script = f"""#!/bin/sh
while kill -0 {pid} 2>/dev/null; do sleep 0.5; done
MNT=$(mktemp -d)
hdiutil attach -nobrowse -quiet -mountpoint "$MNT" {shlex.quote(downloaded)} || exit 1
rm -rf {shlex.quote(str(bundle))}.old
mv {shlex.quote(str(bundle))} {shlex.quote(str(bundle))}.old 2>/dev/null
cp -R "$MNT/Incredible Herness.app" {shlex.quote(str(bundle))} && rm -rf {shlex.quote(str(bundle))}.old || mv {shlex.quote(str(bundle))}.old {shlex.quote(str(bundle))}
hdiutil detach -quiet "$MNT"
open {shlex.quote(str(bundle))}
"""
        path = Path(tempfile.gettempdir()) / "herness-update.sh"
        path.write_text(script)
        path.chmod(0o755)
        return ["/bin/sh", str(path)], script
    script = (f"$p = Get-Process -Id {pid} -ErrorAction SilentlyContinue; if ($p) {{ $p.WaitForExit() }}\r\n"
              f"Start-Process -FilePath '{downloaded}' -ArgumentList '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART' -Wait\r\n")
    path = Path(tempfile.gettempdir()) / "herness-update.ps1"
    path.write_text(script)
    return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", str(path)], script


def download_update(release_api_url: str, token: str = "", progress=None) -> str:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "IncredibleHerness"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(release_api_url, headers=headers), timeout=30) as r:
        rel = json.loads(r.read())
    asset = next((a for a in rel.get("assets", []) if a["name"] == asset_name()), None)
    if not asset:
        raise RuntimeError(f"The new release has no {asset_name()}.")
    dest = Path(tempfile.gettempdir()) / asset["name"]
    req = urllib.request.Request(asset["browser_download_url"], headers={"User-Agent": "IncredibleHerness"})
    with urllib.request.urlopen(req, timeout=600) as r, open(dest, "wb") as f:
        total, done = int(r.headers.get("Content-Length") or 0), 0
        while chunk := r.read(1 << 20):
            f.write(chunk)
            done += len(chunk)
            if progress and total:
                progress(done * 100 // total)
    return str(dest)


def launch_updater(downloaded: str) -> None:
    argv, _ = updater_script(downloaded, os.getpid())
    kw = {"start_new_session": True} if os.name != "nt" else {"creationflags": 0x00000008 | 0x00000200}
    subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)


# ---------------------------------------------------------------- sharing (GitHub Gist)
GITHUB_API = os.environ.get("HERNESS_GITHUB_API", "https://api.github.com")


def publish_gist(token: str, filename: str, content: str, description: str, public: bool = False) -> str:
    if not token:
        raise ValueError("Sharing uses a GitHub token with the 'gist' permission (Settings > Updates).")
    body = json.dumps({"description": description, "public": public, "files": {filename: {"content": content}}}).encode()
    req = urllib.request.Request(f"{GITHUB_API}/gists", data=body, method="POST",
                                 headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                                          "User-Agent": "IncredibleHerness", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())["html_url"]
    except urllib.error.HTTPError as e:
        raise ValueError(f"GitHub refused the gist ({e.code}). The token needs the 'gist' permission.")


# ---------------------------------------------------------------- settings transfer
SECRET_KEYS = ("api_key", "github_token", "voice_key")


def export_settings(config: dict, include_keys: bool) -> dict:
    out = {k: v for k, v in config.items() if k not in ("auto", "plan")}
    if not include_keys:
        for k in SECRET_KEYS:
            out.pop(k, None)
        out["mcp"] = [{**m, "token": ""} for m in out.get("mcp", [])]
        out["mcp_local"] = [{**m, "env": {k: "" for k in (m.get("env") or {})}} for m in out.get("mcp_local", [])]
    return {"incredible_herness_settings": 1, "settings": out}


def import_settings(data: dict, config: dict) -> dict:
    if not isinstance(data, dict) or data.get("incredible_herness_settings") != 1:
        raise ValueError("That file is not an Incredible Herness settings export.")
    incoming = {k: v for k, v in data["settings"].items() if k in config and k not in ("auto", "plan")}
    for k in SECRET_KEYS:  # an export without keys must not wipe the keys already here
        if not incoming.get(k):
            incoming.pop(k, None)
    return {**config, **incoming}


# ---------------------------------------------------------------- access from anywhere
def find_cloudflared() -> str | None:
    import shutil as _sh
    for c in (_sh.which("cloudflared"), "/opt/homebrew/bin/cloudflared", "/usr/local/bin/cloudflared",
              str(Path.home() / ".local/bin/cloudflared"), r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
              r"C:\Program Files\cloudflared\cloudflared.exe"):
        if c and Path(c).is_file():
            return c
    return None


def start_tunnel(port: int, timeout: float = 40):
    """Start a Cloudflare quick tunnel to 127.0.0.1:port. Returns (process, https URL)."""
    import re as _re
    import subprocess as _sp
    import threading as _th
    exe = find_cloudflared()
    if not exe:
        raise RuntimeError("Access from anywhere needs Cloudflare's free cloudflared tool. Install it with "
                           "`brew install cloudflared` (Mac) or `winget install Cloudflare.cloudflared` (Windows), then try again.")
    proc = _sp.Popen([exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"], stdout=_sp.PIPE,
                     stderr=_sp.STDOUT, text=True, errors="replace", stdin=_sp.DEVNULL)
    found, lines = {}, []

    def read():
        for line in proc.stdout:
            lines.append(line)
            m = _re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
            if m and "url" not in found:
                found["url"] = m.group(0)
    _th.Thread(target=read, daemon=True).start()
    import time as _t
    end = _t.time() + timeout
    while _t.time() < end and "url" not in found and proc.poll() is None:
        _t.sleep(0.2)
    if "url" not in found:
        stop_tunnel(proc)
        raise RuntimeError("Cloudflare did not give an address. " + "".join(lines[-5:])[-600:])
    return proc, found["url"]


def stop_tunnel(proc) -> None:
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(5)
        except Exception:
            proc.kill()
