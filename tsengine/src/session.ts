// A chat session: identity + transcript + the agent turn loop, persisted to disk.
// Emits the same events the Python Chat emits (user / text / tool / tool_result /
// turn_end / status / usage / title).

import { join } from "path";
import { homedir } from "os";
import { mkdirSync, writeFileSync, readFileSync } from "fs";
import { randomBytes } from "crypto";
import type { EventHub } from "./events";
import type { Store } from "./config";
import { streamTurn, type Msg } from "./providers";
import { TOOL_SPECS, runTool } from "./tools";
import { projectInstructions, readMemory } from "./workspace";

export const CHATS_DIR = join(homedir(), ".incredible-herness", "chats");

const CYBER_PREAMBLE =
  "Cyber Security mode is ON. The user is authorized to test systems and apps they own, " +
  "in a lab they control. Help with security review, bug hunting and defensive analysis. " +
  "Refuse anything that targets third parties or enables real-world harm.\n\n";

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
  private stopFlag = false;

  constructor(private store: Store, private events: EventHub, init?: Partial<Session> & { transcript?: Msg[] }) {
    this.id = init?.id || "c" + randomBytes(5).toString("hex");
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
    try {
      mkdirSync(CHATS_DIR, { recursive: true });
      writeFileSync(join(CHATS_DIR, this.id + ".json"), JSON.stringify({
        id: this.id, title: this.title, kind: this.kind, cwd: this.cwd, pinned: this.pinned,
        created: this.created, updated: this.updated, transcript: this.transcript,
      }));
    } catch { /* best effort */ }
  }

  static load(store: Store, events: EventHub, id: string): Session | null {
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
      this.save();
      this.events.put({ kind: "turn_end" });
      this.events.put({ kind: "status", busy: false });
      this.events.put({ kind: "chats_changed" });
    }
  }
}
