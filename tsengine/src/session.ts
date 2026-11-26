// A chat session: identity + transcript + the agent turn loop, persisted to disk.
// Emits the same events the Python Chat emits (user / text / tool / tool_result /
// turn_end / status / usage / title).

import { join, resolve, relative } from "path";
import { homedir } from "os";
import { mkdirSync, writeFileSync, readFileSync, existsSync } from "fs";
import { randomBytes } from "crypto";
import type { Event } from "./events";
import type { Store } from "./config";

// Anything the session can push events through (the real EventHub, or a side-chat wrapper that tags them).
export interface Emitter { put(ev: Event): void; }
import { streamTurn, type Msg } from "./providers";
import { TOOL_SPECS, runTool } from "./tools";
import { projectInstructions, readMemory } from "./workspace";

export const CHATS_DIR = join(homedir(), ".incredible-herness", "chats");

const CYBER_PREAMBLE =
  "Cyber Security mode is ON. The user is authorized to test systems and apps they own, " +
  "in a lab they control. Help with security review, bug hunting and defensive analysis. " +
  "Refuse anything that targets third parties or enables real-world harm.\n\n";

// Model context windows (tokens). Rough but good enough for the context rail.
export function contextWindow(model: string, override = 0): number {
  if (override) return override;
  const m = (model || "").toLowerCase();
  if (m.includes("claude") || m.includes("opus") || m.includes("sonnet") || m.includes("haiku")) return 200_000;
  if (m.includes("gpt-5") || m.includes("gpt-4.1") || m.includes("o3") || m.includes("o4")) return 400_000;
  if (m.includes("gpt-4o") || m.includes("gpt-4")) return 128_000;
  if (m.includes("gemini")) return 1_000_000;
  if (m.includes("deepseek")) return 128_000;
  if (m.includes("grok")) return 256_000;
  return 128_000;
}

type Checkpoint = { id: string; label: string; when: number; atMsg: number; files: Record<string, string | null> };

export type ChatMeta = {
  id: string; title: string; kind: string; cwd: string; pinned: boolean; updated: number; running: boolean;
};

export class Session {
  id: string;
  title = "";
  kind = "code";
  cwd: string;
  pinned = false;
  created = Date.now() / 1000;
  updated = Date.now() / 1000;
  transcript: Msg[] = [];
  busy = false;
  turnNo = 0;
  checkpoints: Checkpoint[] = [];
  ephemeral = false;
  private cur: Checkpoint | null = null;
  private stopFlag = false;

  constructor(private store: Store, private events: Emitter, init?: Partial<Session> & { transcript?: Msg[]; ephemeral?: boolean }) {
    this.id = init?.id || "c" + randomBytes(5).toString("hex");
    if (init?.ephemeral) this.ephemeral = true;
    this.cwd = init?.cwd || String(store.config.cwd);
    if (init?.title) this.title = init.title;
    if (init?.kind) this.kind = init.kind;
    if (init?.pinned) this.pinned = init.pinned;
    if (init?.created) this.created = init.created;
    if (init?.updated) this.updated = init.updated;
    if (init?.transcript) this.transcript = init.transcript;
  }

  meta(): ChatMeta {
    return { id: this.id, title: this.title || "Untitled", kind: this.kind, cwd: this.cwd, pinned: this.pinned, updated: this.updated, running: this.busy };
  }

  save(): void {
    if (this.ephemeral) return;
    try {
      mkdirSync(CHATS_DIR, { recursive: true });
      writeFileSync(join(CHATS_DIR, this.id + ".json"), JSON.stringify({
        id: this.id, title: this.title, kind: this.kind, cwd: this.cwd, pinned: this.pinned,
        created: this.created, updated: this.updated, transcript: this.transcript,
      }));
    } catch { /* best effort */ }
  }

  static load(store: Store, events: Emitter, id: string): Session | null {
    try {
      const d = JSON.parse(readFileSync(join(CHATS_DIR, id + ".json"), "utf8"));
      return new Session(store, events, d);
    } catch { return null; }
  }

  replayEvent() {
    return { kind: "replay", chat: { id: this.id, title: this.title, kind: this.kind }, transcript: this.transcript.map((m) => ({ role: m.role, content: m.content })) };
  }

