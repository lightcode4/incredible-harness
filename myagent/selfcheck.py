"""Self-check: tests each part of LightCode⁴ for real inside the installed app (no API spend except an optional
one-line model test). Run from Settings > Updates > Run self-check, or: "LightCode4" --self-test
"""

from __future__ import annotations

import base64
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path


def _pdf() -> bytes:
    stream = "BT /F1 24 Tf 72 700 Td (herness self check) Tj ET"
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream", "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offs = "%PDF-1.4\n", []
    for n, o in enumerate(objs, 1):
        offs.append(len(out))
        out += f"{n} 0 obj\n{o}\nendobj\n"
    x = len(out)
    out += f"xref\n0 {len(objs)+1}\n0000000000 65535 f \n" + "".join(f"{o:010d} 00000 n \n" for o in offs)
    return (out + f"trailer << /Size {len(objs)+1} /Root 1 0 R >>\nstartxref\n{x}\n%%EOF").encode()


def run(session=None, model_test: bool = False) -> list[dict]:
    """Returns [{name, ok, detail}]."""
    results = []

    def check(name, fn):
        t0 = time.time()
        try:
            detail = fn() or "ok"
            results.append({"name": name, "ok": True, "detail": str(detail)[:300], "secs": round(time.time() - t0, 1)})
        except Exception as e:
            results.append({"name": name, "ok": False, "detail": f"{type(e).__name__}: {e}"[:400], "secs": round(time.time() - t0, 1)})

    work = Path(tempfile.mkdtemp(prefix="herness-selfcheck-"))

    def browser():
        from .browser import Browser
        b = Browser(work / "profile", lambda *a: None, lambda *a: None)
        try:
            text = b.call(lambda page: (page.set_content("<h1>herness ok</h1>"), page.inner_text("h1"))[1], timeout=120)
            shot = b.screenshot()
            if "herness ok" not in text or len(base64.b64decode(shot)) < 1000:
                raise RuntimeError("page did not render")
            return "built-in browser opened a page and took a screenshot"
        finally:
            b.close()
    check("Built-in browser", browser)

    def terminal():
        from .terminal import Terminal
        out, done = [], threading.Event()
        t = Terminal(str(work), lambda tid, d: out.append(d), lambda tid: done.set(), 80, 24)
        time.sleep(1.0)
        t.write("echo herness-$((20+22))\r" if os.name != "nt" else "echo herness-42\r")
        for _ in range(60):
            if "herness-42" in "".join(out):
                break
            time.sleep(0.1)
        t.close()
        if "herness-42" not in "".join(out):
            raise RuntimeError("no output from the shell")
        return "shell started and ran a command"
    check("Terminal", terminal)

    def tools():
        from .tools import Workspace
        ws = Workspace(str(work))
        ws.run("write_file", {"path": "a.py", "content": "print(6*7)\n"})
        ws.run("edit_file", {"path": "a.py", "old_text": "6*7", "new_text": "6 * 7"})
        out = ws.run("bash", {"command": f'"{sys.executable}" a.py' if not getattr(sys, "frozen", False) else "echo 42"})
        if "42" not in out:
            raise RuntimeError(out)
        return "write, edit and run commands work"
    check("Files & commands", tools)

    def pdf():
        from .tools import pdf_extract
        text = pdf_extract(_pdf())[0]
        if "herness self check" not in text:
            raise RuntimeError("text not found")
        return "PDF text extracted"
    check("PDF reading", pdf)

    def diagnostics():
        from .extras import diagnose
        (work / "bad.py").write_text("def x(:\n")
        if not diagnose(work / "bad.py", str(work)):
            raise RuntimeError("syntax error not detected")
        return "syntax errors are detected after edits"
    check("Code checks", diagnostics)

    def sandbox():
        from .platform_extras import sandbox_available, wrap_command
        tech = sandbox_available()
        if not tech:
            return "not available on this system (commands run without a sandbox)"
        import subprocess
        target = Path.home() / f".herness-sandbox-probe-{os.getpid()}"
        argv = wrap_command(f"echo x > '{target}'", str(work), "folder")
        subprocess.run(argv, capture_output=True, timeout=30)
        if target.exists():
            target.unlink()
            raise RuntimeError("a write outside the project folder was NOT blocked")
        return f"{tech}: writes outside the project are blocked"
    check("Sandbox", sandbox)

    def git():
        if not shutil.which("git"):
            raise RuntimeError("git is not installed (Source control and parallel sessions need it)")
        from .gitops import git as g
        g(str(work), "init", "-q")
        return "git works"
    check("Git", git)

    def mcp():
        from .mcp_local import McpManager
        McpManager()
        return "MCP client loaded" + ("" if shutil.which("npx") or shutil.which("uvx") else " (install Node.js or uv to use local connectors)")
    check("Connectors (MCP)", mcp)

    if session is not None:
        def provider():
            cfg = session.config
            models = session.list_models(cfg) if cfg["provider"] != "claude" or cfg.get("api_key") else []
            msg = f"{cfg['provider']} reachable" + (f", {len(models)} models" if models else "")
            if model_test:
                reply = session.test_connection(cfg)
                msg += f"; {cfg['model']} replied “{reply[:30]}”"
            return msg
        check("Model provider", provider)

    shutil.rmtree(work, ignore_errors=True)
    return results


def main():
    from .gui import Session
    s = Session()
    s.load()
    res = run(s, model_test="--model" in sys.argv)
    for r in res:
        print(f"{'PASS' if r['ok'] else 'FAIL'}  {r['name']:<18} {r['detail']}  ({r['secs']}s)")
    print(f"{sum(r['ok'] for r in res)}/{len(res)} passed")
    return 0 if all(r["ok"] for r in res) else 1
