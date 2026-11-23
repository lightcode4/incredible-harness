"""A minimal MCP server over stdio for tests: one tool, `shout`, that upper-cases text."""
import json
import sys

for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    m, rid = msg.get("method"), msg["id"]
    if m == "initialize":
        res = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake", "version": "1"}}
    elif m == "tools/list":
        res = {"tools": [{"name": "shout", "description": "Upper-case some text",
                          "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}]}
    elif m == "tools/call":
        res = {"content": [{"type": "text", "text": msg["params"]["arguments"]["text"].upper()}]}
    else:
        print(json.dumps({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "unknown"}}), flush=True)
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": res}), flush=True)