  private system(): string {
    const cfg = this.store.config;
    const cyber = cfg.cyber_mode ? CYBER_PREAMBLE : "";
    let s = cyber + `You are LightCode⁴, a coding agent working in the folder ${this.cwd}. ` +
      `Be concise and act directly. Use the provided tools to read and change files and run commands.`;
    const mem = readMemory();
    if (mem.trim()) s += `\n\n# What the user asked you to remember\n${mem}`;
    const instr = projectInstructions(this.cwd).text;
    if (instr.trim()) s += `\n\n# Project instructions\n${instr}`;
    return s;
  }

  stop(): void { this.stopFlag = true; }

  private snapshot(path: string): void {
    if (!this.cur) return;
    const full = resolve(this.cwd, path);
    const rel = relative(this.cwd, full) || path;
    if (rel in this.cur.files) return;
    try { this.cur.files[rel] = existsSync(full) ? readFileSync(full, "utf8") : null; }
    catch { this.cur.files[rel] = null; }
  }

  checkpointList() {
    return this.checkpoints.slice().reverse().map((c) => ({ turn: c.id, label: c.label, when: c.when, files: Object.keys(c.files) }));
  }

  // Revert every file changed from this checkpoint onward back to its pre-change content.
  restoreTo(turn: string): number {
    const idx = this.checkpoints.findIndex((c) => c.id === turn);
    if (idx < 0) return 0;
    let restored = 0;
    for (let i = this.checkpoints.length - 1; i >= idx; i--) {
      for (const [rel, original] of Object.entries(this.checkpoints[i]!.files)) {
        const full = join(this.cwd, rel);
        try {
          if (original === null) { if (existsSync(full)) require("fs").rmSync(full); }
          else writeFileSync(full, original);
          restored++;
        } catch { /* skip */ }
      }
    }
    this.checkpoints.splice(idx);
    this.events.put({ kind: "fs_changed" });
    return restored;
  }

  // Restore the files recorded in a list of checkpoints (newest first) to their originals.
  private restoreCheckpoints(cps: Checkpoint[]): number {
    let restored = 0;
    for (const c of cps.slice().reverse()) {
      for (const [rel, original] of Object.entries(c.files)) {
        const full = join(this.cwd, rel);
        try {
          if (original === null) { if (existsSync(full)) require("fs").rmSync(full); }
          else writeFileSync(full, original);
          restored++;
        } catch { /* skip */ }
      }
    }
    return restored;
  }

  // Go back to message `idx`: drop later messages, optionally undo the files those turns changed.
  rewind(idx: number, text = "", undoFiles = true): number {
    let restored = 0;
    if (undoFiles) {
      const after = this.checkpoints.filter((c) => c.atMsg >= idx);
      restored = this.restoreCheckpoints(after);
      this.checkpoints = this.checkpoints.filter((c) => c.atMsg < idx);
    }
    this.transcript = this.transcript.slice(0, Math.max(0, idx));
    this.save();
    this.events.put(this.replayEvent());
    this.events.put({ kind: "fs_changed" });
    this.events.put({ kind: "chats_changed" });
    if (text.trim()) void this.send(text).catch(() => {});
    return restored;
  }

  undo(turn: string): number {
    const c = this.checkpoints.find((x) => x.id === turn);
    if (!c) return 0;
    const n = this.restoreCheckpoints([c]);
    this.checkpoints = this.checkpoints.filter((x) => x.id !== turn);
    this.events.put({ kind: "fs_changed" });
    return n;
  }

  // Copy the conversation up to `idx` into a brand-new session.
  forkMeta(idx: number): { cwd: string; kind: string; title: string; transcript: Msg[] } {
    return { cwd: this.cwd, kind: this.kind, title: this.title ? this.title + " (fork)" : "", transcript: this.transcript.slice(0, idx).map((m) => ({ ...m })) };
  }

  // Summarize the conversation so far to free up context.
  async compact(focus = ""): Promise<void> {
    if (this.busy || !this.transcript.length) return;
    this.busy = true;
    this.events.put({ kind: "status", busy: true });
    try {
      const prompt = `Summarize the conversation so far into a compact brief that preserves goals, decisions, file changes and open tasks${focus ? `, focusing on: ${focus}` : ""}. Write it as notes the assistant can continue from.`;
      const res = await streamTurn(this.store.config, "You compress conversations faithfully.",
        [...this.transcript, { role: "user", content: prompt }], [], {});
      this.transcript = [{ role: "user", content: "Summary of the conversation so far:\n\n" + (res.text || "(no summary)") }];
      this.save();
      this.events.put({ kind: "compacted", compactions: 1 });
      this.events.put(this.replayEvent());
    } catch (e) {
      this.events.put({ kind: "notice", text: (e as Error).message });
    } finally {
      this.busy = false;
      this.events.put({ kind: "status", busy: false });
    }
  }

