// Workspace tools the agent can call. TS port of the core of tools.py (fs + shell).
// Each tool returns a short string result; the session loop emits tool / tool_result events.

import { readFileSync, writeFileSync, readdirSync, statSync, mkdirSync } from "fs";
import { join, dirname, resolve } from "path";
import type { ToolSpec } from "./providers";

export const TOOL_SPECS: ToolSpec[] = [
  {
    name: "read_file",
    description: "Read a UTF-8 text file in the working folder.",
    parameters: { type: "object", properties: { path: { type: "string" } }, required: ["path"] },
  },
  {
    name: "write_file",
    description: "Create or overwrite a text file.",
    parameters: { type: "object", properties: { path: { type: "string" }, content: { type: "string" } }, required: ["path", "content"] },
  },
  {
    name: "edit_file",
    description: "Replace the first exact occurrence of old_text with new_text in a file.",
    parameters: { type: "object", properties: { path: { type: "string" }, old_text: { type: "string" }, new_text: { type: "string" } }, required: ["path", "old_text", "new_text"] },
  },
  {
    name: "list_dir",
    description: "List the entries of a directory in the working folder.",
    parameters: { type: "object", properties: { path: { type: "string" } }, required: [] },
  },
  {
    name: "run_shell",
    description: "Run a shell command in the working folder and return its output.",
    parameters: { type: "object", properties: { command: { type: "string" } }, required: ["command"] },
  },
];

// Keep file access inside the working folder.
function within(cwd: string, p: string): string {
  const full = resolve(cwd, p || ".");
  if (full !== cwd && !full.startsWith(cwd + "/")) throw new Error("Path is outside the working folder.");
  return full;
}

export async function runTool(cwd: string, name: string, args: Record<string, any>): Promise<string> {
  switch (name) {
    case "read_file": {
      const f = within(cwd, args.path);
      const t = readFileSync(f, "utf8");
      return t.length > 60_000 ? t.slice(0, 60_000) + "\n…(truncated)" : t;
    }
    case "write_file": {
      const f = within(cwd, args.path);
      mkdirSync(dirname(f), { recursive: true });
      writeFileSync(f, String(args.content ?? ""));
      return `Wrote ${args.path} (${String(args.content ?? "").length} bytes).`;
    }
    case "edit_file": {
      const f = within(cwd, args.path);
      const cur = readFileSync(f, "utf8");
      if (!cur.includes(args.old_text)) return "old_text not found; no change made.";
      writeFileSync(f, cur.replace(args.old_text, args.new_text));
      return `Edited ${args.path}.`;
    }
    case "list_dir": {
      const d = within(cwd, args.path || ".");
      return readdirSync(d).map((n) => {
        try { return statSync(join(d, n)).isDirectory() ? n + "/" : n; } catch { return n; }
      }).join("\n") || "(empty)";
    }
    case "run_shell": {
      const proc = Bun.spawn(["bash", "-lc", String(args.command)], { cwd, stdout: "pipe", stderr: "pipe" });
      const [out, err] = await Promise.all([new Response(proc.stdout).text(), new Response(proc.stderr).text()]);
      await proc.exited;
      const body = (out + (err ? "\n" + err : "")).trim();
      return body.length > 40_000 ? body.slice(0, 40_000) + "\n…(truncated)" : body || "(no output)";
    }
    default:
      throw new Error(`Unknown tool: ${name}`);
  }
}
