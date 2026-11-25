"""Bug hunting: a fast, offline static security scan of a project folder.

It flags well-known risky patterns and hard-coded secrets so a review starts
with leads instead of a blank page. These are first-pass signals, not proof:
pattern matching both misses real bugs and cries wolf, so every finding needs a
human (or the agent) to confirm, and a clean scan is not a guarantee of safety.
No code is run and nothing leaves the machine."""
from __future__ import annotations

import math
import re
from collections import Counter
from pathlib import Path

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "env", ".env.d", "dist", "build",
             "out", "__pycache__", ".next", ".nuxt", "target", "vendor", ".mypy_cache",
             ".pytest_cache", "coverage", ".idea", ".gradle", "Pods", ".terraform",
             "site-packages", ".cache", "bower_components", ".svn", ".hg"}
TEXT_EXT = {".py", ".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs", ".vue", ".go", ".rb",
            ".php", ".java", ".kt", ".c", ".h", ".cc", ".cpp", ".cs", ".rs", ".swift",
            ".sh", ".bash", ".zsh", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf",
            ".env", ".properties", ".tf", ".sql", ".html", ".ipynb", ".txt", ".json"}
MAX_BYTES = 1_500_000   # skip huge or minified files
MAX_LINE = 400          # skip minified / generated lines
PER_FILE_CAP = 40       # one noisy file shouldn't drown everything

PY = {".py"}
JS = {".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs", ".vue"}
SH = {".sh", ".bash", ".zsh"}


class Rule:
    __slots__ = ("id", "severity", "title", "rx", "advice", "exts")

    def __init__(self, id, severity, title, pattern, advice, exts=None):
        self.id = id
        self.severity = severity
        self.title = title
        self.rx = re.compile(pattern)
        self.advice = advice
        self.exts = exts


