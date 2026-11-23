"""Offline tests for Incredible Herness: a scripted fake Claude / OpenAI server drives the real engine.
No real API calls, no tokens used.   Run: python test_loop.py
"""
import base64
import json
import os
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
    s.add_rule("bash(rm *)", "deny")
    s.add_rule("bash(echo *)", "allow")
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
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
