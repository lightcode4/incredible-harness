"""Real end-to-end check against a real model (costs a little: the run stops at $0.50 for Claude).

Uses the provider, model, base URL and API key saved in LightCode⁴ (Settings > Model), stored in
~/.incredible-herness/settings.json. Works with Claude, OpenAI and OpenAI-compatible services (e.g. OpenCode).
The key is never printed.   Run:  python e2e_real.py
"""
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

BUDGET = 0.50


def make_pdf(pages):
    """A tiny valid PDF with one line of text per page."""
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


def main():
    settings = Path.home() / ".incredible-herness" / "settings.json"
    saved = json.loads(settings.read_text()) if settings.exists() else {}
    if not (saved.get("api_key") or os.environ.get("ANTHROPIC_API_KEY")):
        sys.exit("No API key saved: enter it in LightCode⁴ > Settings > Model, press Save, then run this again.")

    from myagent.gui import Session

    work = Path(tempfile.mkdtemp(prefix="herness-e2e-"))
    s = Session()
    s.load()  # the saved provider, model, base URL and key
    claude = s.config["provider"] == "claude"
    s.config.update(effort="low", budget=BUDGET, auto=True, schedules=[], phone=False, computer=False)
    s.apply_config({"cwd": str(work)})
    s.save = lambda: None  # never write test settings back to the user's settings file
    from myagent.gui import Stop
    chat, orig = s.current, s.current.before_request

    def capped():  # providers without known prices: cap tokens instead of dollars
        if chat.tokens > 400_000:
            raise Stop("Test token cap (400k) reached.")
        orig()
    chat.before_request = capped
    print(f"Testing {s.config['provider']} · {s.config['model']}" + (f" · {s.config['base_url']}" if s.config["base_url"] else ""), flush=True)
    events = []
    threading.Thread(target=lambda: [events.append(s.events.get()) for _ in iter(int, 1)], daemon=True).start()

    def run(prompt, timeout=240):
        start = len(events)
        s.send(prompt)
        t0 = time.time()
        while time.time() - t0 < timeout:
            if any(e.get("kind") == "status" and e.get("busy") is False for e in events[start:]):
                return events[start:]
            time.sleep(0.2)
        raise TimeoutError(prompt)

    def report(name, ok, detail=""):
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""), flush=True)
        return ok

    results = []
    # 1. Tools: write a file, run it, report the output
    evs = run("Create sum.py that prints the sum of the numbers 1 to 100, run it with python3, and tell me the output.")
    text = "".join(e.get("text", "") for e in evs if e["kind"] == "text")
    results.append(report("tools: write + run a file", (work / "sum.py").exists() and "5050" in text, f"reply mentions 5050: {'5050' in text}"))
    notices = [e["text"] for e in evs if e["kind"] == "notice"]
    if notices:
        print("   notices:", notices[:3])

    # 2. Second turn: prompt caching should read from cache
    evs = run("In one sentence: which file did you just create?")
    text = "".join(e.get("text", "") for e in evs if e["kind"] == "text")
    results.append(report("memory of the conversation", "sum.py" in text))

    # 3. Web: Claude's own web search, or the fetch_url tool for other providers
    if claude:
        evs = run("Use web search once to find the current stable version of Python, and answer in one short sentence.")
        ok = any(e["kind"] == "server_tool" for e in evs)
    else:
        evs = run("Use fetch_url on https://example.com and tell me the page's main heading, in a few words.")
        ok = any(e["kind"] == "tool" and e.get("name") == "fetch_url" for e in evs) and "example" in "".join(
            e.get("text", "") for e in evs if e["kind"] == "text").lower()
    results.append(report("web", ok))

    # 4. PDF reading (native pages)
    (work / "report.pdf").write_bytes(make_pdf(["Revenue was 42 million", "Profit grew 7 percent"]))
    evs = run("Use read_pdf on report.pdf and tell me what page 2 says, in one sentence.")
    text = "".join(e.get("text", "") for e in evs if e["kind"] == "text")
    results.append(report("PDF reading", "7" in text and "profit" in text.lower()))

    # 5. Sub-agent + todo list
    evs = run("Use the task tool once to have a sub-agent list the files in this folder, then reply with the count only.")
    results.append(report("sub-agent", any(e["kind"] == "tool" and e.get("name") == "task" for e in evs)))

    chat = s.current
    print(f"\nTotal: {chat.tokens} tokens" + (f", ${chat.cost:.4f} (limit ${BUDGET:.2f})" if claude else " (cost depends on your provider's prices)"))
    print(f"{sum(results)}/{len(results)} checks passed")
    s.shutdown()
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
