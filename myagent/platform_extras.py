"""Sandbox for agent commands, auto-install updates, logging and problem reports, gist sharing, settings transfer."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
import threading
import time
import logging
import logging.handlers
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.error
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
    return "LightCode4-mac.dmg" if sys.platform == "darwin" else "LightCode4-Setup.exe"


def asset_match(name: str) -> bool:
    """True if a release asset is this platform's installer — ignoring any -buildNNN in the middle."""
    if sys.platform == "darwin":
        return name.startswith("LightCode4-mac") and name.endswith(".dmg")
    return name.startswith("LightCode4-Setup") and name.endswith(".exe")


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
        bundle = app_bundle() or Path("/Applications/LightCode4.app")
        script = f"""#!/bin/sh
while kill -0 {pid} 2>/dev/null; do sleep 0.5; done
MNT=$(mktemp -d)
hdiutil attach -nobrowse -quiet -mountpoint "$MNT" {shlex.quote(downloaded)} || exit 1
rm -rf {shlex.quote(str(bundle))}.old
mv {shlex.quote(str(bundle))} {shlex.quote(str(bundle))}.old 2>/dev/null
cp -R "$MNT/LightCode4.app" {shlex.quote(str(bundle))} && rm -rf {shlex.quote(str(bundle))}.old || mv {shlex.quote(str(bundle))}.old {shlex.quote(str(bundle))}
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


PART = 8 << 20  # download in 8 MB pieces
WORKERS = 6  # pieces fetched at the same time


def _fetch_range(url: str, start: int, end: int, f, lock, on_bytes, tries: int = 10) -> None:
    """Fetch bytes start..end into the open file; on a dropped connection, wait and continue from the last byte received."""
    pos, attempt = start, 0
    while pos <= end:
        req = urllib.request.Request(url, headers={"User-Agent": "LightCode4", "Range": f"bytes={pos}-{end}"})
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                if r.status != 206 and pos > 0:
                    raise RuntimeError("The download server does not support resuming.")
                while pos <= end and (chunk := r.read(min(256 << 10, end - pos + 1))):
                    with lock:
                        f.seek(pos)
                        f.write(chunk)
                    pos += len(chunk)
                    on_bytes(len(chunk))
                    attempt = 0
        except (OSError, http.client.HTTPException) as e:  # reset, timeout, dropped Wi-Fi: retry this piece only
            attempt += 1
            if attempt > tries:
                raise RuntimeError(f"The download kept failing ({e}). Check your connection and try again: it continues where it stopped.") from e
            time.sleep(min(30, 2 ** attempt))


RELEASES_API = os.environ.get("LC4_RELEASES_API", "https://api.github.com")


def github_json(url: str, token: str = ""):
    """Read GitHub's public release information. Tries without a token first (releases are public), so an expired
    or revoked GitHub token can't break update checks; uses the token only if GitHub needs it (a private repository)."""
    for auth in ([False, True] if token else [False]):
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "LightCode4"}
        if auth:
            headers["Authorization"] = f"Bearer {token}"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if not auth and token and e.code in (401, 403, 404):
                continue
            raise


def download_update(release_api_url: str, token: str = "", progress=None) -> str:
    """Download the installer in parallel pieces. Each piece retries by itself, finished pieces are remembered so a
    later try (even after restarting the app) only fetches what is missing, and the file is checked against GitHub's
    sha256 before it is used."""
    rel = github_json(release_api_url, token)
    asset = next((a for a in rel.get("assets", []) if asset_match(a["name"])), None)
    if not asset:
        raise RuntimeError(f"The new release has no {asset_name()}.")
    size, url = int(asset.get("size") or 0), asset["browser_download_url"]
    want = (asset.get("digest") or "").removeprefix("sha256:")
    dest = Path(tempfile.gettempdir()) / asset["name"]
    part = dest.with_name(dest.name + ".part")
    state_file = dest.with_name(dest.name + ".pieces.json")
    if dest.is_file() and size and dest.stat().st_size == size and (not want or _sha256(dest) == want):
        return str(dest)  # already downloaded
    ranges = [(a, min(a + PART, size) - 1) for a in range(0, size, PART)] if size else [(0, 0)]
    key = f"{url}|{size}|{want}"
    done_set: set[int] = set()
    try:
        st = json.loads(state_file.read_text())
        if st.get("key") == key and part.is_file() and part.stat().st_size == size:
            done_set = set(st.get("done", []))
    except Exception:
        pass
    if not done_set:
        with open(part, "wb") as f:
            f.truncate(size)
    lock, got = threading.Lock(), [sum(ranges[i][1] - ranges[i][0] + 1 for i in done_set if i < len(ranges))]
    last = [-1]

    def on_bytes(n):
        with lock:
            got[0] += n
            pct = got[0] * 100 // size if size else 0
            if progress and pct != last[0]:
                last[0] = pct
                progress(min(pct, 99))

    def piece(i):
        a, b = ranges[i]
        _fetch_range(url, a, b, f, lock, on_bytes)
        with lock:
            done_set.add(i)
            state_file.write_text(json.dumps({"key": key, "done": sorted(done_set)}))

    with open(part, "r+b") as f:
        todo = [i for i in range(len(ranges)) if i not in done_set]
        with ThreadPoolExecutor(WORKERS) as pool:
            for fut in [pool.submit(piece, i) for i in todo]:
                fut.result()
    if want and _sha256(part) != want:
        part.unlink(missing_ok=True)
        state_file.unlink(missing_ok=True)
        raise RuntimeError("The downloaded update was damaged (its fingerprint did not match). Try again.")
    os.replace(part, dest)
    state_file.unlink(missing_ok=True)
    if progress:
        progress(100)
    return str(dest)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(4 << 20):
            h.update(chunk)
    return h.hexdigest()


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
                                          "User-Agent": "LightCode4", "Content-Type": "application/json"})
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
        raise ValueError("That file is not an LightCode⁴ settings export.")
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


def reveal(path: str) -> None:
    """Show a folder in Finder / Explorer / the file manager."""
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", path])
        elif os.name == "nt":
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
