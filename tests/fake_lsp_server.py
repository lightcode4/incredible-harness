"""A tiny fake language server for tests: answers definition/references/hover and publishes one diagnostic."""
import json, re, sys
docs = {}
def read():
    length = None
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            sys.exit(0)
        if line.strip() == b"":
            break
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":")[1])
    return json.loads(sys.stdin.buffer.read(length))
def send(msg):
    body = json.dumps(msg).encode()
    sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n" % len(body) + body); sys.stdout.buffer.flush()
def word_at(text, line, ch):
    l = text.splitlines()[line]
    for m in re.finditer(r"\w+", l):
        if m.start() <= ch <= m.end():
            return m.group(0)
def locs(name, defs_only):
    out = []
    for u, t in docs.items():
        for i, l in enumerate(t.splitlines()):
            for m in re.finditer(rf"\b{name}\b", l):
                if not defs_only or l.lstrip().startswith("def "):
                    out.append({"uri": u, "range": {"start": {"line": i, "character": m.start()}, "end": {"line": i, "character": m.end()}}})
    return out
while True:
    m = read(); meth = m.get("method"); p = m.get("params", {})
    if meth == "initialize":
        send({"jsonrpc": "2.0", "id": m["id"], "result": {"capabilities": {}}})
    elif meth in ("textDocument/didOpen", "textDocument/didChange"):
        u = p["textDocument"]["uri"]
        docs[u] = p["textDocument"]["text"] if meth.endswith("didOpen") else p["contentChanges"][-1]["text"]
        diags = [{"range": {"start": {"line": i, "character": 0}, "end": {"line": i, "character": 1}}, "severity": 1, "message": "undefined name 'oops'"}
                 for i, l in enumerate(docs[u].splitlines()) if "oops" in l]
        send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics", "params": {"uri": u, "diagnostics": diags}})
    elif meth in ("textDocument/definition", "textDocument/references", "textDocument/hover"):
        u = p["textDocument"]["uri"]; w = word_at(docs[u], p["position"]["line"], p["position"]["character"])
        if meth.endswith("hover"):
            send({"jsonrpc": "2.0", "id": m["id"], "result": {"contents": {"kind": "plaintext", "value": f"(function) def {w}(xs: list[int]) -> int"}}})
        else:
            send({"jsonrpc": "2.0", "id": m["id"], "result": locs(w, meth.endswith("definition"))})
    elif meth == "shutdown":
        send({"jsonrpc": "2.0", "id": m["id"], "result": None})
    elif meth == "exit":
        sys.exit(0)
    elif "id" in m:
        send({"jsonrpc": "2.0", "id": m["id"], "result": None})
