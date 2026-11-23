"""Offline tests for Incredible Herness: a scripted fake Claude / OpenAI server drives the real engine.
No real API calls, no tokens used.   Run: python test_loop.py
"""
import base64
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

os.environ["HOME"] = os.environ["USERPROFILE"] = tempfile.mkdtemp()  # keep settings/chats out of the real home
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
    s.apply_config({"cwd": str(work), "mcp": [{"name": "gh", "url": "https://mcp.example/mcp"}]})
    d = Driver(s)

    # 1. bash with approval; request shape (fallbacks, compaction, web, MCP, eager streaming)
    evs = d.run("say hello", script=[("tool", "bash", {"command": "echo hello-from-tool"}), ("text", "Done.")])
    res = d.kinds(evs, "tool_result")
    assert res and "hello-from-tool" in res[0]["output"] and not res[0]["error"], res
    _, hdr, body = REQUESTS[-2]
    betas = hdr.get("anthropic-beta", "")
    for b in ("server-side-fallback-2026-07-01", "compact-2026-01-12", "mcp-client-2025-11-20"):
        assert b in betas, betas
    names = [t.get("name") or t.get("type") for t in body["tools"]]
    assert "web_search" in names and "web_fetch" in names and "mcp_toolset" in names and "fetch_url" not in names, names
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
    d.run("decline", script=[("tool", "bash", {"command": "ls"}), ("text", "ok")])
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
        s.browser.close()
        print("16 built-in browser OK")
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
    s.config["hooks"][folder] = {"after_edit": "exit 3", "before_finish": "python -c \"import sys; sys.exit(0 if open('ok.txt').read()=='yes' else 1)\""}
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
    SCRIPT[:] = [("tool", "bash", {"command": "echo should-not-run"}), ("text", "Summary: skipped one step.")]
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
        assert "Incredible Herness" in ur.urlopen(phone, timeout=5).read().decode()
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
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
