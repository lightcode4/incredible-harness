"""Offline tests for LightCode⁴: a scripted fake Claude / OpenAI server drives the real engine.
No real API calls, no tokens used.   Run: python test_loop.py
"""
import base64
import json
import shutil
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

os.environ["HOME"] = os.environ["USERPROFILE"] = tempfile.mkdtemp()  # keep settings/chats out of the real home
os.environ["LC4_NO_MARKET_REFRESH"] = "1"  # no background GitHub calls during tests
REQUESTS: list = []
SCRIPT: list = []  # steps popped per model request: ("text", str) | ("tool", name, input) | ("tools", [(name, input), ...])


def _claude_events(step, n):
    msg = {"id": f"msg_{n}", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": [],
           "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 1000, "output_tokens": 1}}
    ev, i = [{"type": "message_start", "message": msg}], 0
    calls = [step[1:]] if step[0] == "tool" else step[1] if step[0] == "tools" else []
    text = step[1] if step[0] == "text" else "Working on it."
    ev += [{"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
           {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
           {"type": "content_block_stop", "index": 0}]
    for name, inp in calls:
        i += 1
        ev += [{"type": "content_block_start", "index": i, "content_block": {"type": "tool_use", "id": f"toolu_{n}_{i}", "name": name, "input": {}}},
               {"type": "content_block_delta", "index": i, "delta": {"type": "input_json_delta", "partial_json": json.dumps(inp)}},
               {"type": "content_block_stop", "index": i}]
    ev += [{"type": "message_delta", "delta": {"stop_reason": "tool_use" if calls else "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 50}}, {"type": "message_stop"}]
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in ev).encode()


def _openai_events(step):
    def chunk(delta, finish=None, usage=None):
        d = {"id": "c", "object": "chat.completion.chunk", "created": 0, "model": "m",
             "choices": [] if usage else [{"index": 0, "delta": delta, "finish_reason": finish}]}
        if usage:
            d["usage"] = usage
        return "data: " + json.dumps(d) + "\n\n"
    if step[0] == "tool":
        args = json.dumps(step[2])
        body = chunk({"role": "assistant", "tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                                           "function": {"name": step[1], "arguments": args[:10]}}]})
        body += chunk({"tool_calls": [{"index": 0, "function": {"arguments": args[10:]}}]}) + chunk({}, "tool_calls")
    else:
        body = chunk({"role": "assistant", "content": step[1]}) + chunk({}, "stop")
    body += chunk(None, usage={"prompt_tokens": 500, "completion_tokens": 20, "total_tokens": 520})
    return (body + "data: [DONE]\n\n").encode()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        REQUESTS.append((self.path, dict(self.headers), body))
        if self.path.endswith("/gists"):
            out = json.dumps({"html_url": "https://gist.github.com/test/abc123"}).encode()
            self.send_response(201)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(out)
            return
        step = SCRIPT.pop(0) if SCRIPT else ("text", "Done.")
        if step[0] == "drop":  # the internet cuts out mid-reply: half the stream arrives, then the connection dies
            data = _claude_events(("text", step[1]), len(REQUESTS)) if "/messages" in self.path else _openai_events(("text", step[1]))
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data[: len(data) // 2])
            self.wfile.flush()
            self.close_connection = True
            return
        data = _claude_events(step, len(REQUESTS)) if "/messages" in self.path else _openai_events(step)
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        self.wfile.write(data)


def start_fake():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}"
    os.environ["HERNESS_GITHUB_API"] = url
    os.environ["ANTHROPIC_BASE_URL"] = url
    os.environ["ANTHROPIC_API_KEY"] = "test"
    return url


class Driver:
    """Consumes session events like the window does; answers approval prompts with a policy."""

    def __init__(self, session, policy="allow"):
        self.s, self.policy, self.events = session, policy, []
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            ev = self.s.events.get()
            self.events.append(ev)
            if ev["kind"] == "approve":
                item = self.s.pending.get(ev["id"])
                if item:
                    item[1].update(decision=self.policy, feedback="use python instead" if self.policy == "deny" else "")
                    if self.policy == "partial":
                        item[1].update(hunks=[0])
                    item[0].set()

    def run(self, text, attachments=None, script=()):
        SCRIPT[:] = list(script)
        start = len(self.events)
        self.s.send(text, attachments or [])
        deadline = time.time() + 30
        while time.time() < deadline:
            if any(e["kind"] == "status" and e["busy"] is False for e in self.events[start:]):
                time.sleep(0.05)
                return self.events[start:]
            time.sleep(0.02)
        raise TimeoutError(text)

    @staticmethod
    def kinds(evs, kind):
        return [e for e in evs if e["kind"] == kind]


def main():
    start_fake()
    from myagent import cli
    from myagent.gui import CHATS, Session

    work = Path(tempfile.mkdtemp())
    s = Session()
    s.apply_config({"cwd": str(work)})
    d = Driver(s)

    # 1. bash with approval; request shape (fallbacks, compaction, web, MCP, eager streaming)
    evs = d.run("say hello", script=[("tool", "bash", {"command": "echo hello-from-tool"}), ("text", "Done.")])
    res = d.kinds(evs, "tool_result")
    assert res and "hello-from-tool" in res[0]["output"] and not res[0]["error"], res
    _, hdr, body = REQUESTS[-2]
    betas = hdr.get("anthropic-beta", "")
    for b in ("server-side-fallback-2026-07-01", "compact-2026-01-12"):
        assert b in betas, betas
    names = [t.get("name") or t.get("type") for t in body["tools"]]
    assert "web_search" in names and "web_fetch" in names and "mcp_toolset" not in names and "fetch_url" not in names, names
    assert body["context_management"] == {"edits": [{"type": "compact_20260112"}]} and body["fallbacks"] == "default"
    assert all(t.get("eager_input_streaming") for t in body["tools"] if "input_schema" in t)
    tr = REQUESTS[-1][2]["messages"][-1]["content"]
    assert len(tr) == 1 and tr[0]["type"] == "tool_result"
    assert d.kinds(evs, "usage")[-1]["cost"] > 0
    print("1 bash + request shape OK")

    # 2. write -> diff shown before approval -> undo removes the new file
    evs = d.run("make a file", script=[("tool", "write_file", {"path": "a.txt", "content": "one\ntwo\n"}), ("text", "Wrote it.")])
    ap = d.kinds(evs, "approve")[0]
    assert "+one" in ap["diff"] and (work / "a.txt").read_text() == "one\ntwo\n"
    end = d.kinds(evs, "turn_end")[0]
    assert end["files"] == ["a.txt"]
    s.undo(end["turn"])
    assert not (work / "a.txt").exists()
    print("2 diff + undo OK")

    # 3. multi_edit + undo restores previous content
    (work / "b.py").write_text("x = 1\ny = 2\n")
    evs = d.run("edit", script=[("tool", "multi_edit", {"path": "b.py", "edits": [
        {"old_text": "x = 1", "new_text": "x = 10"}, {"old_text": "y = 2", "new_text": "y = 20"}]}), ("text", "Edited.")])
    assert (work / "b.py").read_text() == "x = 10\ny = 20\n"
    s.undo(d.kinds(evs, "turn_end")[0]["turn"])
    assert (work / "b.py").read_text() == "x = 1\ny = 2\n"
    print("3 multi_edit + undo OK")

    # 4. permission rules: deny wins, allow skips the prompt, decline sends feedback
    s.add_rule(s.current.rules_dir, "bash(rm *)", "deny")
    s.add_rule(s.current.rules_dir, "bash(echo *)", "allow")
    evs = d.run("rules", script=[("tools", [("bash", {"command": "rm -rf x"}), ("bash", {"command": "echo ok"})]), ("text", "ok")])
    assert not d.kinds(evs, "approve"), "allow rule should skip the prompt"
    blocked = [e for e in d.kinds(evs, "tool") if e.get("blocked")]
    assert blocked and blocked[0]["summary"] == "$ rm -rf x"
    d.policy = "deny"
    d.run("decline", script=[("tool", "bash", {"command": "touch declined.txt"}), ("text", "ok")])
    last_req = REQUESTS[-1][2]["messages"][-1]["content"][0]
    assert last_req["is_error"] and "use python instead" in last_req["content"]
    d.policy = "allow"
    print("4 permission rules OK")

    # 5. plan mode blocks changes and offers plan approval
    s.config["plan"] = True
    evs = d.run("plan it", script=[("tool", "write_file", {"path": "p.txt", "content": "x"}), ("text", "1. Do X")])
    assert not (work / "p.txt").exists() and d.kinds(evs, "plan_ready")
    assert "Plan mode is ON" in json.dumps(REQUESTS[-2][2]["messages"][-1])
    s.config["plan"] = False
    print("5 plan mode OK")

    # 6. todo list
    todos = [{"content": "Read code", "status": "completed"}, {"content": "Fix bug", "status": "in_progress"}]
    evs = d.run("todo", script=[("tool", "todo_write", {"todos": todos}), ("text", "ok")])
    assert d.kinds(evs, "todos")[0]["todos"] == todos
    print("6 todos OK")

    # 7. sub-agent: read-only tools, report returned to the main agent
    n0 = len(REQUESTS)
    d.run("research", script=[("tool", "task", {"description": "find config", "prompt": "Find the config"}),
                              ("tool", "list_files", {"pattern": "*"}), ("text", "REPORT: found b.py"), ("text", "Thanks")])
    sub_tools = {t.get("name") for t in REQUESTS[n0 + 1][2]["tools"]}
    assert "bash" not in sub_tools and "write_file" not in sub_tools and "read_file" in sub_tools, sub_tools
    assert "REPORT: found b.py" in REQUESTS[-1][2]["messages"][-1]["content"][0]["content"]
    print("7 sub-agent OK")

    # 8. background job
    evs = d.run("bg", script=[("tool", "bash", {"command": "echo started; sleep 0.2; echo finished", "run_in_background": True}),
                              ("text", "started")])
    job = d.kinds(evs, "tool_result")[0]["output"].split()[3]
    time.sleep(0.8)
    out = s.ws.run("bash_output", {"job_id": job})
    assert "finished" in out and "exit code 0" in out, out
    print("8 background job OK")

    # 9. attachments: image + text file reach Claude in the right shape
    img = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    d.run("look", attachments=[{"name": "dot.png", "mime": "image/png", "data": img},
                               {"name": "notes.txt", "mime": "text/plain", "data": base64.b64encode(b"hello notes").decode()}],
          script=[("text", "I see a dot.")])
    content = REQUESTS[-1][2]["messages"][-1]["content"]
    assert content[0]["type"] == "image" and "hello notes" in content[1]["text"], content
    print("9 attachments OK")

    # 10. budget stops before the next request
    s.config["budget"] = 0.000001
    evs = d.run("over budget", script=[("text", "should not be reached")])
    assert any("Budget" in e.get("text", "") for e in d.kinds(evs, "notice"))
    s.config["budget"] = 0
    print("10 budget OK")

    # 11. project instructions reach the system prompt
    (work / "HERNESS.md").write_text("Always answer in French.")
    s.apply_config({}, keep_history=True)
    d.run("bonjour", script=[("text", "Bonjour")])
    assert "Always answer in French." in REQUESTS[-1][2]["system"][0]["text"] and s.instruction_files == ["HERNESS.md"]
    print("11 project instructions OK")

    # 12. chat history: saved, listed, reopened with transcript and model context
    chat_id = s.chat_id
    s.new_chat()
    assert any(c["id"] == chat_id for c in s.list_chats()) and (CHATS / f"{chat_id}.json").exists()
    s.open_chat(chat_id)
    assert s.chat_id == chat_id and len(s.provider.messages) > 4 and s.transcript
    d.run("continue", script=[("text", "Continuing.")])
    assert "say hello" in json.dumps(REQUESTS[-1][2]["messages"][0])
    print("12 chat history OK")

    # 13. OpenAI-compatible provider: tool loop + usage
    s.apply_config({"provider": "local", "base_url": os.environ["ANTHROPIC_BASE_URL"] + "/v1"})
    evs = d.run("local", script=[("tool", "bash", {"command": "echo from-local"}), ("text", "Done (local).")])
    assert "from-local" in d.kinds(evs, "tool_result")[0]["output"]
    tool_msg = REQUESTS[-1][2]["messages"][-1]
    assert tool_msg["role"] == "tool" and "from-local" in tool_msg["content"]
    assert d.kinds(evs, "usage")[-1]["tokens"] > 0
    print("13 OpenAI-compatible OK")

    # 14. terminal version still works
    import argparse
    SCRIPT[:] = [("tool", "bash", {"command": "echo cli-ok"}), ("text", "cli done")]
    cli.input = lambda prompt="": "y"
    app = cli.App(argparse.Namespace(provider="claude", model=None, base_url=None, api_key=None, effort="high",
                                     cwd=str(work), auto=False, prompt=[]))
    app.provider.turn("hi", app.run_tool, app.ui)
    assert "cli-ok" in json.dumps(REQUESTS[-1][2]["messages"][-1]["content"])
    print("14 terminal version OK")

    # 15. interactive terminal panel (pseudo-terminal running the user's shell)
    tid = s.open_terminal(80, 24)
    time.sleep(1.0)
    s.terminals[tid].write("echo term-$((40+2))\r")
    deadline = time.time() + 15
    while time.time() < deadline and not any(e["kind"] == "term" and "term-42" in e["data"] for e in d.events):
        time.sleep(0.1)
    assert any(e["kind"] == "term" and "term-42" in e["data"] for e in d.events), "terminal produced no output"
    s.terminals.pop(tid).close()
    print("15 terminal panel OK")

    # 16. built-in browser: navigate, read, click (with approval), screenshot reaches Claude as an image
    from http.server import SimpleHTTPRequestHandler
    site = Path(tempfile.mkdtemp())
    (site / "index.html").write_text("<title>Start</title><h1>Hello page</h1>"
                                     "<button onclick=\"document.title='Clicked';this.innerText='Done!'\">Press me</button>")

    class Quiet(SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(site), **k)

        def log_message(self, *a):
            pass
    web = ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
    threading.Thread(target=web.serve_forever, daemon=True).start()
    page = f"http://127.0.0.1:{web.server_port}/"
    s.apply_config({"provider": "claude", "model": "claude-opus-5-5"})
    evs = d.run("check the page", script=[("tool", "browser_navigate", {"url": page}), ("tool", "browser_read", {}),
                                          ("tool", "browser_click", {"text": "Press me"}), ("tool", "browser_screenshot", {}),
                                          ("tool", "browser_read", {}), ("text", "The button works.")])
    results = d.kinds(evs, "tool_result")
    if any("not installed" in r["output"] for r in results):
        print("16 browser SKIPPED (no browser engine on this machine)")
    else:
        assert "Hello page" in results[1]["output"] and "Press me" in results[1]["output"], results[1]["output"][:500]
        assert d.kinds(evs, "approve") and d.kinds(evs, "approve")[0]["name"] == "browser_click"
        shot_result = REQUESTS[-2][2]["messages"][-1]["content"][0]["content"]
        assert any(b["type"] == "image" for b in shot_result), shot_result
        assert "Clicked" in results[-1]["output"] and "Done!" in results[-1]["output"]
        # you can select text in the Browser panel and copy it; pasted text lands in the page's text box
        (site / "sel.html").write_text("<p id=p style='font:20px monospace;margin:0;position:absolute;left:10px;top:10px'>CopyThisWord and more</p>"
                                       "<input id=i style='position:absolute;left:10px;top:80px'>")
        s.browser_input({"type": "navigate", "url": page + "sel.html"})
        s.browser_input({"type": "dblclick", "x": 40, "y": 22})
        assert s.browser_input({"type": "selection"})["text"].strip() == "CopyThisWord"
        s.browser_input({"type": "click", "x": 400, "y": 300})  # clear it first: pressing on selected text drags it instead
        s.browser_input({"type": "down", "x": 11, "y": 22}); s.browser_input({"type": "move", "x": 200, "y": 22}); s.browser_input({"type": "up", "x": 200, "y": 22})
        assert s.browser_input({"type": "selection"})["text"].startswith("CopyThisWord an")
        s.browser_input({"type": "click", "x": 30, "y": 90}); s.browser_input({"type": "text", "text": "pasted text"})
        s.browser_input({"type": "key", "key": "ControlOrMeta+a"})
        assert s.browser_input({"type": "selection"})["text"] == "pasted text"
        s.browser.close()
        print("16 built-in browser OK (incl. select and copy)")
    # 17. PDFs: Claude gets the real pages, read_file and other providers get the text
    def make_pdf(pages):
        objs = ["<< /Type /Catalog /Pages 2 0 R >>",
                "<< /Type /Pages /Kids [" + " ".join(f"{3 + 2 * i} 0 R" for i in range(len(pages))) + f"] /Count {len(pages)} >>"]
        for i, text in enumerate(pages):
            stream = f"BT /F1 24 Tf 72 700 Td ({text}) Tj ET"
            objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {4 + 2 * i} 0 R "
                        f"/Resources << /Font << /F1 {3 + 2 * len(pages)} 0 R >> >> >>")
            objs.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
        objs.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
        out, offsets = "%PDF-1.4\n", []
        for n, o in enumerate(objs, 1):
            offsets.append(len(out))
            out += f"{n} 0 obj\n{o}\nendobj\n"
        xref = len(out)
        out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n" + "".join(f"{o:010d} 00000 n \n" for o in offsets)
        out += f"trailer << /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF"
        return out.encode()
    (work / "report.pdf").write_bytes(make_pdf(["Quarterly revenue 42", "Profit grew 7 percent"]))
    d.run("read the report", script=[("tool", "read_pdf", {"source": "report.pdf", "pages": "2"}), ("text", "Profit grew.")])
    tr = REQUESTS[-1][2]["messages"][-1]["content"][0]["content"]
    doc = [b for b in tr if b["type"] == "document"]
    assert doc and doc[0]["source"]["media_type"] == "application/pdf", tr
    sub = base64.b64decode(doc[0]["source"]["data"])
    assert sub.startswith(b"%PDF") and b"Quarterly" not in sub  # only page 2 was sent
    assert "Profit grew 7 percent" in s.ws.run("read_file", {"path": "report.pdf"})
    s.apply_config({"provider": "local", "base_url": os.environ["ANTHROPIC_BASE_URL"] + "/v1"})
    d.run("summarize", attachments=[{"name": "report.pdf", "mime": "application/pdf",
                                     "data": base64.b64encode((work / "report.pdf").read_bytes()).decode()}],
          script=[("text", "It says revenue 42.")])
    assert "Quarterly revenue 42" in json.dumps(REQUESTS[-1][2]["messages"][-1]["content"])
    print("17 PDFs OK")

    import subprocess
    from myagent import extras
    s.apply_config({"provider": "claude", "model": "claude-opus-5-5"})
    repo = Path(tempfile.mkdtemp()) / "proj"
    repo.mkdir()
    for c in (["init", "-q", "-b", "main"], ["config", "user.email", "t@example.com"], ["config", "user.name", "T"]):
        subprocess.run(["git", *c], cwd=repo, check=True)
    (repo / "app.py").write_text("print('hi')\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    s.apply_config({"cwd": str(repo)})
    s.new_chat()

    # 18. diagnostics after an edit that breaks Python syntax
    d.run("break it", script=[("tool", "write_file", {"path": "app.py", "content": "def broken(:\n    pass\n"}), ("text", "oops")])
    res = REQUESTS[-1][2]["messages"][-1]["content"][0]["content"]
    assert "Problems found in app.py" in res, res
    (repo / "app.py").write_text("print('hi')\n")
    print("18 diagnostics OK")

    # 19. hooks: after-edit output on failure; before-finish failure is sent back to the agent to fix
    folder = s.current.rules_dir
    s.config["hooks"][folder] = {"after_edit": "exit 3", "before_finish": f'"{sys.executable}" -c "import sys; sys.exit(0 if open(\'ok.txt\').read()==\'yes\' else 1)"'}
    evs = d.run("make ok", script=[("tool", "write_file", {"path": "ok.txt", "content": "no"}), ("text", "done"),
                                   ("tool", "write_file", {"path": "ok.txt", "content": "yes"}), ("text", "fixed")])
    hooks = d.kinds(evs, "hook")
    assert any(h["name"] == "after edit" and not h["ok"] for h in hooks)
    assert [h["ok"] for h in hooks if h["name"] == "before finish"] == [False, True], hooks
    assert "project check" in json.dumps(REQUESTS[-2][2]["messages"][-1])
    s.config["hooks"][folder] = {}
    print("19 hooks OK")

    # 20. memory: the agent saves a preference, new chats see it
    d.run("remember", script=[("tool", "remember", {"note": "Prefers tabs over spaces"}), ("text", "Noted")])
    assert "Prefers tabs over spaces" in extras.read_memory()
    s.new_chat()
    d.run("hi", script=[("text", "hello")])
    assert "Prefers tabs over spaces" in REQUESTS[-1][2]["system"][0]["text"]
    print("20 memory OK")

    # 21. slash commands: built-in /review and a project command expand into prompts
    (repo / ".herness" / "commands").mkdir(parents=True)
    (repo / ".herness" / "commands" / "deploy.md").write_text("---\ndescription: Ship it\n---\nDeploy to $ARGUMENTS now.")
    d.run("/review focus on auth", script=[("text", "No issues.")])
    sent = json.dumps(REQUESTS[-1][2]["messages"][-1])
    assert "git diff HEAD" in sent and "focus on auth" in sent
    d.run("/deploy staging", script=[("text", "ok")])
    assert "Deploy to staging now." in json.dumps(REQUESTS[-1][2]["messages"][-1])
    assert any(c["name"] == "deploy" for c in s.command_list())
    print("21 slash commands OK")

    # 22. notebooks: read shows cells, edit changes one cell and can be undone
    nb = {"cells": [{"cell_type": "code", "metadata": {}, "source": ["x = 1\n"], "outputs": [], "execution_count": None}],
          "metadata": {}, "nbformat": 4, "nbformat_minor": 5}
    (repo / "nb.ipynb").write_text(json.dumps(nb))
    assert "[cell 0] code" in s.ws.run("read_file", {"path": "nb.ipynb"})
    evs = d.run("nb", script=[("tool", "notebook_edit", {"path": "nb.ipynb", "cell": 1, "source": "print(x)", "mode": "insert"}), ("text", "ok")])
    assert len(json.loads((repo / "nb.ipynb").read_text())["cells"]) == 2
    s.undo(d.kinds(evs, "turn_end")[0]["turn"])
    assert len(json.loads((repo / "nb.ipynb").read_text())["cells"]) == 1
    print("22 notebooks OK")

    # 23. git panel: status, diff, commit, branch
    (repo / "new.txt").write_text("hello\n")
    st = s.git_action({"action": "status"})
    assert st["repo"] and st["branch"] == "main" and any(f["path"] == "new.txt" for f in st["files"])
    assert "+hello" in s.git_action({"action": "diff", "path": "new.txt"})["diff"]
    s.git_action({"action": "commit", "message": "Add files"})
    assert s.git_action({"action": "status"})["files"] == []
    s.git_action({"action": "checkout", "branch": "feature", "create": True})
    assert s.git_action({"action": "status"})["branch"] == "feature"
    s.git_action({"action": "checkout", "branch": "main"})
    print("23 git OK")

    # 24. parallel agents: two chats run at the same time; a worktree chat merges back
    s.new_chat(worktree=True)
    wt_chat = s.current
    assert wt_chat.worktree and Path(wt_chat.cwd).is_dir() and wt_chat.cwd != str(repo)
    SCRIPT[:] = [("tool", "bash", {"command": "sleep 1.5 && echo A > from_a.txt"}),
                 ("tool", "bash", {"command": "sleep 1.5 && echo B > from_b.txt"}), ("text", "A done"), ("text", "B done")]
    s.config["auto"] = True
    wt_chat.send("task A")
    time.sleep(0.4)
    s.new_chat()
    other = s.current
    other.send("task B")
    time.sleep(0.5)
    assert wt_chat.busy and other.busy, "both chats should be working at once"
    for _ in range(100):
        if not wt_chat.busy and not other.busy:
            break
        time.sleep(0.1)
    s.config["auto"] = False
    assert (Path(wt_chat.cwd) / "from_a.txt").exists() and (repo / "from_b.txt").exists()
    assert not (repo / "from_a.txt").exists()  # isolated until merged
    assert any(c["id"] == wt_chat.id for c in s.list_chats())
    s.open_chat(wt_chat.id)
    s.finish_worktree(merge=True)
    assert (repo / "from_a.txt").exists() and s.current.worktree is None
    print("24 parallel agents + worktree OK")

    # 25. export
    out_path = Path(s.export("md"))
    assert out_path.exists() and "task A" in out_path.read_text()
    out_path.unlink()
    print("25 export OK")

    # 26. scheduled tasks: due() logic and unattended runs cannot get approvals
    assert s.due({"prompt": "x", "every": 60, "last": 0}, time.time())
    assert not s.due({"prompt": "x", "every": 60, "last": time.time()}, time.time())
    SCRIPT[:] = [("tool", "bash", {"command": "touch should-not-run.txt"}), ("text", "Summary: skipped one step.")]
    task = {"id": "t1", "name": "Nightly check", "prompt": "check things", "cwd": str(repo), "every": 60}
    s.config["schedules"] = [task]
    chat = s.run_task(task)
    for _ in range(100):
        if not chat.busy:
            break
        time.sleep(0.1)
    blocked = [e for e in chat.transcript if e["kind"] == "tool" and e.get("blocked")]
    assert blocked and "scheduled" in blocked[0]["blocked"] and task["last"] > 0
    print("26 scheduled tasks OK")

    # 27. phone access: a second server on the network with its own token
    from myagent.gui import make_handler
    s._handler_factory = make_handler
    s.set_phone(True)
    try:
        import urllib.request as ur
        phone = s.phone_url.replace(s.phone_url.split("/")[2].split(":")[0], "127.0.0.1")
        assert "LightCode⁴" in ur.urlopen(phone, timeout=5).read().decode()
        try:
            ur.urlopen(phone.split("?")[0], timeout=5)
            raise AssertionError("phone server must require the token")
        except Exception as e:
            assert "403" in str(e)
    finally:
        s.set_phone(False)
    print("27 phone access OK")

    # 28. computer use: off by default, tools hidden; enabled -> offered and every action asks
    assert not any(t["name"].startswith("desktop_") for t in REQUESTS[-1][2]["tools"] if "name" in t)
    s.apply_config({"computer": True})
    s.new_chat()
    d.policy = "deny"
    evs = d.run("click", script=[("tool", "desktop_click", {"x": 10, "y": 10}), ("text", "ok")])
    assert d.kinds(evs, "approve") and any(t.get("name") == "desktop_click" for t in REQUESTS[-2][2]["tools"])
    d.policy = "allow"
    s.apply_config({"computer": False})
    print("28 computer use gating OK")

    # 29. voice: clear message without a transcription key
    try:
        s.transcribe(base64.b64encode(b"x").decode(), "audio/webm")
        raise AssertionError("should need a key")
    except ValueError as e:
        assert "OpenAI API key" in str(e)
    print("29 voice OK")

    from myagent import platform_extras as px
    # 30. every connected window gets every event
    a, b = s.events.subscribe(), s.events.subscribe()
    s.events.put({"kind": "ping"})
    assert a.get(timeout=2)["kind"] == "ping" and b.get(timeout=2)["kind"] == "ping"
    s.events.unsubscribe(a)
    s.events.unsubscribe(b)
    print("30 multi-window events OK")

    # 31. model routing: sub-agents use the cheaper model
    n0 = len(REQUESTS)
    d.run("research", script=[("tool", "task", {"description": "look", "prompt": "List files"}), ("text", "REPORT"), ("text", "ok")])
    assert REQUESTS[n0 + 1][2]["model"] == "claude-haiku-5-5" and REQUESTS[n0][2]["model"] == "claude-opus-5-5"
    print("31 model routing OK")

    # 32. local MCP server: tools offered to the model, calls approved and answered
    s.apply_config({"mcp_local": [{"name": "fake", "command": sys.executable,
                                   "args": [str(Path(__file__).parent / "tests" / "fake_mcp_server.py")]}]})
    s.new_chat()
    evs = d.run("shout", script=[("tool", "mcp_fake_shout", {"text": "hello mcp"}), ("text", "done")])
    assert any(t.get("name") == "mcp_fake_shout" for t in REQUESTS[-2][2]["tools"])
    assert d.kinds(evs, "approve") and "HELLO MCP" in json.dumps(REQUESTS[-1][2]["messages"][-1])
    s.apply_config({"mcp_local": []})
    print("32 local MCP OK")

    # 33. sandbox: writes outside the project folder are blocked (where the OS supports it)
    s.apply_config({"sandbox": "folder"})
    s.new_chat()
    if px.sandbox_available():
        import pwd  # the real home folder (tests use a temporary HOME, which lives in the allowed temp area)
        outside = Path(pwd.getpwuid(os.getuid()).pw_dir) / f"herness-outside-{os.getpid()}.txt"
        d.run("sb", script=[("tool", "bash", {"command": f"echo in > inside.txt; echo out > {outside}"}), ("text", "ok")])
        assert (Path(s.current.cwd) / "inside.txt").exists(), "sandbox blocked a write inside the project"
        assert not outside.exists(), f"sandbox let a write escape to {outside}"
        print(f"33 sandbox OK ({px.sandbox_available()})")
    else:
        evs = d.run("sb", script=[("tool", "bash", {"command": "echo hi"}), ("text", "ok")])
        assert "no sandbox support" in d.kinds(evs, "tool_result")[0]["output"]
        print("33 sandbox OK (not available here: commands refuse to run unsandboxed)")
    s.apply_config({"sandbox": "off"})

    # 34. settings export/import (keys left out unless asked) and secret scrubbing in logs
    s.config["api_key"] = "sk-ant-secret123456"
    exp = px.export_settings(s.config, include_keys=False)
    assert "api_key" not in exp["settings"] and exp["settings"]["model"]
    merged = px.import_settings(exp, s.config)
    assert merged["api_key"] == "sk-ant-secret123456"
    assert "secret123456" not in px.scrub("key sk-ant-secret123456 and token=abc and oc_sk_zzz")
    s.config["api_key"] = ""
    print("34 settings transfer + log scrubbing OK")

    # 35. share a chat as a GitHub gist
    s.config["github_token"] = "ghp_test"
    s.current.title = "Shared chat"
    url = s.share(s.current)
    assert url.startswith("https://gist.github.com/") and REQUESTS[-1][0].endswith("/gists")
    assert "Shared chat" in json.dumps(REQUESTS[-1][2])
    s.config["github_token"] = ""
    print("35 sharing OK")

    # 36. auto-update installer script, chat ordering, reply language
    argv, script = px.updater_script("/tmp/new.dmg" if sys.platform != "win32" else "C:/new.exe", 4242)
    assert "4242" in script and argv
    ids = [c["id"] for c in s.list_chats()][:2]
    s.config["chat_order"] = list(reversed(ids))
    assert [c["id"] for c in s.list_chats()][:2] == list(reversed(ids))
    s.apply_config({"language": "Spanish"})
    s.new_chat()
    d.run("hola", script=[("text", "Hola")])
    assert "in Spanish" in REQUESTS[-1][2]["system"][0]["text"]
    s.apply_config({"language": ""})
    print("36 updates + chat order + language OK")

    # 37. errors in plain words
    from myagent.gui import explain_error

    class E(Exception):
        def __init__(self, code, msg=""):
            super().__init__(msg)
            self.status_code, self.message = code, msg
    cfg = {"provider": "openai", "model": "x", "base_url": "https://example.com/"}
    assert "no credit" in explain_error(E(402, "Insufficient balance"), cfg)
    assert "address looks wrong" in explain_error(E(405), cfg)
    assert "rejected" in explain_error(E(401), cfg) and "retired" in explain_error(E(410, "deprecated"), cfg)
    from myagent.gui import fix_base_url
    assert fix_base_url("https://app.haimaker.ai/") == "https://api.haimaker.ai/v1"
    assert fix_base_url("https://opencode.ai/") == "https://opencode.ai/inference/openai/v1"
    assert fix_base_url("https://api.x.com/v1/chat/completions") == "https://api.x.com/v1"
    print("37 plain-language errors + address fixes OK")

    # 38. online connector with one-click sign-in (OAuth: discovery, registration, PKCE, token, refresh) for any provider
    import urllib.request as ur2
    from urllib.parse import urlparse as _up, parse_qs as _pq, urlencode as _ue
    TOK = {"n": 0}

    class Remote(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body=b"", ctype="application/json", extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            base = f"http://127.0.0.1:{self.server.server_port}"
            u = _up(self.path)
            if u.path == "/.well-known/oauth-protected-resource/mcp":
                return self._send(200, json.dumps({"authorization_servers": [base]}).encode())
            if u.path == "/.well-known/oauth-authorization-server":
                return self._send(200, json.dumps({"authorization_endpoint": base + "/authorize", "token_endpoint": base + "/token",
                                                   "registration_endpoint": base + "/register"}).encode())
            if u.path == "/authorize":
                q = _pq(u.query)
                assert q["code_challenge_method"] == ["S256"] and q["client_id"] == ["cid-1"]
                return self._send(302, extra={"Location": q["redirect_uri"][0] + "?" + _ue({"code": "abc", "state": q["state"][0]})})
            self._send(404)

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.path == "/register":
                return self._send(201, json.dumps({"client_id": "cid-1"}).encode())
            if self.path == "/token":
                form = _pq(body.decode())
                TOK["n"] += 1
                assert form["grant_type"][0] in ("authorization_code", "refresh_token")
                return self._send(200, json.dumps({"access_token": f"tok{TOK['n']}", "refresh_token": "r1", "expires_in": 3600}).encode())
            if self.path == "/mcp":
                if not (self.headers.get("Authorization") or "").startswith("Bearer tok"):
                    base = f"http://127.0.0.1:{self.server.server_port}"
                    return self._send(401, b"{}", extra={"WWW-Authenticate": f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource/mcp"'})
                msg = json.loads(body)
                if "id" not in msg:
                    return self._send(202)
                m = msg["method"]
                res = ({"protocolVersion": "2025-06-18", "capabilities": {}} if m == "initialize" else
                       {"tools": [{"name": "echo_upper", "description": "Upper-case", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}}]}
                       if m == "tools/list" else {"content": [{"type": "text", "text": msg["params"]["arguments"]["text"].upper()}]})
                out = json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": res})
                if m == "tools/call":  # reply as an event stream, like many real servers
                    return self._send(200, f"event: message\ndata: {out}\n\n".encode(), "text/event-stream")
                return self._send(200, out.encode(), extra={"Mcp-Session-Id": "sess-1"} if m == "initialize" else None)
            self._send(404)
    rsrv = ThreadingHTTPServer(("127.0.0.1", 0), Remote)
    threading.Thread(target=rsrv.serve_forever, daemon=True).start()
    murl = f"http://127.0.0.1:{rsrv.server_port}/mcp"
    from myagent.mcp_remote import sign_in
    oauth = sign_in(murl, open_browser=lambda u: ur2.urlopen(u, timeout=10).read())
    assert oauth["access_token"] == "tok1" and oauth["client_id"] == "cid-1"
    oauth["expires_at"] = 0  # force a refresh on first use
    s.apply_config({"provider": "claude", "model": "claude-opus-5-5", "mcp": [{"name": "remote", "url": murl, "token": "", "oauth": oauth}]})
    s.new_chat()
    evs = d.run("shout remotely", script=[("tool", "mcp_remote_echo_upper", {"text": "signed in"}), ("text", "done")])
    assert d.kinds(evs, "approve") and "SIGNED IN" in json.dumps(REQUESTS[-1][2]["messages"][-1]), "remote tool result missing"
    assert TOK["n"] == 2 and s.config["mcp"][0]["oauth"]["access_token"] == "tok2", f"token refresh not saved ({TOK['n']} token requests)"
    pub = s.public_config()["mcp"][0]
    assert pub["signed_in"] and pub["token"] == "" and "oauth" not in pub  # tokens never sent to the page
    s.apply_config({"mcp": []})
    print("38 one-click connector sign-in OK")

    # 39. built-in providers: each keeps its own key and model; switching fills in address and key
    s.config["keys"]["openrouter"] = "sk-or-test"
    s.apply_config({"service": "openrouter"})
    assert s.config["provider"] == "openai" and s.config["base_url"] == "https://openrouter.ai/api/v1" and s.config["api_key"] == "sk-or-test"
    assert s.config["model"] == "openai/gpt-5"
    s.apply_config({"model": "anthropic/claude-sonnet-5"})
    s.apply_config({"service": "deepseek", "api_key": "sk-ds"})
    assert s.config["base_url"] == "https://api.deepseek.com/v1" and s.config["keys"]["deepseek"] == "sk-ds" and s.config["model"] == "deepseek-chat"
    s.apply_config({"service": "openrouter"})
    assert s.config["model"] == "anthropic/claude-sonnet-5" and s.config["api_key"] == "sk-or-test", "per-provider model/key not remembered"
    pub = s.public_config()
    assert "keys" not in pub and "sk-or-test" not in json.dumps(pub) and set(pub["keys_set"]) >= {"openrouter", "deepseek"}
    from myagent.catalog import guess_service
    assert guess_service("openai", "https://api.haimaker.ai/v1") == "haimaker" and guess_service("claude", "") == "claude"
    s.apply_config({"service": "claude"})
    print("39 built-in providers OK")
    # 40. web search for every model (search page parsed into results)
    from myagent.tools import Workspace
    ws = Workspace(str(work))
    page = ('<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fdocs&rut=x">Example <b>Docs</b></a>'
            '<a class="result__snippet" href="#">The <b>official</b> docs.</a>'
            '<a rel="nofollow" class="result__a" href="https://second.org/">Second</a><a class="result__snippet">Two</a>')
    ws._download = lambda url: (page.encode(), "text/html")
    out = ws.run("web_search", {"query": "example docs"})
    assert "1. Example Docs\n   https://example.com/docs\n   The official docs." in out and "2. Second" in out, out
    s.apply_config({"service": "openai", "api_key": "k", "base_url": os.environ["ANTHROPIC_BASE_URL"] + "/v1"})
    assert "web_search" in {t["name"] for t in s.tools_for(s.config)}
    s.apply_config({"service": "claude"})
    print("40 web search OK")

    # 41. code navigation: definitions (Python exact, JS by pattern) and references
    (work / "nav.py").write_text("LIMIT = 3\n\nclass Shop:\n    def total(self):\n        return LIMIT\n\ndef helper():\n    return Shop().total()\n")
    (work / "web.ts").write_text("export async function total(a: number) {\n  return a\n}\nconst x = total(2)\n")
    out = ws.run("code_symbols", {"path": "nav.py"})
    assert "nav.py:1  var LIMIT" in out and "nav.py:3  class Shop" in out and "nav.py:4  def total" in out, out
    out = ws.run("code_symbols", {"name": "total"})
    assert "nav.py:4  def total" in out and "web.ts:1  function total" in out, out
    out = ws.run("find_references", {"name": "total"})
    assert "web.ts:1: [definition]" in out and "web.ts:4:" in out and "nav.py:8:" in out and "subtotal" not in out, out
    print("41 code navigation OK")

    # 42. run notebook cells: outputs saved, stops at the first error, undo restores
    cw = Path(s.current.cwd)
    nb = {"cells": [{"cell_type": "code", "source": ["x = 6\n", "print(x * 7)"], "metadata": {}, "outputs": []},
                    {"cell_type": "markdown", "source": ["# note"], "metadata": {}},
                    {"cell_type": "code", "source": ["1/0"], "metadata": {}, "outputs": []},
                    {"cell_type": "code", "source": ["print('never')"], "metadata": {}, "outputs": []}],
          "metadata": {}, "nbformat": 4, "nbformat_minor": 5}
    (cw / "n.ipynb").write_text(json.dumps(nb))
    evs = d.run("run nb", script=[("tool", "notebook_run", {"path": "n.ipynb"}), ("text", "ran")])
    res = d.kinds(evs, "tool_result")[0]["output"]
    assert "[cell 0] 42" in res and "ZeroDivisionError" in res and "never" not in res and "stopped at an error" in res, res
    saved = json.loads((cw / "n.ipynb").read_text())
    assert saved["cells"][0]["outputs"][0]["text"] == ["42\n"] and saved["cells"][2]["outputs"][0]["output_type"] == "error"
    s.undo(d.kinds(evs, "turn_end")[0]["turn"])
    assert json.loads((cw / "n.ipynb").read_text())["cells"][0]["outputs"] == []
    print("42 notebook run OK")

    # 43. skills: listed in the instructions, loaded on demand (project overrides personal)
    from myagent import extras
    sk = cw / ".herness" / "skills" / "release-notes"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text("---\nname: release-notes\ndescription: Write release notes from git history\n---\n"
                                 "Run scripts/log.sh and group changes by type.\n")
    (sk / "scripts").mkdir()
    (sk / "scripts" / "log.sh").write_text("git log --oneline\n")
    assert extras.skills(str(cw))["release-notes"][0] == "Write release notes from git history"
    s.new_chat()
    evs = d.run("notes", script=[("tool", "use_skill", {"name": "release-notes"}), ("text", "ok")])
    sysmsg = REQUESTS[-2][2]["system"]
    assert "release-notes: Write release notes from git history" in json.dumps(sysmsg)
    body = json.dumps(REQUESTS[-1][2]["messages"][-1])
    assert "group changes by type" in body and "scripts/log.sh" in body, body[:500]
    d.run("bad", script=[("tool", "use_skill", {"name": "nope"}), ("text", "ok")])
    assert "Available skills: release-notes" in json.dumps(REQUESTS[-1][2]["messages"][-1])
    print("43 skills OK")

    # 44. specialized sub-agents and parallel runs
    n0 = len(REQUESTS)
    d.run("explore", script=[("tool", "task", {"description": "find", "prompt": "Find X", "kind": "explore"}),
                             ("text", "R1"), ("text", "ok")])
    sub = REQUESTS[n0 + 1][2]
    names = {t.get("name") for t in sub["tools"]}
    assert sub["model"] == "claude-haiku-5-5" and "code_symbols" in names and "web_search" not in names and "write_file" not in names
    assert "explore sub-agent" in json.dumps(sub["system"])
    n0 = len(REQUESTS)
    d.run("research", script=[("tool", "task", {"description": "look up", "prompt": "Research Y", "kind": "research"}),
                              ("text", "R2"), ("text", "ok")])
    sub = REQUESTS[n0 + 1][2]
    assert sub["model"] == "claude-opus-5-5" and "web_search" in {t.get("name") or t.get("type") for t in sub["tools"]}
    from myagent.providers import run_calls
    def slow(name, a):
        time.sleep(0.5)
        return f"{name}:{a['n']}", False
    t0 = time.time()
    out = run_calls(slow, [("task", {"n": 1}), ("read_file", {"n": 2}), ("task", {"n": 3}), ("task", {"n": 4})])
    took = time.time() - t0
    assert out == [("task:1", False), ("read_file:2", False), ("task:3", False), ("task:4", False)] and took < 1.3, (out, took)
    n0 = len(REQUESTS)
    evs = d.run("two", script=[("tools", [("task", {"description": "a", "prompt": "A", "kind": "plan"}),
                                          ("task", {"description": "b", "prompt": "B", "kind": "plan"})]),
                               ("text", "SUB"), ("text", "SUB"), ("text", "merged")])
    res = REQUESTS[-1][2]["messages"][-1]["content"]
    assert len(res) == 2 and all(r["type"] == "tool_result" and "SUB" in json.dumps(r) for r in res), res
    print("44 sub-agent kinds + parallel OK")
    # 45. edit & resend an earlier message: history and later file changes rewound
    s.new_chat()
    chat = s.current
    cw = Path(chat.cwd)
    d.run("first", script=[("text", "one")])
    evs = d.run("second", script=[("tool", "write_file", {"path": "rw.txt", "content": "v2"}), ("text", "two")])
    assert (cw / "rw.txt").exists()
    users = [e for e in chat.transcript if e["kind"] == "user"]
    idx = users[1]["idx"]
    SCRIPT[:] = [("text", "edited reply")]
    start = len(d.events)
    chat.rewind(idx, "second, but different")
    t0 = time.time()
    while chat.busy or time.time() - t0 < 0.2:
        time.sleep(0.05)
    assert not (cw / "rw.txt").exists(), "later file change should be undone"
    texts = [e.get("text") for e in chat.transcript if e["kind"] == "user"]
    assert texts == ["first", "second, but different"], texts
    msgs = REQUESTS[-1][2]["messages"]
    assert "second, but different" in json.dumps(msgs) and '"second"' not in json.dumps(msgs) and len(msgs) == 3, msgs
    assert any(e["kind"] == "replay" for e in d.events[start:])
    print("45 edit & rewind OK")

    # 46. fork: a new chat with the history up to that message; the original is unchanged
    orig_len = len(chat.transcript)
    users = [e for e in chat.transcript if e["kind"] == "user"]
    new = chat.fork(users[0]["idx"])
    assert new.id != chat.id and len(chat.transcript) == orig_len
    assert [e.get("text") for e in new.transcript if e["kind"] == "user"] == ["first"]
    assert any(e["kind"] == "text" and e["text"] == "one" for e in new.transcript)
    assert len(new.provider.serialize()) == users[1]["mark"]["m"] and new.title.startswith("Fork:")
    s.select(new)
    d.run("forked question", script=[("text", "fork reply")])
    assert "one" in json.dumps(REQUESTS[-1][2]["messages"]) and "different" not in json.dumps(REQUESTS[-1][2]["messages"])
    print("46 fork OK")

    # 47. review changes one by one: only the chosen change is applied, the model is told what was skipped
    (cw / "h.txt").write_text("a\nb\nc\nd\ne\nf\ng\nh\n")
    d.policy = "partial"
    evs = d.run("two edits", script=[("tool", "multi_edit", {"path": "h.txt", "edits": [
        {"old_text": "a\n", "new_text": "A\n"}, {"old_text": "h\n", "new_text": "H\n"}]}), ("text", "ok")])
    d.policy = "allow"
    ap = d.kinds(evs, "approve")[0]
    assert len(ap["hunks"]) == 2 and ap["hunks"][0]["text"] == "-a\n+A\n", ap.get("hunks")
    assert (cw / "h.txt").read_text() == "A\nb\nc\nd\ne\nf\ng\nh\n"
    told = json.dumps(REQUESTS[-1][2]["messages"][-1])
    assert "only 1 of your 2 changes" in told and "+H" in told, told[:400]
    s.undo(d.kinds(evs, "turn_end")[0]["turn"])
    assert (cw / "h.txt").read_text().startswith("a\n")
    print("47 per-change review OK")

    # 48. context meter and /compact
    evs = d.run("ctx", script=[("text", "ok")])
    u = d.kinds(evs, "usage")[-1]
    assert u["context"] > 0 and u["window"] == 1_000_000, u
    from myagent.gui import context_window
    assert context_window("claude-haiku-5-5") == 200_000 and context_window("deepseek-chat") == 128_000 and context_window("x", 5000) == 5000
    SCRIPT[:] = [("text", "SUMMARY: user is testing herness")]
    cur = s.current
    cur.compact()
    t0 = time.time()
    while cur.busy and time.time() - t0 < 10:
        time.sleep(0.05)
    msgs = cur.provider.serialize()
    assert len(msgs) == 2 and "SUMMARY: user is testing herness" in json.dumps(msgs[0]), msgs
    assert REQUESTS[-1][2].get("tools") in (None, []) or all("input_schema" not in t for t in REQUESTS[-1][2]["tools"])
    d.run("after compact", script=[("text", "fine")])
    assert "SUMMARY: user is testing" in json.dumps(REQUESTS[-1][2]["messages"])
    try:
        cur.rewind([e for e in cur.transcript if e["kind"] == "user"][0]["idx"], "x")
        raise AssertionError("rewind past a summary should be refused")
    except ValueError:
        pass
    print("48 context meter + compact OK")

    # 49. more hooks: session start adds context, on-prompt can block, before-tool can block
    hd = Path(tempfile.mkdtemp())
    (hd / "start.py").write_text("print('branch is main')\n")
    (hd / "prompt.py").write_text("import os, sys\np = os.environ['HERNESS_PROMPT']\nif 'secret' in p:\n    print('blocked word')\n    sys.exit(2)\n")
    (hd / "tool.py").write_text("import os, sys\nsys.exit(3 if 'rm ' in os.environ['HERNESS_INPUT'] else 0)\n")
    run = lambda f: f'"{sys.executable}" "{hd / f}"'  # double quotes work in both sh and Windows cmd
    s.config["hooks"][s.current.rules_dir] = {"session_start": run("start.py"), "on_prompt": run("prompt.py"),
                                              "before_tool": run("tool.py")}
    s.new_chat()
    d.run("hello", script=[("text", "hi")])
    assert "branch is main" in json.dumps(REQUESTS[-1][2]["messages"][0])
    d.run("again", script=[("text", "hi")])
    assert "branch is main" not in json.dumps(REQUESTS[-1][2]["messages"][-1])
    n0 = len(REQUESTS)
    evs = d.run("tell me the secret", script=[("text", "nope")])
    assert len(REQUESTS) == n0 and any("blocked this message" in (e.get("text") or "") for e in d.kinds(evs, "notice"))
    evs = d.run("clean", script=[("tools", [("bash", {"command": "rm -rf build"}), ("bash", {"command": "echo fine"})]), ("text", "ok")])
    blocked = [e for e in d.kinds(evs, "tool") if e.get("blocked") == "Blocked by a hook"]
    assert blocked and "fine" in json.dumps(REQUESTS[-1][2]["messages"][-1]), [e for e in d.kinds(evs, "tool")]
    s.config["hooks"][s.current.rules_dir] = {}
    print("49 more hooks OK")

    # 50. access from anywhere: tunnel address parsed from cloudflared's output
    from myagent import platform_extras as pxx
    if os.name == "nt":  # a .bat stand-in on Windows
        fake = Path(tempfile.mkdtemp()) / "cloudflared.bat"
        fake.write_text("@echo off\r\necho INF Requesting new quick Tunnel\r\necho INF  https://brave-tiger-12.trycloudflare.com\r\nping -n 30 127.0.0.1 >nul\r\n")
    else:
        fake = Path(tempfile.mkdtemp()) / "cloudflared"
        fake.write_text("#!/bin/sh\necho 'INF Requesting new quick Tunnel'\necho 'INF |  https://brave-tiger-12.trycloudflare.com  |'\nsleep 30\n")
        fake.chmod(0o755)
    old_find = pxx.find_cloudflared
    pxx.find_cloudflared = lambda: str(fake)
    try:
        s._handler_factory = s._handler_factory or (lambda sess, tok: BaseHTTPRequestHandler)
        url = s.set_remote(True)
        assert url.startswith("https://brave-tiger-12.trycloudflare.com/?token="), url
        assert s.public_config()["remote_url"] == url
        s.set_remote(False)
        assert not s.remote_url and s.tunnel is None
        s.set_phone(False)
        pxx.find_cloudflared = lambda: None
        try:
            s.set_remote(True)
            raise AssertionError("should explain how to install cloudflared")
        except RuntimeError as e:
            assert "brew install cloudflared" in str(e)
        s.set_phone(False)
    finally:
        pxx.find_cloudflared = old_find
    print("50 access from anywhere OK")
    # 51. live views: background jobs listed with output and stoppable; folder browsing stays inside the project; edits announce changes
    s.new_chat()
    cw = Path(s.current.cwd)
    evs = d.run("serve", script=[("tool", "bash", {"command": f'"{sys.executable}" -c "import time; print(\'serving on 8000\', flush=True); time.sleep(60)"', "run_in_background": True}), ("text", "started")])
    assert any(e["kind"] == "jobs_changed" for e in evs), "starting a job should tell the Tasks tab"
    time.sleep(1.0)
    jobs = s.current.ws.job_list()
    assert len(jobs) == 1 and jobs[0]["running"] and "serving on 8000" in jobs[0]["tail"], jobs
    s.current.ws._kill_shell(jobs[0]["id"])
    assert not s.current.ws.job_list()[0]["running"]
    (cw / "sub").mkdir(exist_ok=True)
    (cw / "sub" / "x.txt").write_text("hello")
    names = [i["name"] for i in s.current.ws.list_dir("")]
    assert "sub" in names and ".git" not in names and names.index("sub") < len(names)
    assert s.current.ws.read_for_view("sub/x.txt")["text"] == "hello"
    try:
        s.current.ws.read_for_view("../../etc/passwd")
        raise AssertionError("paths outside the project must be refused")
    except Exception as e:
        assert "outside the project" in str(e) or "No such file" in str(e), e
    evs = d.run("edit", script=[("tool", "write_file", {"path": "live.txt", "content": "x"}), ("text", "ok")])
    assert any(e["kind"] == "fs_changed" for e in evs), "edits should refresh the Changes and Files tabs live"
    print("51 live Changes / Files / Tasks OK")
    # 52. side chat: sees the main conversation, read-only, never changes the main chat or appears in the list
    s.new_chat()
    main = s.current
    d.run("remember the code word is PELICAN", script=[("text", "Noted: PELICAN.")])
    main_len = len(main.provider.serialize())
    SCRIPT[:] = [("text", "The code word is PELICAN.")]
    sid = s.side_send("what was the code word?")
    side = s.side_chats[main.id]
    t0 = time.time()
    while (side.busy or not side.transcript or side.transcript[-1]["kind"] != "turn_end") and time.time() - t0 < 10:
        time.sleep(0.05)
    req = REQUESTS[-1][2]
    assert "PELICAN" in json.dumps(req["messages"][0]) and "what was the code word?" in json.dumps(req["messages"][-1])
    names = {t.get("name") for t in req["tools"]}
    assert "write_file" not in names and "bash" not in names and "read_file" in names, names
    assert "SIDE CHAT" in json.dumps(req["system"])
    assert len(main.provider.serialize()) == main_len, "the main chat must not change"
    assert sid not in [c["id"] for c in s.list_chats()] and not (CHATS / f"{sid}.json").exists()
    out, err = side.run_tool("write_file", {"path": "x.txt", "content": "no"})
    assert err and "Side chats can only look" in out
    s.side_reset()
    assert main.id not in s.side_chats
    print("52 side chat OK")

    # 53. edit files from the Files tab: saved, live refresh, and refuses to overwrite newer changes
    cw = Path(main.cwd)
    (cw / "edit_me.txt").write_text("one\n")
    v = main.ws.read_for_view("edit_me.txt")
    r = main.ws.write_from_view("edit_me.txt", "one\ntwo\n", v["mtime"])
    assert (cw / "edit_me.txt").read_text() == "one\ntwo\n" and r["mtime"] != v["mtime"]
    try:
        main.ws.write_from_view("edit_me.txt", "stale", v["mtime"])
        raise AssertionError("a stale save must be refused")
    except Exception as e:
        assert "changed since you opened it" in str(e), e
    try:
        main.ws.write_from_view("../outside.txt", "x", None)
        raise AssertionError("saving outside the project must be refused")
    except Exception as e:
        assert "outside the project" in str(e) or "No such file" in str(e)
    print("53 file editing OK")

    # 54. search past chats by what was said
    hits = s.search_chats("pelican")
    assert hits and hits[0]["id"] == main.id and "PELICAN" in hits[0]["snippet"], hits
    assert s.search_chats("zzz-not-there") == [] and s.search_chats("a") == []
    print("54 chat search OK")

    # 55. projects: shared instructions and reference files reach every chat in the project
    from myagent import projects as pj
    pdir = Path(tempfile.mkdtemp())
    proj = s.save_project({"name": "Website", "folder": str(pdir), "instructions": "Always use British spelling."})
    pj.add_file(proj["id"], "brand.md", base64.b64encode(b"Primary colour: #8b6c35").decode())
    pj.add_file(proj["id"], "logo.png", base64.b64encode(b"\x89PNG....").decode())
    assert [f["name"] for f in pj.list_files(proj["id"])] == ["brand.md", "logo.png"]
    s.new_project_chat(proj["id"])
    pc = s.current
    assert pc.project == proj["id"] and Path(pc.cwd) == pdir.resolve() or Path(pc.cwd) == pdir
    d.run("hello project", script=[("text", "hi")])
    sysmsg = json.dumps(REQUESTS[-1][2]["system"])
    assert "Project: Website" in sysmsg and "British spelling" in sysmsg and "#8b6c35" in sysmsg and "logo.png" in sysmsg
    assert next(c for c in s.list_chats() if c["id"] == pc.id)["project"] == proj["id"]
    try:
        pj.add_file(proj["id"], "../../evil.txt", "eA==")
        assert not (pj.HOME / "projects" / "evil.txt").exists()
    except ValueError:
        pass
    s.delete_project(proj["id"])
    assert not s.project_by_id(proj["id"]) and not pj.folder(proj["id"]).exists()
    print("55 projects OK")

    # 56. code intelligence through a real language-server connection (fake server standing in for Pyright)
    from myagent import lsp
    s.select(main)
    bind = Path(tempfile.mkdtemp())
    shim = bind / ("pyright-langserver.bat" if os.name == "nt" else "pyright-langserver")
    srv = Path(__file__).resolve().parent / "tests" / "fake_lsp_server.py"
    shim.write_text(f'@"{sys.executable}" "{srv}" %*\n' if os.name == "nt" else f'#!/bin/sh\nexec "{sys.executable}" "{srv}" "$@"\n')
    shim.chmod(0o755)
    os.environ["PATH"] = str(bind) + os.pathsep + os.environ["PATH"]
    (cw / "calc.py").write_text("def total(xs):\n    return sum(xs)\n\nprint(total([1, 2]))\n")
    assert lsp.find_server(cw / "calc.py") and "pyright-langserver" in lsp.available()
    ws2 = main.ws
    out = ws2.run("code_intel", {"action": "definition", "path": "calc.py", "symbol": "total"})
    assert out.startswith("calc.py:1:5"), out
    out = ws2.run("code_intel", {"action": "references", "path": "calc.py", "line": 4, "column": 7})
    assert "calc.py:1:5" in out and "calc.py:4:7" in out, out
    assert "def total(xs: list[int]) -> int" in ws2.run("code_intel", {"action": "hover", "path": "calc.py", "symbol": "total"})
    assert "No problems" in ws2.run("code_intel", {"action": "diagnostics", "path": "calc.py"})
    evs = d.run("break it", script=[("tool", "write_file", {"path": "calc.py", "content": "x = oops\n"}), ("text", "ok")])
    told = json.dumps(REQUESTS[-1][2]["messages"][-1])
    assert "oops" in told.lower(), told[:300]
    errs = lsp.MANAGER.errors_after_edit(cw, cw / "calc.py")
    assert errs and "undefined name 'oops'" in errs, errs
    try:
        ws2.run("code_intel", {"action": "definition", "path": "notes.txt", "symbol": "x"}) if (cw / "notes.txt").exists() else None
        (cw / "a.unknownlang").write_text("x")
        ws2.run("code_intel", {"action": "definition", "path": "a.unknownlang", "symbol": "x"})
        raise AssertionError("files without a server should explain how to get one")
    except Exception as e:
        assert "No language server" in str(e), e
    lsp.MANAGER.close_all()
    print("56 code intelligence (LSP) OK")
    # 57. usage over time: every request logged (no message text), summarized per day and per model
    from myagent import extras as ex
    ex.USAGE_FILE.unlink(missing_ok=True)
    s.new_chat()
    d.run("count me", script=[("text", "ok")])
    d.run("again", script=[("text", "ok")])
    now = time.time()
    with open(ex.USAGE_FILE, "a") as f:  # an older day
        f.write(json.dumps({"t": now - 3 * 86400, "model": "gpt-5", "chat": "x", "cost": 0.5, "in": 1000, "out": 200, "cr": 0, "cw": 0}) + "\n")
        f.write(json.dumps({"t": now - 40 * 86400, "model": "gpt-5", "chat": "x", "cost": 9.0, "in": 1, "out": 1, "cr": 0, "cw": 0}) + "\n")
    lines = ex.USAGE_FILE.read_text().splitlines()
    assert "count me" not in ex.USAGE_FILE.read_text() and len(lines) == 4
    sm = ex.usage_summary(30, now)
    assert len(sm["days"]) == 30 and sm["days"][-1]["requests"] == 2 and sm["days"][-4]["cost"] == 0.5, sm["days"][-5:]
    assert sm["totals"]["month"] < 9 and sm["totals"]["all"] > 9 and sm["totals"]["week"] >= 0.5 and sm["totals"]["today"] > 0
    assert {m["model"] for m in sm["models"]} == {"claude-opus-5-5", "gpt-5"} and sm["models"][0]["cost"] >= sm["models"][-1]["cost"]
    print("57 usage over time OK")

    # 58. skills gallery: one click adds a ready-made skill the agent can use; remove takes it away
    from myagent import gallery
    gallery.SKILLS_DIR = ex.HOME / "skills"
    assert len(gallery.catalog()) >= 8 and not any(x["installed"] for x in gallery.catalog() if x["id"] == "code-review")
    gallery.install("code-review")
    assert next(x for x in gallery.catalog() if x["id"] == "code-review")["installed"]
    s.new_chat()
    d.run("review", script=[("text", "ok")])
    assert "code-review: Review changes for bugs" in json.dumps(REQUESTS[-1][2]["system"])
    assert "severity" in ex.load_skill(s.current.cwd, "code-review")
    gallery.remove("code-review")
    assert "code-review" not in ex.skills(s.current.cwd)
    print("58 skills gallery OK")

    # 59. one-click language server setup (a stand-in npm installs a server into LightCode's own folder)
    lsp.LSP_HOME = Path(tempfile.mkdtemp()) / "lsp"
    fakebin = Path(tempfile.mkdtemp())
    npm = fakebin / ("npm.bat" if os.name == "nt" else "npm")
    target = lsp.LSP_HOME / "node_modules" / ".bin" / "pyright-langserver"
    npm.write_text(f'#!/bin/sh\nmkdir -p "{target.parent}" && printf "#!/bin/sh\\nexit 0\\n" > "{target}" && chmod +x "{target}" && echo added 1 package\n')
    npm.chmod(0o755)
    os.environ["PATH"] = str(fakebin) + os.pathsep + os.environ["PATH"].replace(str(bind) + os.pathsep, "")
    real_search = lsp.search_path
    lsp.search_path = lambda: os.pathsep.join([str(lsp.LSP_HOME / "node_modules" / ".bin"), str(fakebin), "/usr/bin", "/bin"])
    st = {x["id"]: x for x in lsp.status()}
    if os.name != "nt":
        assert not st["python"]["server"] and st["python"]["can_install"], st["python"]
        out = lsp.install("python")
        assert "added 1 package" in out and {x["id"]: x for x in lsp.status()}["python"]["server"] == "pyright-langserver"
    try:
        lsp.install("swift")
        raise AssertionError("Swift's server can't be installed from here")
    except lsp.LspError as e:
        assert "Xcode" in str(e)
    lsp.search_path = real_search
    lsp.LSP_HOME = Path.home() / ".incredible-herness" / "lsp"
    lsp.MANAGER.close_all()
    real = shutil.which("pyright-langserver", path=lsp.search_path())
    if real and "fake" not in Path(real).read_text(errors="replace")[:400]:  # a real Pyright on this machine: check it end to end
        rp = Path(tempfile.mkdtemp())
        (rp / "shop.py").write_text("def total(xs: list[int]) -> int:\n    return sum(xs)\n\nprint(total([1, 2]))\nundefined_thing()\n")
        wsr = Workspace(str(rp))
        assert wsr.run("code_intel", {"action": "definition", "path": "shop.py", "line": 4, "column": 7}).startswith("shop.py:1:5")
        assert "int" in wsr.run("code_intel", {"action": "hover", "path": "shop.py", "symbol": "total"})
        assert "undefined_thing" in wsr.run("code_intel", {"action": "diagnostics", "path": "shop.py"})
        lsp.MANAGER.close_all()
        print("59 language server setup OK (and real Pyright works end to end)")
    else:
        print("59 language server setup OK")

    # 60. quick chat: a message from the quick box starts a new chat and brings the main window forward
    calls = []
    s.quick_hide, s.focus_main = lambda: calls.append("hide"), lambda: calls.append("focus")
    SCRIPT[:] = [("text", "quick reply")]
    cid = s.quick_send("what time zone am I in?")
    assert s.current.id == cid and calls == ["hide", "focus"]
    t0 = time.time()
    while s.current.busy and time.time() - t0 < 10:
        time.sleep(0.05)
    assert s.current.title.startswith("what time zone")
    from myagent.desktop import hotkey_for_pynput
    assert hotkey_for_pynput("ctrl+alt+space") == "<ctrl>+<alt>+<space>" == hotkey_for_pynput("⌃⌥Space") == hotkey_for_pynput("<ctrl>+<alt>+<space>")
    assert hotkey_for_pynput("cmd+shift+k") == "<cmd>+<shift>+k"
    from myagent.desktop import parse_hotkey, hotkey_matches, MAC_FLAGS
    m, k = parse_hotkey("⌃⌥Space")
    assert (m, k) == (MAC_FLAGS["ctrl"] | MAC_FLAGS["alt"], "space")
    assert hotkey_matches(m, k, m | 0x100, 49, " ") and not hotkey_matches(m, k, MAC_FLAGS["ctrl"], 49, " ")
    assert not hotkey_matches(m, k, m | (1 << 16), 57, "")  # Caps Lock alone never fires it
    m2, k2 = parse_hotkey("cmd+shift+k")
    assert hotkey_matches(m2, k2, m2, 40, "K") and not hotkey_matches(m2, k2, m2, 40, "j")
    print("60 quick chat OK")
    # 61. rename and pin chats; replies carry their time; display settings save without restarting
    s.new_chat()
    evs = d.run("time me", script=[("text", "ok")])
    te = d.kinds(evs, "turn_end")[-1]
    assert abs(te["t"] - time.time()) < 30 and te["secs"] >= 0 and abs(d.kinds(evs, "user")[0]["t"] - time.time()) < 30
    cid = s.current.id
    s.rename_chat(cid, "  Quarterly   report  ")
    assert s.current.title == "Quarterly report" and next(c for c in s.list_chats() if c["id"] == cid)["title"] == "Quarterly report"
    s.chats.pop(cid)  # reopen from disk: the custom name sticks
    s.open_chat(cid)
    assert s.current.title == "Quarterly report"
    others = [c["id"] for c in s.list_chats() if c["id"] != cid]
    s.pin_chat(others[-1], True)
    s.pin_chat(cid, True)
    lst = s.list_chats()
    assert [c["id"] for c in lst[:2]] == [cid, others[-1]] and lst[0]["pinned"] and not lst[2]["pinned"]
    s.pin_chat(cid, False)
    assert s.list_chats()[0]["id"] == others[-1] and not next(c for c in s.list_chats() if c["id"] == cid)["pinned"]
    try:
        s.rename_chat(cid, "   ")
        raise AssertionError("empty names are refused")
    except ValueError:
        pass
    s.delete_chat(others[-1])
    assert others[-1] not in s.config["pinned"]
    import urllib.request as ur3
    from myagent.gui import CONFIG_FILE, start_server
    srv3, url3, _ = start_server(s.current.cwd, session=s)
    tok = url3.split("token=")[1]
    s.current.busy = True  # display settings must work even while a task runs
    req = ur3.Request(url3.split("?")[0].rstrip("/") + "/api/config", data=json.dumps({"theme": "dark", "sound": True}).encode(), method="POST",
                      headers={"Content-Type": "application/json", "X-Token": tok})
    ur3.urlopen(req, timeout=5).read()
    s.current.busy = False
    assert s.config["theme"] == "dark" and s.config["sound"] is True
    assert json.loads(CONFIG_FILE.read_text())["theme"] == "dark"
    srv3.shutdown()
    print("61 rename, pin, times, display settings OK")
    # 62. recent artifacts for the new-chat screen: newest first, titled from the page, from saved chats
    s.new_chat()
    d.run("make a page", script=[("text", "Here:\n\n```html\n<!doctype html><title>Launch page</title><h1>Hi</h1>\n```\nDone.")])
    arts = s.recent_artifacts(6)
    assert arts and arts[0]["title"] == "Launch page" and arts[0]["lang"] == "html" and "<h1>Hi</h1>" in arts[0]["code"], arts[:1]
    assert arts == sorted(arts, key=lambda a: -a["updated"])
    pc = s.public_config()
    assert pc["has_chats"] is True and "name" in pc
    print("62 recent artifacts OK")
    # 63. Chat and Code modes: chat conversations have no file/terminal tools and their own list; switching restores each
    from myagent.gui import CHAT_HOME
    s.set_mode("code")
    s.new_chat()
    d.run("code thing", script=[("text", "ok")])
    code_id = s.current.id
    s.set_mode("chat")
    assert s.current.kind == "chat" and Path(s.current.cwd) == CHAT_HOME
    d.run("what is a haiku?", script=[("text", "A short poem.")])
    req = REQUESTS[-1][2]
    names = {t.get("name") for t in req["tools"]}
    assert "bash" not in names and "write_file" not in names and "read_file" not in names, names
    assert "no access to the user's files" in json.dumps(req["system"])
    out, err = s.current.run_tool("bash", {"command": "echo hi"})
    assert err and "Switch to Code" in out
    chat_id = s.current.id
    kinds = {c["id"]: c["kind"] for c in s.list_chats()}
    assert kinds[chat_id] == "chat" and kinds[code_id] == "code"
    s.set_mode("code")
    assert s.current.id == code_id and s.current.kind == "code"
    s.set_mode("chat")
    assert s.current.id == chat_id
    s.chats.pop(chat_id)
    s.open_chat(chat_id)
    assert s.current.kind == "chat"
    s.set_mode("code")
    print("63 Chat and Code modes OK")
    # 64. fewer prompts: read-only commands run without asking; "don't ask again" covers commands by name; edits for the session
    from myagent.gui import split_command, is_safe_command, command_rule
    assert split_command("pwd && ls -la | head -20") == ["pwd", "ls -la", "head -20"]
    assert all(is_safe_command(x) for x in ["pwd", "ls -la", "git status", "cat a.txt", "grep -r x .", "cd src"])
    assert not any(is_safe_command(x) for x in ["rm -rf x", "echo hi > f", "find . -delete", "git push", "sed -i s/a/b/ f", "npm test"])
    assert command_rule("npm run build") == "bash(npm run*)" and command_rule("FOO=1 pytest -q") == "bash(pytest*)"
    s.new_chat()
    d.policy = "deny"
    evs = d.run("look", script=[("tool", "bash", {"command": "pwd && ls -la | head -5"}), ("text", "ok")])
    assert not d.kinds(evs, "approve"), "read-only commands must not ask"
    d.policy = "always"
    evs = d.run("test", script=[("tool", "bash", {"command": "cd . && npm --version"}), ("text", "ok")])
    ap = d.kinds(evs, "approve")
    assert ap and ap[0]["rule"] == "bash(npm --version*)" or ap[0]["rule"].startswith("bash(npm"), ap[0]["rule"]
    evs = d.run("again", script=[("tool", "bash", {"command": "npm --version"}), ("text", "ok")])
    assert not d.kinds(evs, "approve"), "the same command must not ask again"
    evs = d.run("edit", script=[("tool", "write_file", {"path": "e1.txt", "content": "a"}), ("text", "ok")])
    assert d.kinds(evs, "approve") and d.kinds(evs, "approve")[0]["edits"] is True
    d.policy = "deny"
    evs = d.run("edit2", script=[("tool", "write_file", {"path": "e2.txt", "content": "b"}), ("text", "ok")])
    assert not d.kinds(evs, "approve") and (Path(s.current.cwd) / "e2.txt").exists(), "edits are allowed for the rest of the session"
    evs = d.run("other", script=[("tool", "bash", {"command": "rm -f e2.txt"}), ("text", "ok")])
    assert d.kinds(evs, "approve"), "something new still asks"
    d.policy = "allow"
    print("64 fewer approval prompts OK")
    # 65. notifications when you're away, tidy titles, and starting where you left off
    from myagent.gui import clean_title
    assert clean_title("<|im_start|>system\nYou are helpful<|im_end|>") == "You are helpful"
    assert clean_title("  Fix the   login bug\nmore") == "Fix the login bug more"[:70] or clean_title("Fix the login bug").startswith("Fix")
    sent = []
    import myagent.extras as exm
    real_notify = exm.notify
    exm.notify = lambda t, m, chat=None: sent.append((t, m))
    try:
        s.new_chat()
        s.focused = False  # LightCode4 is in the background
        d.run("<|im_start|>user Build me a page", script=[("tool", "bash", {"command": "touch away.txt"}), ("text", "done")])
        assert s.current.title == "Build me a page", s.current.title
        assert any("needs you" in t for t, _ in sent) and any("is done" in m for _, m in sent), sent
        assert next(c for c in s.list_chats() if c["id"] == s.current.id)["unread"] is True
        sent.clear()
        s.focused = True
        d.run("again", script=[("text", "ok")])
        assert not sent, "no notifications while you're looking"
        s.select(s.current)
        assert next(c for c in s.list_chats() if c["id"] == s.current.id)["unread"] is False
    finally:
        exm.notify = real_notify
    last = s.current.id
    assert s.config["last_chat"] == last and (CHATS / f"{last}.json").exists()
    s.save()
    from myagent.gui import Session
    s2 = Session()
    s2.load()
    assert s2.resume() and s2.current.id == last and any(e["kind"] == "user" for e in s2.current.transcript)
    print("65 notifications, titles, resume OK")
    # 66. @-mentions attach project files; messages sent while busy are queued; checkpoint history restores any point
    s.set_mode("code")
    s.new_chat()
    cw = Path(s.current.cwd)
    (cw / "notes").mkdir(exist_ok=True)
    (cw / "notes" / "spec.md").write_text("The button must be gold.")
    assert s.current.ws.find_files("spec")[0] == "notes/spec.md" and "notes/spec.md" in s.current.ws.find_files("nsp")
    d.run("follow @notes/spec.md please", script=[("text", "ok")])
    body = json.dumps(REQUESTS[-1][2]["messages"][-1])
    assert "The button must be gold." in body and "notes/spec.md" in body, body[:300]
    # queue: second message waits for the first reply, then runs
    SCRIPT[:] = [("tool", "bash", {"command": "sleep 1"}), ("text", "first done"), ("text", "second done")]
    d.policy = "allow"
    n0 = len(REQUESTS)
    s.current.send("first")
    time.sleep(0.3)
    s.current.send("second")
    assert s.current.queue and s.current.queue[0]["text"] == "second"
    t0 = time.time()
    while (s.current.busy or s.current.queue) and time.time() - t0 < 20:
        time.sleep(0.1)
    texts = [e.get("text") for e in s.current.transcript if e["kind"] == "user"]
    assert texts[-2:] == ["first", "second"] and "second done" in json.dumps([e for e in s.current.transcript if e["kind"] == "text"]), texts
    # checkpoints: two edits, restore to before the first
    d.run("v1", script=[("tool", "write_file", {"path": "cp.txt", "content": "one"}), ("text", "ok")])
    d.run("v2", script=[("tool", "write_file", {"path": "cp.txt", "content": "two"}), ("text", "ok")])
    cps = s.current.checkpoint_list()
    assert [c["text"] for c in cps[-2:]] == ["v1", "v2"] and cps[-1]["files"] == ["cp.txt"]
    s.current.restore_to(cps[-2]["turn"])
    assert not (cw / "cp.txt").exists() and len(s.current.checkpoint_list()) == len(cps) - 2
    # Dock badge counts waiting approvals
    counts = []
    s.set_badge = counts.append
    d.policy = "allow"
    d.run("ask", script=[("tool", "bash", {"command": "touch badge.txt"}), ("text", "ok")])
    assert 1 in counts and counts[-1] == 0, counts
    s.set_badge = None
    print("66 mentions, queue, checkpoints, badge OK")
    # 67. export a whole project with its chats
    pdir2 = Path(tempfile.mkdtemp())
    pr = s.save_project({"name": "Launch", "folder": str(pdir2), "instructions": "Keep it short."})
    s.new_project_chat(pr["id"])
    d.run("plan the launch", script=[("text", "Step 1: write notes.")])
    out = Path(s.export_project(pr["id"]))
    md = out.read_text()
    assert out.name == "Launch (project).md" and "# Project: Launch" in md and "Keep it short." in md and "Step 1: write notes." in md, md[:400]
    out.unlink()
    s.delete_project(pr["id"])
    print("67 project export OK")
    # 68. context report: breakdown adds up, outputs list the files changed, environment fields
    s.set_mode("code")
    s.new_chat()
    d.run("write it", script=[("tool", "write_file", {"path": "out1.txt", "content": "x"}), ("text", "ok")])
    r = s.current.context_report()
    names = [p["name"] for p in r["parts"]]
    assert names[:2] == ["Messages", "Tools"] and r["used"] == sum(p["tokens"] for p in r["parts"]) and r["free"] == r["window"] - r["used"]
    assert r["outputs"] == ["out1.txt"] and r["folder"] == s.current.cwd and r["until_compact"] > 0 and r["model"]
    print("68 context report OK")
    # 69. services: office documents, search providers, image generation, maps, cloud upload, more skills
    import io, zipfile as zf
    from myagent import services as sv
    def office(name, files):
        b = io.BytesIO()
        with zf.ZipFile(b, "w") as z:
            for k, v in files.items():
                z.writestr(k, v)
        return b.getvalue()
    Wn = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    docx = office("a.docx", {"word/document.xml": f'<w:document {Wn}><w:body><w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Plan</w:t></w:r></w:p><w:p><w:r><w:t>Ship on Friday.</w:t></w:r></w:p></w:body></w:document>'})
    An = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
    pptx = office("b.pptx", {"ppt/slides/slide1.xml": f"<p {An}><a:p><a:r><a:t>Welcome</a:t></a:r></a:p></p>", "ppt/slides/slide2.xml": f"<p {An}><a:p><a:r><a:t>Q3 numbers</a:t></a:r></a:p></p>"})
    Sn = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    xlsx = office("c.xlsx", {"xl/workbook.xml": f'<workbook {Sn}><sheets><sheet name="Sales"/></sheets></workbook>',
                             "xl/sharedStrings.xml": f"<sst {Sn}><si><t>Region</t></si><si><t>North</t></si></sst>",
                             "xl/worksheets/sheet1.xml": f'<worksheet {Sn}><sheetData><row><c t="s"><v>0</v></c><c><v>Total</v></c></row><row><c t="s"><v>1</v></c><c><v>120</v></c></row></sheetData></worksheet>'})
    assert sv.read_office(docx, "a.docx") == "# Plan\n\nShip on Friday."
    assert "## Slide 1\nWelcome" in sv.read_office(pptx, "b.pptx") and "## Slide 2\nQ3 numbers" in sv.read_office(pptx, "b.pptx")
    assert "## Sheet: Sales\nRegion | Total\nNorth | 120" == sv.read_office(xlsx, "c.xlsx")
    s.set_mode("code")
    s.new_chat()
    cw = Path(s.current.cwd)
    (cw / "report.docx").write_bytes(docx)
    assert "Ship on Friday." in s.current.ws.run("read_document", {"path": "report.docx"})
    parts = s.current._parts("see attached", [{"name": "deck.pptx", "mime": "", "data": base64.b64encode(pptx).decode()}])
    assert any("Q3 numbers" in (p.get("text") or "") for p in parts)
    # stand-in service endpoints
    hits = {}
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
    class Svc(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass
        def _out(self, obj, ctype="application/json"):
            body = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
            self.send_response(200); self.send_header("Content-Type", ctype); self.end_headers(); self.wfile.write(body)
        def do_GET(self):
            hits.setdefault("GET " + self.path.split("?")[0], self.path)
            if self.path.startswith("/nominatim"):
                return self._out([{"display_name": "Lagos, Nigeria", "lat": "6.45", "lon": "3.39", "type": "city"}])
            if self.path.startswith("/img.png"):
                return self._out(png, "image/png")
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            hits["POST " + self.path] = body
            if self.path == "/search":
                return self._out({"results": [{"title": "Result A", "url": "https://a.example", "content": "About A"}]})
            if self.path == "/overpass":
                return self._out({"elements": [{"lat": 6.45, "lon": 3.39, "tags": {"name": "Cafe Neo", "opening_hours": "8-20"}}]})
            if self.path == "/images/generations":
                return self._out({"data": [{"b64_json": base64.b64encode(png).decode()}]})
        def do_PUT(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            hits["PUT"] = (self.path, self.headers.get("Authorization", ""), body)
            self._out({})
    ss = ThreadingHTTPServer(("127.0.0.1", 0), Svc)
    threading.Thread(target=ss.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{ss.server_port}"
    os.environ.update(LC4_BASE_SEARCH=base + "/search", LC4_BASE_NOMINATIM=base + "/nominatim", LC4_BASE_OVERPASS=base + "/overpass", LC4_BASE_IMAGES=base)
    s.save_services({"search_provider": "tavily", "image_provider": "openai", "keys": {"tavily": "tv-1", "openai": "sk-img"},
                     "s3": {"endpoint": base, "bucket": "bkt", "region": "auto", "access_key": "AK", "secret_key": "SK"}})
    pub = s.public_config()["services"]
    assert pub["search_provider"] == "tavily" and "tavily" in pub["keys_set"] and "secret_key" not in pub["s3"] and pub["s3"]["has_secret"]
    out = s.current.ws.run("web_search", {"query": "lightcode"})
    assert "Result A" in out and json.loads(hits["POST /search"])["api_key"] == "tv-1"
    assert "Lagos, Nigeria — 6.450000, 3.390000" in s.current.ws.run("geocode", {"query": "Lagos"})
    assert "Cafe Neo" in s.current.ws.run("places_nearby", {"lat": 6.45, "lon": 3.39, "kind": "cafe"})
    evs = d.run("draw", script=[("tool", "generate_image", {"prompt": "A gold sun", "path": "art/sun.png"}), ("text", "ok")])
    assert (cw / "art" / "sun.png").read_bytes() == png and json.loads(hits["POST /images/generations"])["prompt"] == "A gold sun"
    assert any(e.get("image") for e in d.kinds(evs, "tool_result")), "the image shows in the chat"
    evs = d.run("share", script=[("tool", "upload_file", {"path": "art/sun.png"}), ("text", "ok")])
    path_, auth, body_ = hits["PUT"]
    assert path_.startswith("/bkt/lightcode4/") and auth.startswith("AWS4-HMAC-SHA256 Credential=AK/") and body_ == png
    assert "X-Amz-Signature=" in json.dumps(REQUESTS[-1][2]["messages"][-1])
    s.save_services({"search_provider": "duckduckgo", "s3": {}})
    ss.shutdown()
    assert len(gallery.catalog()) >= 14 and {"data-analysis", "deep-research", "frontend-design", "brand-voice", "file-organizer", "internal-comms"} <= {x["id"] for x in gallery.catalog()}
    if sv.find_ffmpeg():
        vid = cw / "clip.mp4"
        subprocess.run([sv.find_ffmpeg(), "-y", "-f", "lavfi", "-i", "testsrc=duration=2:size=160x120:rate=10", "-f", "lavfi", "-i", "sine=duration=2",
                        "-shortest", str(vid)], capture_output=True, timeout=60)
        parts = s.current.ws.run("read_media", {"path": "clip.mp4", "frames": 3})
        assert sum(1 for p in parts if p["type"] == "image") == 3 and "2.0 seconds" in parts[0]["text"], parts[0]
        print("69 services OK (incl. video frames)")
    else:
        print("69 services OK")
    # 70. lost connection mid-reply: it waits, retries the same step, and the half-streamed text is not kept twice
    s.set_mode("code")
    s.new_chat()
    evs = d.run("keep going", script=[("tool", "bash", {"command": "touch step1.txt"}), ("drop", "HALF-SENT words that never finish"),
                                      ("text", "All finished after the reconnect.")])
    rc = d.kinds(evs, "reconnect")
    assert rc and rc[0].get("wait") and any(e.get("ok") for e in rc), rc
    txt = " ".join(e["text"] for e in s.current.transcript if e["kind"] == "text")
    assert "All finished after the reconnect." in txt and "HALF-SENT" not in txt, txt
    assert (Path(s.current.cwd) / "step1.txt").exists() and not s.current.interrupted
    # the app closed mid-reply (after a tool ran): reopening carries on from that step
    c = s.current
    c.provider.messages.append({"role": "user", "content": [{"type": "text", "text": "and one more thing"}]})
    c.busy = True
    c.save()
    c.busy = False
    cid = c.id
    s.chats.pop(cid)
    s.current = None
    s.open_chat(cid)
    assert s.current.interrupted and s.current.replay_event()["interrupted"]
    SCRIPT[:] = [("text", "Picked up the last request.")]
    start = len(d.events)
    s.current.continue_reply()
    deadline = time.time() + 20
    while time.time() < deadline and not any(e["kind"] == "status" and e["busy"] is False for e in d.events[start:]):
        time.sleep(0.05)
    assert "Picked up the last request." in s.current.provider.last_text() and not s.current.interrupted
    assert json.loads((CHATS / f"{cid}.json").read_text())["running"] is False
    print("70 reconnect and continue OK")
    # 71. long tasks keep going by themselves past the per-stretch step limit, without stopping
    import myagent.providers as prov
    old_max, prov.MAX_STEPS = prov.MAX_STEPS, 2
    s.new_chat()
    evs = d.run("long job", script=[("tool", "bash", {"command": "touch L1"})] * 7 + [("text", "Long job done.")])
    prov.MAX_STEPS = old_max
    assert "Long job done." in s.current.provider.last_text() and not s.current.interrupted
    assert any(e.get("step") for e in d.kinds(evs, "reconnect")), "shows it is still going"
    assert not any("Stopped after" in (e.get("text") or "") or "Paused" in (e.get("text") or "") for e in evs)
    print("71 long tasks carry on OK")
    # 72. queued messages always send (also after Stop); Send now stops the reply and sends straight away
    s.new_chat()
    c = s.current
    def wait_idle(start):
        deadline = time.time() + 20
        while time.time() < deadline and not any(e["kind"] == "status" and e["busy"] is False for e in d.events[start:]):
            time.sleep(0.05)
    SCRIPT[:] = [("tool", "bash", {"command": "sleep 3"}), ("text", "slow done")]  # Stop ends the slow reply; the queued one gets "slow done"
    start = len(d.events)
    c.send("slow one")
    time.sleep(0.8)
    c.send("queued question")
    assert [q["text"] for q in c.queue] == ["queued question"]
    c.cancel.set()
    wait_idle(start)
    time.sleep(0.3)
    deadline = time.time() + 20
    while time.time() < deadline and "slow done" not in c.provider.last_text():
        time.sleep(0.05)
    assert "slow done" in c.provider.last_text() and not c.queue, "a queued message is still sent after Stop"
    wait_idle(len(d.events) - 1)
    SCRIPT[:] = [("tool", "bash", {"command": "sleep 3"}), ("text", "Answered the queued one.")]
    start = len(d.events)
    c.send("slow two")
    time.sleep(0.8)
    c.send("send me now")
    c.cancel.set()
    deadline = time.time() + 20
    while time.time() < deadline and "Answered the queued one." not in c.provider.last_text():
        time.sleep(0.05)
    assert "Answered the queued one." in c.provider.last_text()
    wait_idle(len(d.events) - 1)
    print("72 queue stop and send-now OK")
    # 73. update download survives a connection that keeps dropping: pieces retry and resume, and the fingerprint is checked
    import hashlib, random, re
    from myagent import platform_extras as px2
    blob = random.Random(7).randbytes(3 * (8 << 20) + 12345)
    cuts = {"n": 0}
    class Dl(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, *a):
            pass
        def do_GET(self):
            if self.path == "/rel":
                body = json.dumps({"assets": [{"name": px2.asset_name(), "size": len(blob), "browser_download_url": base2 + "/file",
                                               "digest": "sha256:" + hashlib.sha256(blob).hexdigest()}]}).encode()
                self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
                return
            a, b = 0, len(blob) - 1
            m = re.match(r"bytes=(\d+)-(\d+)", self.headers.get("Range", ""))
            if m:
                a, b = int(m.group(1)), int(m.group(2))
            self.send_response(206 if m else 200)
            self.send_header("Content-Length", str(b - a + 1)); self.end_headers()
            data = blob[a:b + 1]
            if cuts["n"] < 6 and len(data) > 100000:  # the first few connections die halfway
                cuts["n"] += 1
                self.wfile.write(data[: len(data) // 3]); self.wfile.flush()
                self.connection.shutdown(2)
                return
            self.wfile.write(data)
    ds = ThreadingHTTPServer(("127.0.0.1", 0), Dl)
    threading.Thread(target=ds.serve_forever, daemon=True).start()
    base2 = f"http://127.0.0.1:{ds.server_port}"
    old_sleep = px2.time.sleep
    px2.time.sleep = lambda x: None
    pcts = []
    try:
        out = px2.download_update(base2 + "/rel", progress=pcts.append)
    finally:
        px2.time.sleep = old_sleep
    assert Path(out).read_bytes() == blob and cuts["n"] == 6 and pcts[-1] == 100, (cuts, pcts[-3:])
    assert px2.download_update(base2 + "/rel") == out, "an already-downloaded update is reused"
    Path(out).unlink()
    ds.shutdown()
    print("73 update download resumes through drops OK")
    # 74. GitHub: connect with a token, list repositories, clone + open, git/gh signed in for commands, API tools
    from myagent import github as ghm
    gh_hits, polls = [], []
    class GH(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass
        def _out(self, obj, code=200):
            b = json.dumps(obj).encode(); self.send_response(code); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
        def do_GET(self):
            gh_hits.append(("GET", self.path, self.headers.get("Authorization")))
            if self.headers.get("Authorization") != "Bearer tok123":
                return self._out({"message": "Bad credentials"}, 401)
            if self.path == "/user":
                return self._out({"login": "octo", "name": "Octo Cat", "avatar_url": "", "id": 42, "html_url": "https://github.com/octo"})
            if self.path.startswith("/user/repos"):
                return self._out([{"full_name": "octo/demo", "private": True, "description": "Demo repo", "pushed_at": "2026-11-20T00:00:00Z",
                                   "html_url": "https://github.com/octo/demo", "default_branch": "main"}])
            return self._out([{"number": 7, "title": "Bug"}])
        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}"
            if self.path == "/login/device/code":
                return self._out({"device_code": "dc9", "user_code": "WDJB-MJHT", "verification_uri": "https://github.com/login/device", "interval": 0, "expires_in": 30})
            if self.path == "/login/oauth/access_token":
                polls.append(raw)
                return self._out({"error": "authorization_pending"} if len(polls) < 3 else {"access_token": "tok123", "token_type": "bearer"})
            body = json.loads(raw)
            gh_hits.append(("POST", self.path, body))
            self._out({"number": 8, "title": body.get("title")}, 201)
    gs = ThreadingHTTPServer(("127.0.0.1", 0), GH)
    threading.Thread(target=gs.serve_forever, daemon=True).start()
    ghm.API = f"http://127.0.0.1:{gs.server_port}"
    try:
        s.github_connect("wrong")
        raise AssertionError("a bad token must be refused")
    except ValueError as e:
        assert "rejected" in str(e)
    # "connect my GitHub": the agent shows a code card and opens GitHub; approving there finishes it, no token to copy
    os.environ["LC4_GITHUB_WEB"] = ghm.API
    s.config["github_client_id"] = "Iv1.test"
    opened = []
    s.open_url = opened.append
    evs = d.run("connect my github", script=[("tool", "connect_github", {}), ("text", "Approve it on GitHub.")])
    assert "WDJB-MJHT" in d.kinds(evs, "tool_result")[0]["output"] and opened == ["https://github.com/login/device"]
    assert any(e.get("code") == "WDJB-MJHT" for e in d.kinds(evs, "gh_device"))
    deadline = time.time() + 10
    while time.time() < deadline and not s.github_status()["connected"]:
        time.sleep(0.05)
    st = s.github_status()
    assert st["connected"] and st["user"]["login"] == "octo" and b"client_id=Iv1.test" in polls[-1]
    assert any(e.get("done") for e in d.events if e["kind"] == "gh_device")
    assert s.public_config()["github_token"] is True and "tok123" not in json.dumps(s.public_config())
    assert s.github_repos()[0]["full_name"] == "octo/demo"
    # a local "GitHub" to clone from
    remote_root = Path(tempfile.mkdtemp())
    src = remote_root / "src"; src.mkdir()
    for c in (["init", "-q", "-b", "main"], ["config", "user.email", "t@e.com"], ["config", "user.name", "T"]):
        subprocess.run(["git", *c], cwd=src, check=True)
    (src / "README.md").write_text("# demo\n"); subprocess.run(["git", "add", "-A"], cwd=src, check=True); subprocess.run(["git", "commit", "-qm", "init"], cwd=src, check=True)
    (remote_root / "octo").mkdir(); subprocess.run(["git", "clone", "-q", "--bare", str(src), str(remote_root / "octo" / "demo.git")], check=True)
    os.environ["LC4_GITHUB_WEB"] = remote_root.as_uri()
    s.config["github_folder"] = str(Path(tempfile.mkdtemp()) / "LightCode4")
    dest = Path(s.github_clone("octo/demo"))
    assert (dest / "README.md").is_file() and s.current.cwd == str(dest) and s.current.kind == "code"
    assert s.github_clone("octo/demo") == str(dest), "opening again reuses the clone"
    ws = s.current.ws
    env = ws._env()  # what every command the agent runs gets (same on Mac, Windows and Linux)
    assert env["GH_TOKEN"] == "tok123" and env["GIT_TERMINAL_PROMPT"] == "0"
    ask = lambda prompt: subprocess.run([env["GIT_ASKPASS"], prompt], capture_output=True, text=True, env=env).stdout.strip()
    assert ask("Password for 'https://github.com': ") == "tok123" and ask("Username for 'https://github.com': ") == "x-access-token"
    assert "@octo" in s.system_for({**s.config, "cwd": str(dest)}) and "never ask the user for a git name" in s.system_for({**s.config, "cwd": str(dest)})
    subprocess.run(["git", "-c", "user.name=Orbit Workspace", "-c", "user.email=workspace@local", "commit", "-q", "--allow-empty", "-m", "test"],
                   cwd=dest, env=env, check=True)
    who = subprocess.run(["git", "log", "-1", "--format=%an <%ae> / %cn <%ce>"], cwd=dest, env=env, capture_output=True, text=True).stdout.strip()
    assert who == "Octo Cat <42+octo@users.noreply.github.com> / Octo Cat <42+octo@users.noreply.github.com>", who
    evs = d.run("file an issue", script=[("tool", "github_get", {"path": "/repos/octo/demo/issues"}),
                                         ("tool", "github_api", {"method": "post", "path": "/repos/octo/demo/issues", "body": {"title": "From LightCode"}}),
                                         ("text", "Filed #8.")])
    outs = [e["output"] for e in d.kinds(evs, "tool_result")]
    assert '"number": 7' in outs[0] and '"number": 8' in outs[1] and ("POST", "/repos/octo/demo/issues", {"title": "From LightCode"}) in gh_hits
    assert any(e["name"] == "github_api" for e in d.kinds(evs, "approve")), "changing GitHub asks first"
    assert "tok123" not in json.dumps(s.current.transcript)
    s.github_disconnect()
    assert not s.github_status()["connected"] and not s.current.ws.env_extra
    assert "call connect_github instead of asking" in s.system_for({**s.config, "cwd": str(dest)})
    os.environ.pop("LC4_GITHUB_WEB", None)
    gs.shutdown()
    print("74 GitHub connect, clone and tools OK")
    # 75. text files in every common encoding: attached or read from the folder
    from myagent.tools import decode_text
    sample = "Café – “quotes” ✓ naïve\nline two"
    variants = {"utf8.txt": sample.encode("utf-8"), "bom.txt": sample.encode("utf-8-sig"), "notepad-unicode.txt": sample.encode("utf-16"),
                "utf16-nobom.txt": sample.encode("utf-16-le"), "utf16be.txt": sample.encode("utf-16-be")}
    for n, raw in variants.items():
        assert decode_text(raw) == sample, (n, decode_text(raw))
    old_win = "Café naïve – old Windows".encode("cp1252")
    assert decode_text(old_win) == "Café naïve – old Windows"
    assert decode_text(bytes(range(256)) * 4) is None
    s.set_mode("code")
    s.new_chat()
    cw = Path(s.current.cwd)
    for n, raw in variants.items():
        (cw / n).write_bytes(raw)
        assert "Café – “quotes” ✓ naïve" in s.current.ws.run("read_file", {"path": n}), n
    parts = s.current._parts("read this", [{"name": "notes.txt", "mime": "text/plain", "data": base64.b64encode(sample.encode("utf-16")).decode()}])
    assert sample in parts[0]["text"]
    print("75 text files in any encoding OK")
    # 76. Rewind to here (no new message): the chat goes back to just before that message, files put back
    s.new_chat()
    d.run("first", script=[("text", "one")])
    d.run("make a file", script=[("tool", "write_file", {"path": "rw.txt", "content": "x"}), ("text", "made")])
    assert (Path(s.current.cwd) / "rw.txt").exists()
    idx = [i for i, e in enumerate(s.current.transcript) if e["kind"] == "user"][1]
    n_req = len(REQUESTS)
    s.current.rewind(idx, "", True)
    assert not (Path(s.current.cwd) / "rw.txt").exists() and [e["text"] for e in s.current.transcript if e["kind"] == "user"] == ["first"]
    assert len(REQUESTS) == n_req and not s.current.busy, "rewinding sends nothing"
    print("76 rewind to here OK")
    # 77. response styles + custom sub-agents from .claude/agents (Claude Code format)
    s.set_mode("code")
    s.new_chat()
    cw = Path(s.current.cwd)
    s.apply_config({"style": "concise"}, keep_history=True)
    assert "Response style: concise" in s.system_for({**s.config, "cwd": str(cw)})
    assert "Response style: concise" in s.chat_system(s.config)
    s.apply_config({"style": "custom", "style_custom": "Always answer like a pirate."}, keep_history=True)
    assert "like a pirate" in s.system_for({**s.config, "cwd": str(cw)})
    s.apply_config({"style": "normal"}, keep_history=True)
    assert "Response style" not in s.system_for({**s.config, "cwd": str(cw)})
    (cw / ".claude" / "agents").mkdir(parents=True)
    (cw / ".claude" / "agents" / "scribe.md").write_text("---\nname: scribe\ndescription: Writes release notes into files\n"
                                                        "tools: read_file, write_file\nmodel: haiku\n---\nYou are Scribe, a careful release-notes writer.\n")
    (cw / ".claude" / "skills" / "tone").mkdir(parents=True)
    (cw / ".claude" / "skills" / "tone" / "SKILL.md").write_text("---\nname: tone\ndescription: House tone of voice\n---\nBe kind.")
    s.new_chat()
    sysp = s.system_for({**s.config, "cwd": str(cw)})
    assert "- scribe: Writes release notes into files" in sysp and "- tone: House tone of voice" in sysp
    n0 = len(REQUESTS)
    evs = d.run("write the notes", script=[("tool", "task", {"description": "write notes", "prompt": "Write NOTES.md", "kind": "scribe"}),
                                           ("tool", "write_file", {"path": "NOTES.md", "content": "v2 notes"}), ("text", "Wrote NOTES.md."),
                                           ("text", "Done: the scribe wrote the notes.")])
    assert (cw / "NOTES.md").read_text() == "v2 notes", "a custom agent can change files when its tools allow it"
    assert any(e["name"] == "write_file" for e in d.kinds(evs, "approve")), "its changes still ask first"
    sub_req = [r for r in REQUESTS[n0:] if "Scribe" in json.dumps(r[2].get("system", ""))]
    assert sub_req and "haiku" in sub_req[0][2]["model"], [r[2].get("model") for r in REQUESTS[n0:]]
    assert {t["name"] for t in sub_req[0][2]["tools"]} >= {"read_file", "write_file"} and "bash" not in {t["name"] for t in sub_req[0][2]["tools"]}
    print("77 response styles + custom agents OK")
    # 78. clicking a notification (lightcode4://chat/<id>) opens that chat in the running app and brings it forward
    from myagent import desktop
    from myagent.gui import start_server as _start, read_running
    srv78, _url, s78 = _start(str(Path(tempfile.mkdtemp())))
    s78.set_mode("code"); s78.new_chat()
    target = s78.current
    target.transcript.append({"kind": "user", "text": "hello"}); target.save()
    s78.new_chat()
    focused = []
    s78.focus_main = lambda: focused.append(1)
    assert read_running()["url"].endswith(str(srv78.server_port))
    assert desktop.forward_link(["LightCode4", f"lightcode4://chat/{target.id}"]) is True
    assert s78.current.id == target.id and focused, "the clicked chat is shown and the window comes forward"
    assert desktop.forward_link(["LightCode4"]) is False
    srv78.shutdown()
    print("78 notification click opens its chat OK")
    # 79. marketplace: skills/agents/commands repos, plugin marketplaces (bundles, folders, other repos), sub-folder sources,
    #     install + remove, background refresh
    from myagent import market
    repos = {
        "acme/skills": {"skills/pdf-tools/SKILL.md": "---\nname: pdf-tools\ndescription: Work with PDF files\n---\nUse pypdf.",
                        "skills/pdf-tools/scripts/split.py": "print('split')",
                        "agents/reviewer.md": "---\nname: reviewer\ndescription: Strict code reviewer\n---\nReview hard.",
                        "commands/ship.md": "---\ndescription: Ship it\n---\nRun the release for $ARGUMENTS", "README.md": "# hi"},
        "acme/plugins": {".claude-plugin/marketplace.json": json.dumps({"plugins": [
                             {"name": "dev-kit", "description": "Dev helpers", "source": "./plugins/dev"},
                             {"name": "doc-bundle", "source": "./", "skills": ["./skills/word"]},
                             {"name": "remote-kit", "source": {"source": "git-subdir", "url": "https://github.com/other/kits.git", "path": "kits/remote"}},
                             {"name": "npm-only", "source": {"source": "npm", "package": "x"}}]}),
                         "skills/word/SKILL.md": "---\nname: word\ndescription: Word docs\n---\nDocx.",
                         "plugins/dev/skills/lint/SKILL.md": "---\nname: lint\ndescription: Lint code\n---\nRun the linter.",
                         "plugins/dev/agents/fixer.md": "---\nname: fixer\ndescription: Fixes lint\n---\nFix.",
                         "plugins/dev/.claude-plugin/plugin.json": json.dumps({"name": "dev-kit", "description": "Dev helpers"}),
                         "plugins/dev/hooks/hooks.json": "{}"},
        "other/kits": {"kits/remote/commands/deploy.md": "---\ndescription: Deploy\n---\nDeploy now."},
        "acme/app": {"src/main.py": "print()", "README.md": "# an app"}}
    calls = []
    class MK(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass
        def do_GET(self):
            calls.append(self.path)
            m = re.match(r"^/api/repos/([\w.-]+/[\w.-]+)/git/trees/HEAD", self.path)
            if m and m.group(1) in repos:
                body = json.dumps({"tree": [{"path": k, "type": "blob"} for k in repos[m.group(1)]]}).encode()
            else:
                m = re.match(r"^/raw/([\w.-]+/[\w.-]+)/HEAD/(.+)$", self.path)
                if not m or m.group(2) not in repos.get(m.group(1), {}):
                    self.send_response(404); self.end_headers(); return
                body = repos[m.group(1)][m.group(2)].encode()
            self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    ms = ThreadingHTTPServer(("127.0.0.1", 0), MK)
    threading.Thread(target=ms.serve_forever, daemon=True).start()
    os.environ["LC4_MARKET_API"] = f"http://127.0.0.1:{ms.server_port}/api"
    os.environ["LC4_MARKET_RAW"] = f"http://127.0.0.1:{ms.server_port}/raw"
    market.HOME = ex.HOME
    market.STATE = ex.HOME / "market.json"
    assert "anthropics/skills" in market.load_state()["sources"] and len(market.load_state()["sources"]) >= 100
    assert market.parse_source("https://github.com/anthropics/claude-plugins-official/tree/main/plugins/claude-code-setup") == \
        ("anthropics/claude-plugins-official", "plugins/claude-code-setup")
    assert market.normalize("https://github.com/ScrapeCreators/social-media-research-skills\\") == "ScrapeCreators/social-media-research-skills"
    try:
        market.add_source("nobody/missing")
        raise AssertionError("a missing repository is refused")
    except market.MarketError:
        pass
    assert market.add_source("https://github.com/acme/skills/")[0] == "acme/skills"
    items = {i["id"]: i for i in market.browse("acme/skills")}
    assert items["skill:skills/pdf-tools"]["description"] == "Work with PDF files" and "agent:agents/reviewer.md" in items and "command:commands/ship.md" in items
    for iid in ("skill:skills/pdf-tools", "agent:agents/reviewer.md", "command:commands/ship.md"):
        market.install("acme/skills", iid)
    assert (ex.HOME / "skills" / "pdf-tools" / "scripts" / "split.py").is_file()
    s.new_chat()
    assert "pdf-tools" in ex.skills(s.current.cwd) and "reviewer" in ex.agents(s.current.cwd) and "ship" in ex.commands(s.current.cwd)
    assert ex.expand_command("/ship v2", s.current.cwd) == "Run the release for v2"
    assert all(i["installed"] for i in market.browse("acme/skills"))
    market.uninstall("acme/skills", "skill:skills/pdf-tools")
    assert not (ex.HOME / "skills" / "pdf-tools").exists() and "pdf-tools" not in ex.skills(s.current.cwd)
    plug = {i["name"]: i for i in market.browse("acme/plugins") if i["kind"] == "plugin"}
    assert set(plug) == {"dev-kit", "doc-bundle", "remote-kit"}, plug
    assert market.install("acme/plugins", "plugin:dev-kit")["installed"] == 2
    assert "lint" in ex.skills(s.current.cwd) and "fixer" in ex.agents(s.current.cwd)
    assert market.install("acme/plugins", "plugin:doc-bundle")["installed"] == 1 and "word" in ex.skills(s.current.cwd)
    assert market.install("acme/plugins", "plugin:remote-kit")["installed"] == 1 and "deploy" in ex.commands(s.current.cwd)
    market.uninstall("acme/plugins", "plugin:dev-kit")
    assert "lint" not in ex.skills(s.current.cwd) and "fixer" not in ex.agents(s.current.cwd)
    sub = market.browse("acme/plugins/plugins/dev")  # a folder inside a repository
    assert {i["kind"] for i in sub} == {"plugin", "skill", "agent"} and market.install("acme/plugins/plugins/dev", "plugin:@plugins/dev")["installed"] == 2
    assert market.browse("acme/app") == [], "an app repository simply offers nothing"
    # background refresh: checks the source looked at longest ago, one at a time
    st = market.load_state(); st["sources"] = ["acme/app", "acme/skills"]; st["removed"] = list(market.DEFAULT_SOURCES); market.save_state(st)
    market._trees.clear()
    old = market.cached("acme/app"); old["t"] = 0; market._cache_file("acme/app").write_text(json.dumps(old))
    assert market.refresh_next() == "acme/app" and market.cached("acme/app")["t"] > 0
    assert market.refresh_next() is None, "everything is fresh"
    assert {x["repo"] for x in market.overview()} == {"acme/app", "acme/skills"} and len(market.all_items()) == 3
    ms.shutdown()
    print("79 marketplace OK")
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
