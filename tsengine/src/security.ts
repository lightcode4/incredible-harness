// Bug hunting: a fast, offline static security scan of a project folder.
// TS port of security.py — flags well-known risky patterns and hard-coded secrets.
// First-pass signals, not proof; no code is run and nothing leaves the machine.

import { readdirSync, statSync, readFileSync, lstatSync } from "fs";
import { join, extname, relative } from "path";

const SKIP_DIRS = new Set([
  ".git", "node_modules", ".venv", "venv", "env", ".env.d", "dist", "build", "out",
  "__pycache__", ".next", ".nuxt", "target", "vendor", ".mypy_cache", ".pytest_cache",
  "coverage", ".idea", ".gradle", "Pods", ".terraform", "site-packages", ".cache",
  "bower_components", ".svn", ".hg",
]);
const TEXT_EXT = new Set([
  ".py", ".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs", ".vue", ".go", ".rb", ".php",
  ".java", ".kt", ".c", ".h", ".cc", ".cpp", ".cs", ".rs", ".swift", ".sh", ".bash",
  ".zsh", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf", ".env", ".properties",
  ".tf", ".sql", ".html", ".ipynb", ".txt", ".json",
]);
const MAX_BYTES = 1_500_000, MAX_LINE = 400, PER_FILE_CAP = 40;

const PY = new Set([".py"]);
const JS = new Set([".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs", ".vue"]);
const SH = new Set([".sh", ".bash", ".zsh"]);

type Sev = "high" | "medium" | "low";
type Rule = { id: string; severity: Sev; title: string; rx: RegExp; advice: string; exts?: Set<string> };