  contextReport() {
    const est = (o: unknown) => Math.floor(JSON.stringify(o).length / 4);
    const model = String(this.store.config.model || "");
    const window = contextWindow(model, Number((this.store.config as any).context_window) || 0);
    const parts = [
      { name: "Messages", tokens: est(this.transcript) },
      { name: "Tools", tokens: est(TOOL_SPECS) },
      { name: "System prompt", tokens: Math.floor(this.system().length / 4) },
    ];
    const used = parts.reduce((a, p) => a + p.tokens, 0);
    const at = Math.floor(window * 0.85);
    const outputs = [...new Set(this.checkpoints.flatMap((c) => Object.keys(c.files)))].slice(0, 30);
    let branch = "";
    try {
      const p = Bun.spawnSync(["git", "rev-parse", "--abbrev-ref", "HEAD"], { cwd: this.cwd });
      if (p.exitCode === 0) branch = p.stdout.toString().trim();
    } catch {}
    return {
      window, used, parts, free: Math.max(0, window - used), autocompact_at: at,
      until_compact: Math.max(0, at - used), outputs, branch, folder: this.cwd, model, mode: this.kind,
    };
  }

  private haveKey(): boolean {
    const cfg = this.store.config, keys = (cfg.keys as any) || {};
    return Boolean(cfg.api_key || keys[cfg.service as string] || keys[cfg.provider as string]);
  }

  async send(text: string): Promise<void> {
    if (this.busy) throw new Error("The agent is already working. Stop it first.");
    if (!this.haveKey()) {
      this.events.put({ kind: "setup", text: "Add a provider API key in Settings first." });
      throw new Error("No API key configured.");
    }
    const cfg = this.store.config;
    this.stopFlag = false;
    this.busy = true;
    this.events.put({ kind: "user", text });
    this.events.put({ kind: "status", busy: true });
    this.transcript.push({ role: "user", content: text });
    this.turnNo++;
    this.cur = { id: "t" + this.turnNo, label: text.slice(0, 40), when: Date.now() / 1000, atMsg: this.transcript.length - 1, files: {} };
    if (!this.title) {
      this.title = text.slice(0, 48).replace(/\s+/g, " ").trim();
      this.events.put({ kind: "title", chat: this.id, title: this.title });
    }

    try {
      for (let step = 0; step < 24 && !this.stopFlag; step++) {
        const result = await streamTurn(cfg, this.system(), this.transcript, TOOL_SPECS, {
          onText: (d) => this.events.put({ kind: "text", text: d }),
          onThinking: (d) => this.events.put({ kind: "thinking", text: d }),
        });
        if (result.text) this.transcript.push({ role: "assistant", content: result.text });
        this.events.put({ kind: "end_block" });
        this.events.put({ kind: "usage", cost: 0, priced: false, tokens: result.usage.input + result.usage.output, context: result.usage.input, window: 0 });
        if (!result.toolCalls.length || this.stopFlag) break;
        const outs: string[] = [];
        for (const tc of result.toolCalls) {
          if (this.stopFlag) break;
          this.events.put({ kind: "tool", id: tc.id, name: tc.name, input: tc.args, summary: tc.name });
          if ((tc.name === "write_file" || tc.name === "edit_file") && tc.args?.path) this.snapshot(String(tc.args.path));
          let out: string;
          try { out = await runTool(this.cwd, tc.name, tc.args); }
          catch (e) { out = `Error: ${(e as Error).message}`; }
          this.events.put({ kind: "tool_result", id: tc.id, output: out });
          outs.push(`[${tc.name}] ${out}`);
        }
        this.transcript.push({ role: "user", content: "Tool results:\n" + outs.join("\n\n") });
      }
    } catch (e) {
      this.events.put({ kind: "notice", text: (e as Error).message });
    } finally {
      this.busy = false;
      this.updated = Date.now() / 1000;
      if (this.cur && Object.keys(this.cur.files).length) this.checkpoints.push(this.cur);
      this.cur = null;
      this.save();
      this.events.put({ kind: "turn_end" });
      this.events.put({ kind: "status", busy: false });
      this.events.put({ kind: "chats_changed" });
    }
  }
}