RULES = [
    # ---- hard-coded secrets -------------------------------------------------
    Rule("secret.aws_key", "high", "Possible AWS access key id",
         r"\bAKIA[0-9A-Z]{16}\b",
         "Rotate the key immediately and load it from an environment variable or secrets manager."),
    Rule("secret.private_key", "high", "Private key committed in source",
         r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----",
         "Remove the key from the repo, rotate it, and keep keys out of version control."),
    Rule("secret.github_token", "high", "Possible GitHub token",
         r"\bgh[pousr]_[A-Za-z0-9]{36,}\b",
         "Revoke the token and store it as a secret, not in code."),
    Rule("secret.slack_token", "high", "Possible Slack token",
         r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b",
         "Revoke the token and move it to a secret store."),
    Rule("secret.google_key", "medium", "Possible Google API key",
         r"\bAIza[0-9A-Za-z\-_]{35}\b",
         "Restrict and rotate the key; load it from configuration, not source."),
    Rule("secret.stripe_live", "high", "Possible live Stripe secret key",
         r"\bsk_live_[0-9A-Za-z]{16,}\b",
         "Roll the key in the Stripe dashboard and never commit live keys."),
    Rule("secret.jwt", "low", "Hard-coded JWT",
         r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{6,}\b",
         "If this token is sensitive, remove it and fetch it at runtime."),

    # ---- dangerous code -----------------------------------------------------
    Rule("code.pickle_load", "high", "Insecure deserialization (pickle)",
         r"\b(?:pickle|cPickle|_pickle)\.loads?\s*\(",
         "pickle executes arbitrary code on untrusted input. Use json, or a signed/validated format.", PY),
    Rule("code.yaml_load", "high", "Unsafe yaml.load()",
         r"\byaml\.load\s*\((?![^)]*Safe)",
         "Use yaml.safe_load(); yaml.load without SafeLoader can build arbitrary objects.", PY),
    Rule("code.marshal_load", "medium", "marshal.loads on untrusted data",
         r"\bmarshal\.loads?\s*\(",
         "marshal is not safe for untrusted input; prefer json.", PY),
    Rule("code.exec_shell_true", "high", "Shell command built with shell=True",
         r"\bsubprocess\.(?:run|call|Popen|check_output|check_call)\s*\([^)]*shell\s*=\s*True",
         "Pass an argument list and shell=False so user input can't inject commands.", PY),
    Rule("code.os_system", "high", "os.system() call",
         r"\bos\.system\s*\(",
         "Use subprocess with an argument list instead; os.system runs a shell.", PY),
    Rule("code.py_eval", "medium", "eval()/exec() on dynamic input",
         r"\b(?:eval|exec)\s*\(",
         "Avoid eval/exec on anything derived from input; use explicit parsing.", PY),
    Rule("code.js_eval", "medium", "eval() / Function() on dynamic input",
         r"\beval\s*\(|new\s+Function\s*\(",
         "Avoid eval and new Function on dynamic strings; they enable code injection.", JS),
    Rule("code.child_exec", "high", "Shell exec in Node",
         r"child_process\.(?:exec|execSync)\s*\(|(?<![A-Za-z_.])exec(?:Sync)?\s*\(\s*`",
         "Use execFile/spawn with an argument array instead of a shell string.", JS),
    Rule("code.sql_fstring", "high", "SQL built with an f-string",
         r"\.execute(?:many)?\s*\(\s*f['\"]",
         "Use parameterized queries (placeholders), never string-formatted SQL.", PY),
    Rule("code.sql_concat", "medium", "SQL built by string concatenation",
         r"\.execute(?:many)?\s*\([^)]*['\"]\s*\+",
         "Use parameterized queries instead of concatenating values into SQL.", PY),
    Rule("code.sql_template", "medium", "SQL built with a template literal",
         r"\.query\s*\(\s*`[^`]*\$\{",
         "Use parameterized queries ($1/?) rather than interpolating into SQL.", JS),
    Rule("code.react_dangerous_html", "medium", "dangerouslySetInnerHTML",
         r"dangerouslySetInnerHTML",
         "Sanitize HTML (e.g. DOMPurify) before injecting it, or avoid raw HTML.", JS),

    # ---- insecure configuration --------------------------------------------
    Rule("config.tls_verify_off_py", "high", "TLS verification disabled",
         r"verify\s*=\s*False",
         "Leaving verify=False lets anyone MITM the connection. Keep verification on.", PY),
    Rule("config.tls_verify_off_node", "high", "TLS verification disabled",
         r"rejectUnauthorized\s*:\s*false",
         "rejectUnauthorized:false disables certificate checks. Remove it.", JS),
    Rule("config.tls_verify_off_go", "high", "TLS verification disabled",
         r"InsecureSkipVerify\s*:\s*true",
         "InsecureSkipVerify:true disables certificate checks. Remove it.", {".go"}),
    Rule("config.curl_insecure", "medium", "curl called with --insecure",
         r"\bcurl\b[^\n]*\s(?:-k|--insecure)\b",
         "Drop -k/--insecure so TLS certificates are checked.", SH),
    Rule("config.cors_wildcard", "medium", "CORS allows any origin",
         r"Access-Control-Allow-Origin['\"]?\s*[:,]\s*['\"]\*|allow_origins\s*=\s*\[?\s*['\"]\*",
         "Avoid '*' for credentialed APIs; list the origins you trust."),
    Rule("config.debug_on", "medium", "Debug mode enabled",
         r"\b(?:app\.run\([^)]*debug\s*=\s*True|DEBUG\s*=\s*True)",
         "Disable debug in anything reachable beyond your machine; it can leak internals or allow code execution."),
    Rule("config.bind_all", "low", "Service bound to 0.0.0.0",
         r"0\.0\.0\.0",
         "Binding to 0.0.0.0 exposes the service on every interface; bind to 127.0.0.1 if it's local-only."),

    # ---- weak crypto --------------------------------------------------------
    Rule("crypto.weak_hash", "low", "Weak hash (md5/sha1)",
         r"\b(?:md5|sha1)\s*\(",
         "md5/sha1 are broken for integrity/signatures; use SHA-256+. For passwords use bcrypt/argon2."),
]

