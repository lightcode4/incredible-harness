// Project instructions (CLAUDE.md etc.), user memory, and slash commands.
// TS port of project_instructions() (tools.py) + read/write_memory + commands() (extras.py).

import { readFileSync, writeFileSync, readdirSync, mkdirSync, existsSync, statSync } from "fs";
import { join } from "path";
import { homedir } from "os";

const HOME = join(homedir(), ".incredible-herness");
const MEMORY_FILE = join(HOME, "memory.md");
const INSTRUCTION_NAMES = ["LIGHTCODE.md", "HERNESS.md", "CLAUDE.md", "AGENTS.md", ".herness/instructions.md"];

export function projectInstructions(cwd: string): { text: string; names: string[] } {
  const names: string[] = [], parts: string[] = [];
  for (const name of INSTRUCTION_NAMES) {
    const p = join(cwd, name);
    try {
      if (existsSync(p) && statSync(p).isFile()) {
        parts.push(`## ${name}\n${readFileSync(p, "utf8").slice(0, 20000)}`);
        names.push(name);
      }
    } catch { /* skip */ }
  }
  return { text: parts.join("\n\n"), names };
}

export function instructionFile(cwd: string): string {
  return projectInstructions(cwd).names[0] || "HERNESS.md";
}

export function readMemory(): string {
  try { return readFileSync(MEMORY_FILE, "utf8").slice(0, 12000); } catch { return ""; }
}
export function writeMemory(text: string): void {
  mkdirSync(HOME, { recursive: true });
  writeFileSync(MEMORY_FILE, text);
}

// built-in slash commands: name -> [description, promptTemplate]
const BUILTIN: Record<string, [string, string]> = {
  review: ["Review uncommitted changes for bugs and security issues", "Review the uncommitted changes in this project for bugs and security issues. Read surrounding code where needed. Do not change files. Report findings as a numbered list, most severe first, each with file:line, what is wrong, a failing scenario, and a fix. $ARGUMENTS"],
  init: ["Study this project and write HERNESS.md", "Explore this project and write HERNESS.md in the root: what it is, how to install/run/test/build it (exact commands), the folder structure, and key conventions. Keep it under 80 lines. $ARGUMENTS"],
  test: ["Run the tests and fix failures", "Find how this project runs its tests, run them, and fix any failures. Re-run until they pass, then summarize what was wrong. $ARGUMENTS"],
  explain: ["Explain how this project or a file works", "Explain how this works, clearly and concisely, with the important files and the flow between them: $ARGUMENTS"],
  commit: ["Write a commit message and commit", "Look at `git status` and `git diff HEAD`, then commit the changes with a clear, specific message. Do not push. $ARGUMENTS"],
  fix: ["Fix a bug or error", "Find and fix this problem. Reproduce it first if you can, then verify the fix: $ARGUMENTS"],
};

export const UI_COMMANDS: Record<string, string> = {
  clear: "Start a new chat", plan: "Toggle plan mode", model: "Change model or provider",
  cost: "Show this chat's cost", export: "Export this chat", help: "List commands",
  parallel: "Start a parallel chat in its own worktree", memory: "Edit what LightCode⁴ remembers",
  compact: "Summarize the conversation to free up context", fork: "Copy this chat to try another direction",
};

function loadDir(dir: string): Record<string, [string, string]> {
  const out: Record<string, [string, string]> = {};
  try {
    if (!statSync(dir).isDirectory()) return out;
    for (const f of readdirSync(dir).sort()) {
      if (!f.endsWith(".md")) continue;
      let text = readFileSync(join(dir, f), "utf8");
      let desc = "";
      const m = /^---\s*\n([\s\S]*?)\n---\s*\n/.exec(text);
      if (m) {
        const dm = /^description:\s*(.+)$/m.exec(m[1]!);
        desc = dm ? dm[1]!.trim() : "";
        text = text.slice(m[0].length);
      }
      out[f.slice(0, -3).toLowerCase()] = [desc || `Custom command from ${dir}`, text.trim()];
    }
  } catch { /* no dir */ }
  return out;
}

export function commands(cwd: string): Record<string, [string, string]> {
  return {
    ...BUILTIN,
    ...loadDir(join(homedir(), ".claude", "commands")),
    ...loadDir(join(HOME, "commands")),
    ...loadDir(join(cwd, ".claude", "commands")),
    ...loadDir(join(cwd, ".herness", "commands")),
  };
}

export function commandList(cwd: string): { name: string; description: string; kind: string }[] {
  const out = Object.entries(UI_COMMANDS).map(([name, description]) => ({ name, description, kind: "ui" }));
  for (const [name, [description]] of Object.entries(commands(cwd))) out.push({ name, description, kind: "prompt" });
  return out;
}
