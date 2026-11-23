"""Interface test: real engine + scripted fake model + headless browser (Node Playwright). No API calls."""
import os, subprocess, sys, tempfile
from pathlib import Path
import test_loop
from test_loop import SCRIPT, start_fake

start_fake()
work = Path(tempfile.mkdtemp()); (work / "app.py").write_text("def total(xs):\n    return sum(xs)\n")
(work / "HERNESS.md").write_text("Use pytest.")
from myagent.gui import start_server
SCRIPT[:] = [
    ("tool", "todo_write", {"todos": [{"content": "Read app.py", "status": "completed"},
                                      {"content": "Handle empty input", "status": "in_progress"},
                                      {"content": "Run the tests", "status": "pending"}]}),
    ("tool", "edit_file", {"path": "app.py", "old_text": "    return sum(xs)", "new_text": "    if not xs:\n        return 0\n    return sum(xs)"}),
    ("text", "Fixed **total()** so it handles an empty list.\n\n## Changes\n| File | Change |\n|---|---|\n| `app.py` | Return 0 for empty input |\n\n```python\ndef total(xs):\n    if not xs:\n        return 0\n    return sum(xs)\n```"),
]
server, url, session = start_server(str(work))
r = subprocess.run(["node", str(Path(__file__).with_name("test_gui.js")), url], capture_output=True, text=True, timeout=120)
print(r.stdout, r.stderr[-1500:])
assert "BROWSER OK" in r.stdout
assert "return 0" not in (work / "app.py").read_text()  # undone in the browser test
print("GUI FLOW OK")