_ASSIGN = re.compile(
    r"""(?ix)
    \b(?P<key>\w*(?:secret|passwd|password|api[_-]?key|access[_-]?key|
        private[_-]?key|client[_-]?secret|auth[_-]?token|access[_-]?token)\w*)\b
    \s*[:=]\s*
    (?P<q>["'])(?P<val>[^"']{8,160})(?P=q)
    """,
)
_PLACEHOLDER = re.compile(
    r"(?i)^(?:your|xxx|changeme|change_me|example|placeholder|todo|none|null|true|false|"
    r"test|dummy|sample|redacted|\*+|\.+|\$\{|\{\{|<|%|process\.env|os\.environ|getenv)")


def _entropy(s: str) -> float:
    if not s:
        return 0.0
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in Counter(s).values())


def _generic_secret(line: str):
    """A long, high-entropy value assigned to a secret-looking name — and not a placeholder or env lookup."""
    m = _ASSIGN.search(line)
    if not m:
        return None
    val = m.group("val").strip()
    low = line.lower()
    if _PLACEHOLDER.match(val):
        return None
    if any(t in low for t in ("process.env", "os.environ", "getenv", "config(", "import.meta.env")):
        return None
    if _entropy(val) < 3.0 or len(set(val)) < 6:
        return None
    return val


def _walk(root: Path, max_files: int):
    seen = 0
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = sorted(d.iterdir())
        except OSError:
            continue
        for p in entries:
            if p.is_dir():
                if p.name not in SKIP_DIRS and not p.is_symlink():
                    stack.append(p)
            elif p.suffix.lower() in TEXT_EXT:
                try:
                    if p.stat().st_size > MAX_BYTES:
                        continue
                except OSError:
                    continue
                yield p
                seen += 1
                if seen >= max_files:
                    return


def scan(root, max_files: int = 6000, max_findings: int = 800) -> dict:
    """Scan a folder and return {findings, counts, scanned, root, truncated}.

    Each finding: {id, severity, title, file, line, snippet, advice}."""
    root = Path(root)
    findings: list[dict] = []
    scanned = 0
    if not root.is_dir():
        return {"findings": [], "counts": {"high": 0, "medium": 0, "low": 0},
                "scanned": 0, "root": str(root), "truncated": False}
    for p in _walk(root, max_files):
        scanned += 1
        try:
            text = p.read_text(errors="replace")
        except OSError:
            continue
        rel = p.relative_to(root).as_posix()
        ext = p.suffix.lower()
        here = 0
        for i, line in enumerate(text.splitlines(), 1):
            if len(line) > MAX_LINE or here >= PER_FILE_CAP:
                continue
            for rule in RULES:
                if rule.exts and ext not in rule.exts:
                    continue
                if rule.rx.search(line):
                    findings.append({"id": rule.id, "severity": rule.severity, "title": rule.title,
                                     "file": rel, "line": i, "snippet": line.strip()[:200],
                                     "advice": rule.advice})
                    here += 1
            val = _generic_secret(line)
            if val:
                findings.append({"id": "secret.hardcoded", "severity": "medium",
                                 "title": "Possible hard-coded secret", "file": rel, "line": i,
                                 "snippet": line.strip()[:200],
                                 "advice": "Load this from an environment variable or secrets manager instead of source."})
                here += 1
            if len(findings) >= max_findings:
                break
        if len(findings) >= max_findings:
            break
    sev = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda f: (sev.get(f["severity"], 3), f["file"], f["line"]))
    counts = {"high": 0, "medium": 0, "low": 0}
    for f in findings:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
    return {"findings": findings[:max_findings], "counts": counts, "scanned": scanned,
            "root": str(root), "truncated": len(findings) >= max_findings}
