"""Run the UI audit (tests/ui_audit.js) against the real engine with a scripted fake model. No API calls."""
import subprocess, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import test_loop
from test_loop import SCRIPT, start_fake
start_fake()
work = Path(tempfile.mkdtemp()); (work / "app.py").write_text("def total(xs):\n    return sum(xs)\n\n\n\n\ndef avg(xs):\n    return total(xs) / len(xs)\n")
for c in (["init", "-q", "-b", "main"], ["config", "user.email", "t@e.com"], ["config", "user.name", "T"], ["add", "-A"], ["commit", "-qm", "init"]):
    subprocess.run(["git", *c], cwd=work, check=True)
from myagent.gui import start_server
SCRIPT[:] = [("tool", "todo_write", {"todos": [{"content": "Read app.py", "status": "completed"}, {"content": "Handle empty input", "status": "in_progress"}]}),
             ("tool", "edit_file", {"path": "app.py", "old_text": "    return sum(xs)", "new_text": "    return sum(xs or [])"}), ("text", "Done."),
             ("tool", "multi_edit", {"path": "app.py", "edits": [{"old_text": "def total(xs):", "new_text": "def total(xs=()):"},
                                                                {"old_text": "/ len(xs)", "new_text": "/ max(len(xs), 1)"}]}),
             ("text", "Reviewed."), ("text", "Edited answer."), ("text", "SUMMARY: total and avg were tidied."), ("text", "Side answer: it sums the list."),
             ("text", "Here is a live demo:\n\n```html\n<!doctype html><title>Sunrise counter</title><button id=b>Clicks: 0</button><script>let n=0;b.onclick=()=>b.textContent='Clicks: '+(++n)</script>\n```\n\nThe flow:\n\n```mermaid\nflowchart LR\n  A[Cart] --> B{Empty?}\n  B -- yes --> C[0]\n  B -- no --> D[sum]\n```\n\nAnd the formula: $$\\bar{x} = \\frac{1}{n}\\sum_{i=1}^{n} x_i$$ while a price like $5 stays text.")]
import os
server, url, session = start_server(str(work))
lang = os.environ.get("HERNESS_UI_LANG", "")
if lang:
    session.config["ui_language"] = lang
r = subprocess.run(["node", str(Path(__file__).with_name("ui_audit.js")), url] + ([lang] if lang else []), capture_output=True, text=True, timeout=300, cwd=Path(__file__).parent.parent)
print(r.stdout[-6000:], r.stderr[-2000:])