const RULES: Rule[] = [
  { id: "secret.aws_key", severity: "high", title: "Possible AWS access key id", rx: /\bAKIA[0-9A-Z]{16}\b/, advice: "Rotate the key immediately and load it from an environment variable or secrets manager." },
  { id: "secret.private_key", severity: "high", title: "Private key committed in source", rx: /-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----/, advice: "Remove the key from the repo, rotate it, and keep keys out of version control." },
  { id: "secret.github_token", severity: "high", title: "Possible GitHub token", rx: /\bgh[pousr]_[A-Za-z0-9]{36,}\b/, advice: "Revoke the token and store it as a secret, not in code." },
  { id: "secret.slack_token", severity: "high", title: "Possible Slack token", rx: /\bxox[baprs]-[0-9A-Za-z-]{10,}\b/, advice: "Revoke the token and move it to a secret store." },
  { id: "secret.google_key", severity: "medium", title: "Possible Google API key", rx: /\bAIza[0-9A-Za-z\-_]{35}\b/, advice: "Restrict and rotate the key; load it from configuration, not source." },
  { id: "secret.stripe_live", severity: "high", title: "Possible live Stripe secret key", rx: /\bsk_live_[0-9A-Za-z]{16,}\b/, advice: "Roll the key in the Stripe dashboard and never commit live keys." },
  { id: "secret.jwt", severity: "low", title: "Hard-coded JWT", rx: /\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{6,}\b/, advice: "If this token is sensitive, remove it and fetch it at runtime." },

  { id: "code.pickle_load", severity: "high", title: "Insecure deserialization (pickle)", rx: /\b(?:pickle|cPickle|_pickle)\.loads?\s*\(/, advice: "pickle executes arbitrary code on untrusted input. Use json, or a signed/validated format.", exts: PY },
  { id: "code.yaml_load", severity: "high", title: "Unsafe yaml.load()", rx: /\byaml\.load\s*\((?![^)]*Safe)/, advice: "Use yaml.safe_load(); yaml.load without SafeLoader can build arbitrary objects.", exts: PY },
  { id: "code.marshal_load", severity: "medium", title: "marshal.loads on untrusted data", rx: /\bmarshal\.loads?\s*\(/, advice: "marshal is not safe for untrusted input; prefer json.", exts: PY },
  { id: "code.exec_shell_true", severity: "high", title: "Shell command built with shell=True", rx: /\bsubprocess\.(?:run|call|Popen|check_output|check_call)\s*\([^)]*shell\s*=\s*True/, advice: "Pass an argument list and shell=False so user input can't inject commands.", exts: PY },
  { id: "code.os_system", severity: "high", title: "os.system() call", rx: /\bos\.system\s*\(/, advice: "Use subprocess with an argument list instead; os.system runs a shell.", exts: PY },
  { id: "code.py_eval", severity: "medium", title: "eval()/exec() on dynamic input", rx: /\b(?:eval|exec)\s*\(/, advice: "Avoid eval/exec on anything derived from input; use explicit parsing.", exts: PY },
  { id: "code.js_eval", severity: "medium", title: "eval() / Function() on dynamic input", rx: /\beval\s*\(|new\s+Function\s*\(/, advice: "Avoid eval and new Function on dynamic strings; they enable code injection.", exts: JS },
  { id: "code.child_exec", severity: "high", title: "Shell exec in Node", rx: /child_process\.(?:exec|execSync)\s*\(|(?<![A-Za-z_.])exec(?:Sync)?\s*\(\s*`/, advice: "Use execFile/spawn with an argument array instead of a shell string.", exts: JS },
  { id: "code.sql_fstring", severity: "high", title: "SQL built with an f-string", rx: /\.execute(?:many)?\s*\(\s*f['"]/, advice: "Use parameterized queries (placeholders), never string-formatted SQL.", exts: PY },
  { id: "code.sql_concat", severity: "medium", title: "SQL built by string concatenation", rx: /\.execute(?:many)?\s*\([^)]*['"]\s*\+/, advice: "Use parameterized queries instead of concatenating values into SQL.", exts: PY },
  { id: "code.sql_template", severity: "medium", title: "SQL built with a template literal", rx: /\.query\s*\(\s*`[^`]*\$\{/, advice: "Use parameterized queries ($1/?) rather than interpolating into SQL.", exts: JS },
  { id: "code.react_dangerous_html", severity: "medium", title: "dangerouslySetInnerHTML", rx: /dangerouslySetInnerHTML/, advice: "Sanitize HTML (e.g. DOMPurify) before injecting it, or avoid raw HTML.", exts: JS },

  { id: "config.tls_verify_off_py", severity: "high", title: "TLS verification disabled", rx: /verify\s*=\s*False/, advice: "Leaving verify=False lets anyone MITM the connection. Keep verification on.", exts: PY },
  { id: "config.tls_verify_off_node", severity: "high", title: "TLS verification disabled", rx: /rejectUnauthorized\s*:\s*false/, advice: "rejectUnauthorized:false disables certificate checks. Remove it.", exts: JS },
  { id: "config.tls_verify_off_go", severity: "high", title: "TLS verification disabled", rx: /InsecureSkipVerify\s*:\s*true/, advice: "InsecureSkipVerify:true disables certificate checks. Remove it.", exts: new Set([".go"]) },
  { id: "config.curl_insecure", severity: "medium", title: "curl called with --insecure", rx: /\bcurl\b[^\n]*\s(?:-k|--insecure)\b/, advice: "Drop -k/--insecure so TLS certificates are checked.", exts: SH },
  { id: "config.cors_wildcard", severity: "medium", title: "CORS allows any origin", rx: /Access-Control-Allow-Origin['"]?\s*[:,]\s*['"]\*|allow_origins\s*=\s*\[?\s*['"]\*/, advice: "Avoid '*' for credentialed APIs; list the origins you trust." },
  { id: "config.debug_on", severity: "medium", title: "Debug mode enabled", rx: /\b(?:app\.run\([^)]*debug\s*=\s*True|DEBUG\s*=\s*True)/, advice: "Disable debug in anything reachable beyond your machine; it can leak internals or allow code execution." },
  { id: "config.bind_all", severity: "low", title: "Service bound to 0.0.0.0", rx: /0\.0\.0\.0/, advice: "Binding to 0.0.0.0 exposes the service on every interface; bind to 127.0.0.1 if it's local-only." },

  { id: "crypto.weak_hash", severity: "low", title: "Weak hash (md5/sha1)", rx: /\b(?:md5|sha1)\s*\(/, advice: "md5/sha1 are broken for integrity/signatures; use SHA-256+. For passwords use bcrypt/argon2." },
];

const ASSIGN = /\b(\w*(?:secret|passwd|password|api[_-]?key|access[_-]?key|private[_-]?key|client[_-]?secret|auth[_-]?token|access[_-]?token)\w*)\b\s*[:=]\s*(["'])([^"']{8,160})\2/i;
const PLACEHOLDER = /^(?:your|xxx|changeme|change_me|example|placeholder|todo|none|null|true|false|test|dummy|sample|redacted|\*+|\.+|\$\{|\{\{|<|%|process\.env|os\.environ|getenv)/i;

function entropy(s: string): number {
  if (!s) return 0;
  const n = s.length, counts: Record<string, number> = {};
  for (const c of s) counts[c] = (counts[c] || 0) + 1;
  return -Object.values(counts).reduce((a, c) => a + (c / n) * Math.log2(c / n), 0);
}

function genericSecret(line: string): string | null {
  const m = ASSIGN.exec(line);
  if (!m) return null;
  const val = m[3]!.trim();
  const low = line.toLowerCase();
  if (PLACEHOLDER.test(val)) return null;
  if (["process.env", "os.environ", "getenv", "config(", "import.meta.env"].some((t) => low.includes(t))) return null;
  if (entropy(val) < 3.0 || new Set(val).size < 6) return null;
  return val;
}

type Finding = { id: string; severity: Sev; title: string; file: string; line: number; snippet: string; advice: string };

function* walk(root: string, maxFiles: number): Generator<string> {
  let seen = 0;
  const stack = [root];
  while (stack.length) {
    const d = stack.pop()!;
    let entries: string[];
    try { entries = readdirSync(d).sort(); } catch { continue; }
    for (const name of entries) {
      const p = join(d, name);
      let st; try { st = lstatSync(p); } catch { continue; }
      if (st.isDirectory()) {
        if (!SKIP_DIRS.has(name) && !st.isSymbolicLink()) stack.push(p);
      } else if (TEXT_EXT.has(extname(name).toLowerCase())) {
        try { if (statSync(p).size > MAX_BYTES) continue; } catch { continue; }
        yield p;
        if (++seen >= maxFiles) return;
      }
    }
  }
}

export function scan(root: string, maxFiles = 6000, maxFindings = 800) {
  const findings: Finding[] = [];
  let scanned = 0;
  try { if (!statSync(root).isDirectory()) throw 0; }
  catch { return { findings: [], counts: { high: 0, medium: 0, low: 0 }, scanned: 0, root, truncated: false }; }

  outer: for (const p of walk(root, maxFiles)) {
    scanned++;
    let text: string;
    try { text = readFileSync(p, "utf8"); } catch { continue; }
    const rel = relative(root, p).split("\\").join("/");
    const ext = extname(p).toLowerCase();
    let here = 0;
    const lines = text.split("\n");
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i]!;
      if (line.length > MAX_LINE || here >= PER_FILE_CAP) continue;
      for (const rule of RULES) {
        if (rule.exts && !rule.exts.has(ext)) continue;
        if (rule.rx.test(line)) {
          findings.push({ id: rule.id, severity: rule.severity, title: rule.title, file: rel, line: i + 1, snippet: line.trim().slice(0, 200), advice: rule.advice });
          here++;
        }
      }
      const val = genericSecret(line);
      if (val) {
        findings.push({ id: "secret.hardcoded", severity: "medium", title: "Possible hard-coded secret", file: rel, line: i + 1, snippet: line.trim().slice(0, 200), advice: "Load this from an environment variable or secrets manager instead of source." });
        here++;
      }
      if (findings.length >= maxFindings) break outer;
    }
  }
  const order: Record<Sev, number> = { high: 0, medium: 1, low: 2 };
  findings.sort((a, b) => order[a.severity] - order[b.severity] || a.file.localeCompare(b.file) || a.line - b.line);
  const counts = { high: 0, medium: 0, low: 0 };
  for (const f of findings) counts[f.severity]++;
  return { findings: findings.slice(0, maxFindings), counts, scanned, root, truncated: findings.length >= maxFindings };
}
