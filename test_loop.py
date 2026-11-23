"""End-to-end loop test against fake Claude and OpenAI servers (no real API calls)."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from myagent.cli import App, main  # noqa: F401
from myagent import cli

REQUESTS = []


def sse(events):
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def claude_reply(n):
    msg = {"id": f"msg_{n}", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": [],
           "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 1}}
    ev = [{"type": "message_start", "message": msg}]
    if n % 2 == 1:
        ev += [
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "I will run a quick command to check your setup."}},
            {"type": "content_block_stop", "index": 0},
            {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "toolu_1", "name": "bash", "input": {}}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "{\"command\": \"echo hel"}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "lo-from-tool\"}"}},
            {"type": "content_block_stop", "index": 1},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None}, "usage": {"output_tokens": 5}},
        ]
    else:
        ev += [
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Done: it printed hello.\n\n## What I did\n- Ran an **echo** command in your folder\n- Checked the output matches\n\n```bash\necho hello-from-tool\n```\nAnything else you want me to build?"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 5}},
        ]
    ev.append({"type": "message_stop"})
    return sse(ev)


def openai_reply(n):
    def chunk(delta, finish=None):
        return "data: " + json.dumps({"id": "c", "object": "chat.completion.chunk", "created": 0, "model": "m",
                                      "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}) + "\n\n"
    if n % 2 == 1:
        body = chunk({"role": "assistant", "tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                                            "function": {"name": "bash", "arguments": "{\"command\":"}}]})
        body += chunk({"tool_calls": [{"index": 0, "function": {"arguments": " \"echo hello-from-tool\"}"}}]})
        body += chunk({}, "tool_calls")
    else:
        body = chunk({"role": "assistant", "content": "Done (openai)."}) + chunk({}, "stop")
    return (body + "data: [DONE]\n\n").encode()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        REQUESTS.append((self.path, dict(self.headers), body))
        n = sum(1 for p, _, _ in REQUESTS if p == self.path)
        data = claude_reply(n) if "/messages" in self.path else openai_reply(n)
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}"

    import os
    os.environ["ANTHROPIC_BASE_URL"] = url
    os.environ["ANTHROPIC_API_KEY"] = "test"

    answers = iter(["y", "y"])
    cli.input = lambda prompt="": next(answers)  # approve the bash call

    # --- Claude
    args = cli.argparse.Namespace(provider="claude", model=None, base_url=None, api_key=None, effort="high",
                                  cwd=".", auto=False, prompt=[])
    app = App(args)
    app.provider.turn("say hello with bash", app.run_tool, app.ui)
    app.ui.end_block()
    (_, h1, b1), (_, _, b2) = REQUESTS[0], REQUESTS[1]
    assert b1["model"] == "claude-opus-5-5" and b1["fallbacks"] == "default", b1.keys()
    assert "server-side-fallback-2026-07-01" in h1.get("anthropic-beta", ""), h1.get("anthropic-beta")
    assert b1["thinking"] == {"type": "adaptive", "display": "summarized"} and b1["output_config"] == {"effort": "high"}
    assert all(t["eager_input_streaming"] for t in b1["tools"])
    tr = b2["messages"][-1]["content"]
    assert len(tr) == 1 and tr[0]["tool_use_id"] == "toolu_1" and "hello-from-tool" in tr[0]["content"] and not tr[0]["is_error"], tr
    assert b2["messages"][1]["content"][1]["type"] == "tool_use"
    print("\nCLAUDE LOOP OK")

    # --- OpenAI-compatible (as 'local')
    args.provider, args.base_url = "local", url + "/v1"
    app = App(args)
    app.provider.turn("say hello with bash", app.run_tool, app.ui)
    app.ui.end_block()
    o2 = [b for p, _, b in REQUESTS if p.endswith("/chat/completions")][1]
    tool_msg = o2["messages"][-1]
    assert tool_msg["role"] == "tool" and tool_msg["tool_call_id"] == "call_1" and "hello-from-tool" in tool_msg["content"], tool_msg
    assert o2["model"] == "llama3.1"
    print("\nOPENAI-COMPATIBLE LOOP OK")

    # --- validation + decline
    assert "Missing required" in app.run_tool("write_file", {"path": "x"})[0]
    cli.input = lambda prompt="": "no, use python instead"
    out, err = app.run_tool("bash", {"command": "rm -rf /tmp/nothing"})
    assert err and "use python instead" in out
    print("VALIDATION + DECLINE OK")
