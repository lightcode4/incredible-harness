"""GUI test: fake Claude server + real engine + headless browser clicking Allow. No API calls."""
import os, tempfile, threading, time
from http.server import HTTPServer
os.environ["HOME"] = tempfile.mkdtemp()

from test_loop import Handler, REQUESTS
REQUESTS.clear()
srv = HTTPServer(("127.0.0.1", 0), Handler); threading.Thread(target=srv.serve_forever, daemon=True).start()
os.environ["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{srv.server_port}"
os.environ["ANTHROPIC_API_KEY"] = "test"
from myagent.gui import start_server, CONFIG_FILE
server, url = start_server(tempfile.mkdtemp())
import subprocess, json
r = subprocess.run(["node", "test_gui.js", url], capture_output=True, text=True, timeout=60)
print(r.stdout, r.stderr[-2000:])
assert "BROWSER OK" in r.stdout
assert CONFIG_FILE.exists() and oct(CONFIG_FILE.stat().st_mode)[-3:] == "600"
print("GUI FLOW OK")
