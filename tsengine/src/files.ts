// Workspace file endpoints (file panel / editor): list, read, write, find.
// TS port of the files/* endpoints from gui.py.

import { readdirSync, statSync, readFileSync, writeFileSync, mkdirSync } from "fs";
import { join, dirname, resolve, relative, extname } from "path";

const SKIP = new Set([".git", "node_modules", ".venv", "venv", "dist", "build", "__pycache__", ".next", "target", ".cache"]);

function within(cwd: string, p: string): string {
  const full = resolve(cwd, p || ".");
  if (full !== cwd && !full.startsWith(cwd + "/")) throw new Error("Path is outside the working folder.");
  return full;
}

export function list(cwd: string, path = ".") {
  const dir = within(cwd, path);
  const entries = readdirSync(dir).map((name) => {
    let isDir = false;
    try { isDir = statSync(join(dir, name)).isDirectory(); } catch {}
    return { name, dir: isDir };
  }).filter((e) => !(e.dir && SKIP.has(e.name)))
    .sort((a, b) => Number(b.dir) - Number(a.dir) || a.name.localeCompare(b.name));
  return { path: relative(cwd, dir) || ".", entries };
}

export function read(cwd: string, path: string) {
  const f = within(cwd, path);
  const content = readFileSync(f, "utf8");
  return { path, content: content.length > 500_000 ? content.slice(0, 500_000) : content, lang: extname(path).slice(1) };
}

export function write(cwd: string, path: string, content: string) {
  const f = within(cwd, path);
  mkdirSync(dirname(f), { recursive: true });
  writeFileSync(f, content ?? "");
  return { ok: true, path };
}

export function find(cwd: string, query: string, limit = 200) {
  const q = (query || "").toLowerCase();
  const out: string[] = [];
  const stack = ["."];
  while (stack.length && out.length < limit) {
    const rel = stack.pop()!;
    let entries: string[];
    try { entries = readdirSync(join(cwd, rel)); } catch { continue; }
    for (const name of entries) {
      const childRel = rel === "." ? name : rel + "/" + name;
      let isDir = false;
      try { isDir = statSync(join(cwd, childRel)).isDirectory(); } catch { continue; }
      if (isDir) { if (!SKIP.has(name)) stack.push(childRel); }
      else if (!q || name.toLowerCase().includes(q) || childRel.toLowerCase().includes(q)) {
        out.push(childRel);
        if (out.length >= limit) break;
      }
    }
  }
  return { files: out };
}
