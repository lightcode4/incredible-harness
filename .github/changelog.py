"""Turn GitHub's auto-generated release notes (bullets on stdin) into a Summary / Added / Changed / Fixed
changelog, the way the What's new view shows it. Falls back safely: unknown lines go under Changed."""
import re
import sys

build = sys.argv[1] if len(sys.argv) > 1 else ""
added, changed, fixed = [], [], []
for line in sys.stdin:
    m = re.match(r"\s*[-*]\s+(.*)", line)
    if not m:
        continue
    text = m.group(1)
    text = re.sub(r"\s+by @[\w-]+.*$", "", text)       # strip "by @user in #123"
    text = re.sub(r"\s*\(#\d+\)\s*$", "", text).strip()
    text = text.rstrip(".")
    if not text or text.lower().startswith(("test:", "merge ", "bump ", "ci:")):
        continue
    low = text.lower()
    if low.startswith(("add", "new ", "introduce", "support")):
        added.append(text)
    elif low.startswith(("fix", "resolve", "repair", "correct", "prevent")):
        fixed.append(text)
    else:
        changed.append(text)

n = len(added) + len(changed) + len(fixed)
out = []
if n:
    bits = []
    if added:
        bits.append(f"{len(added)} new")
    if changed:
        bits.append(f"{len(changed)} improved")
    if fixed:
        bits.append(f"{len(fixed)} fixed")
    out.append("## Summary\n")
    out.append(f"Build {build} — " + ", ".join(bits) + ".\n")


def section(title, items):
    if items:
        out.append(f"## {title}\n")
        out.extend(f"- {i}" for i in items)
        out.append("")


section("✨ Added", added)
section("✦ Changed", changed)
section("🐛 Fixed", fixed)
sys.stdout.write("\n".join(out).strip() + "\n")
