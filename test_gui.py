"""Interface test: real engine + scripted fake model + headless browser (Node Playwright). No API calls."""
import os, subprocess, sys, tempfile
from pathlib import Path
import test_loop
from test_loop import SCRIPT, start_fake

start_fake()
work = Path(tempfile.mkdtemp()); (work / "app.py").write_text("def total(xs):\n    return sum(xs)\n")
for c in (["init", "-q", "-b", "main"], ["config", "user.email", "t@e.com"], ["config", "user.name", "T"], ["add", "-A"], ["commit", "-qm", "init"]):
    subprocess.run(["git", *c], cwd=work, check=True)
(work / "HERNESS.md").write_text("Use pytest.")
from myagent.gui import start_server
SCRIPT[:] = [
    ("tool", "todo_write", {"todos": [{"content": "Read app.py", "status": "completed"},
                                      {"content": "Handle empty input", "status": "in_progress"},
                                      {"content": "Run the tests", "status": "pending"}]}),
    ("tool", "edit_file", {"path": "app.py", "old_text": "    return sum(xs)", "new_text": "    if not xs:\n        return 0\n    return sum(xs)"}),
    ("text", "Fixed **total()** so it handles an empty list.\n\n## Changes\n| File | Change |\n|---|---|\n| `app.py` | Return 0 for empty input |\n\n```python\ndef total(xs):\n    if not xs:\n        return 0\n    return sum(xs)\n```"),
]
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
site = Path(tempfile.mkdtemp())
(site / "index.html").write_text("""<title>Demo shop</title><body style="font-family:sans-serif;padding:40px;background:#f4efe6">
<h1>Demo shop</h1><p>A page served from your computer.</p><button style="font-size:20px;padding:10px 24px"
onclick="this.innerText='Added to cart ✓'">Add to cart</button></body>""")
class Q(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k): super().__init__(*a, directory=str(site), **k)
    def log_message(self, *a): pass
web = ThreadingHTTPServer(("127.0.0.1", 0), Q); threading.Thread(target=web.serve_forever, daemon=True).start()
server, url, session = start_server(str(work))
r = subprocess.run(["node", str(Path(__file__).with_name("test_gui.js")), url, f"http://127.0.0.1:{web.server_port}/"], capture_output=True, text=True, timeout=120)
print(r.stdout, r.stderr[-1500:])
assert "BROWSER OK" in r.stdout
assert "return 0" not in (work / "app.py").read_text()  # undone in the browser test
print("GUI FLOW OK")
