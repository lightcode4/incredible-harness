// Marketplace: discover and install skills / agents / commands straight from GitHub
// repositories. Focused TS port of market.py (scan via the git tree API, browse with a
// small cache, install by downloading files into the local skill/agent/command dirs).

import { readFileSync, writeFileSync, mkdirSync, existsSync } from "fs";
import { join, dirname } from "path";
import { homedir } from "os";
import DEFAULT_SOURCES from "./data/market-sources.json";

const HOME = join(homedir(), ".incredible-herness");
const STATE_FILE = join(HOME, "market.json");
const CACHE_DIR = join(HOME, "market-cache");
const MAX_ITEMS = 120, MAX_DESCRIBE = 30, CACHE_TTL = 12 * 3600 * 1000;

export class MarketError extends Error {}

type Item = { id: string; kind: string; name: string; description: string };
type State = { sources: string[]; installed: string[] };

function state(): State {
  try { return JSON.parse(readFileSync(STATE_FILE, "utf8")); }
  catch { return { sources: [...(DEFAULT_SOURCES as string[])], installed: [] }; }
}
function saveState(s: State): void { mkdirSync(HOME, { recursive: true }); writeFileSync(STATE_FILE, JSON.stringify(s)); }

function parseSource(src: string): [string, string] {
  const parts = src.replace(/^https?:\/\/github\.com\//, "").replace(/\.git$/, "").split("/");
  const repo = parts.slice(0, 2).join("/");
  const sub = parts.slice(2).join("/");
  return [repo, sub];
}

async function gh(path: string, token: string): Promise<any> {
  const res = await fetch("https://api.github.com" + path, {
    headers: { accept: "application/vnd.github+json", "user-agent": "LightCode4", ...(token ? { authorization: `Bearer ${token}` } : {}) },
  });
  if (res.status === 403 || res.status === 429) throw new MarketError("GitHub rate limit reached. Connect GitHub in Settings to raise it (signed-in requests get a much higher limit).");
  if (res.status === 404) throw new MarketError("Repository not found (or private).");
  if (!res.ok) throw new MarketError(`GitHub said ${res.status}.`);
  return res.json();
}

async function raw(repo: string, path: string, token: string): Promise<string> {
  const r = await gh(`/repos/${repo}/contents/${encodeURI(path)}`, token);
  if (r.content) return Buffer.from(r.content, "base64").toString("utf8");
  return "";
}

async function tree(repo: string, token: string): Promise<string[]> {
  const info = await gh(`/repos/${repo}`, token);
  const t = await gh(`/repos/${repo}/git/trees/${info.default_branch}?recursive=1`, token);
  return (t.tree || []).filter((n: any) => n.type === "blob").map((n: any) => n.path as string);
}

function frontMatter(text: string): { name: string; description: string } {
  const m = /^---\s*\n([\s\S]*?)\n---/.exec(text);
  let name = "", description = "";
  if (m) {
    const nm = /^name:\s*(.+)$/m.exec(m[1]!); if (nm) name = nm[1]!.trim().replace(/^["']|["']$/g, "");
    const dm = /^description:\s*(.+)$/m.exec(m[1]!); if (dm) description = dm[1]!.trim().replace(/^["']|["']$/g, "");
  }
  return { name, description: description.slice(0, 240) };
}

export async function scan(src: string, token = ""): Promise<Item[]> {
  const [repo, sub] = parseSource(src);
  const files = await tree(repo, token);
  const scope = files.filter((f) => !sub || f.startsWith(sub + "/"));
  const found: [string, string, string][] = [];
  for (const f of scope) {
    if (found.length >= MAX_ITEMS) break;
    if (f.endsWith("/SKILL.md") || f === "SKILL.md") found.push(["skill", f.includes("/") ? f.slice(0, f.lastIndexOf("/")) : "", f]);
    else if (/(^|\/)agents\/[^/]+\.md$/.test(f) && !f.toLowerCase().endsWith("readme.md")) found.push(["agent", f, f]);
    else if (/(^|\/)commands\/[^/]+\.md$/.test(f) && !f.toLowerCase().endsWith("readme.md")) found.push(["command", f, f]);
  }
  const items: Item[] = [];
  const descs = await Promise.all(found.slice(0, MAX_DESCRIBE).map(async ([, , f]) => {
    try { return frontMatter(await raw(repo, f, token)); } catch { return { name: "", description: "" }; }
  }));
  found.forEach(([kind, ref, f], i) => {
    const d = descs[i] || { name: "", description: "" };
    const fallback = kind === "skill" ? (ref.split("/").pop() || repo.split("/")[1]!) : f.split("/").pop()!.replace(/\.md$/, "");
    items.push({ id: `${kind}:${ref}`, kind, name: d.name || fallback, description: d.description });
  });
  return items;
}

function cacheFile(src: string): string { return join(CACHE_DIR, src.replace(/[^\w.-]/g, "_") + ".json"); }

export async function browse(src: string, token = "", refresh = false): Promise<Item[]> {
  let c: any = null;
  if (!refresh) { try { c = JSON.parse(readFileSync(cacheFile(src), "utf8")); } catch {} }
  if (!c || Date.now() - c.t > CACHE_TTL) {
    try { c = { t: Date.now(), items: await scan(src, token), error: "" }; }
    catch (e) { c = { t: Date.now(), items: [], error: (e as Error).message }; if (refresh) { mkdirSync(CACHE_DIR, { recursive: true }); writeFileSync(cacheFile(src), JSON.stringify(c)); throw e; } }
    mkdirSync(CACHE_DIR, { recursive: true }); writeFileSync(cacheFile(src), JSON.stringify(c));
  }
  const installed = new Set(state().installed);
  return c.items.map((it: Item) => ({ ...it, source: src, installed: installed.has(`${src}|${it.id}`) }));
}

export function overview(): { repo: string; count: number | null; t: number; error: string }[] {
  return state().sources.map((s) => {
    let c: any = null; try { c = JSON.parse(readFileSync(cacheFile(s), "utf8")); } catch {}
    return { repo: s, count: c ? c.items.length : null, t: c?.t || 0, error: c?.error || "" };
  });
}

export function addSource(repo: string): string[] {
  const s = state();
  const norm = repo.replace(/^https?:\/\/github\.com\//, "").replace(/\.git$/, "").replace(/\/+$/, "");
  if (!/^[\w.-]+\/[\w.-]+/.test(norm)) throw new MarketError("Enter a GitHub repository as owner/repo.");
  if (!s.sources.includes(norm)) s.sources.unshift(norm);
  saveState(s);
  return s.sources;
}
export function removeSource(repo: string): string[] {
  const s = state(); s.sources = s.sources.filter((x) => x !== repo); saveState(s); return s.sources;
}

export async function install(src: string, id: string, token = ""): Promise<{ installed: string }> {
  const [repo] = parseSource(src);
  const [kind, ref] = [id.slice(0, id.indexOf(":")), id.slice(id.indexOf(":") + 1)];
  const files = await tree(repo, token);
  let targets: string[];
  let destDir: string;
  if (kind === "skill") {
    targets = files.filter((f) => f === `${ref}/SKILL.md` || f.startsWith(ref + "/"));
    destDir = join(homedir(), ".claude", "skills", ref.split("/").pop() || "skill");
  } else {
    targets = [ref];
    destDir = join(homedir(), ".claude", kind === "agent" ? "agents" : "commands");
  }
  if (!targets.length) throw new MarketError("Nothing to install for that item.");
  for (const f of targets) {
    const content = await raw(repo, f, token);
    const rel = kind === "skill" ? f.slice(ref.length + 1) : f.split("/").pop()!;
    const out = join(destDir, rel);
    mkdirSync(dirname(out), { recursive: true });
    writeFileSync(out, content);
  }
  const s = state(); const key = `${src}|${id}`;
  if (!s.installed.includes(key)) s.installed.push(key);
  saveState(s);
  return { installed: destDir };
}

export function uninstall(src: string, id: string): void {
  const s = state(); s.installed = s.installed.filter((x) => x !== `${src}|${id}`); saveState(s);
}

export function sources(): string[] { return state().sources; }
