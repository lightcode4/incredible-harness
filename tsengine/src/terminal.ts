// A lightweight line-mode terminal for xterm.js: runs commands with echo, backspace,
// Ctrl-C, `cd`, and streamed output. Not a full PTY (no vim/top), but covers running
// builds, tests and git — without a native pty dependency. (A true PTY via node-pty
// is the follow-up for full-screen apps.)

import { resolve } from "path";
import type { Subprocess } from "bun";
import type { EventHub } from "./events";

const PROMPT = (cwd: string) => `\r\n\x1b[36m${cwd.replace(/^.*\/([^/]+\/[^/]+)$/, "…/$1")}\x1b[0m $ `;

class Term {
  private buffer = "";
  private proc: Subprocess<any, any, any> | null = null;

  constructor(public id: string, public cwd: string, private events: EventHub) {
    this.write(`LightCode⁴ terminal — line mode. Runs commands in ${cwd}.`);
    this.prompt();
  }

  private write(data: string) { this.events.put({ kind: "term", id: this.id, data }); }
  private prompt() { this.write(PROMPT(this.cwd)); }

  async input(data: string): Promise<void> {
    for (const ch of data) {
      if (this.proc) {
        if (ch === "\x03") { this.proc.kill(); this.write("^C"); }
        else { try { (this.proc.stdin as any)?.write(ch); } catch {} }
        continue;
      }
      if (ch === "\r" || ch === "\n") { this.write("\r\n"); await this.run(this.buffer); this.buffer = ""; }
      else if (ch === "\x7f" || ch === "\b") { if (this.buffer) { this.buffer = this.buffer.slice(0, -1); this.write("\b \b"); } }
      else if (ch === "\x03") { this.write("^C"); this.buffer = ""; this.prompt(); }
      else if (ch >= " ") { this.buffer += ch; this.write(ch); }
    }
  }

  private async run(line: string): Promise<void> {
    const cmd = line.trim();
    if (!cmd) { this.prompt(); return; }
    // handle `cd` ourselves so directory changes persist between commands
    const cd = /^cd\s*(.*)$/.exec(cmd);
    if (cd) {
      const target = cd[1]!.trim() || process.env.HOME || "/";
      const next = resolve(this.cwd, target.replace(/^~(?=\/|$)/, process.env.HOME || "~"));
      try { if (require("fs").statSync(next).isDirectory()) this.cwd = next; else this.write(`cd: not a directory: ${target}\r\n`); }
      catch { this.write(`cd: no such file or directory: ${target}\r\n`); }
      this.prompt();
      return;
    }
    const shell = process.platform === "win32" ? ["cmd", "/c", cmd] : ["bash", "-lc", cmd];
    try {
      const proc = Bun.spawn(shell, { cwd: this.cwd, stdin: "pipe", stdout: "pipe", stderr: "pipe" });
      this.proc = proc;
      const pump = async (stream: ReadableStream<Uint8Array>) => {
        const r = stream.getReader(); const dec = new TextDecoder();
        for (;;) { const { done, value } = await r.read(); if (done) break; this.write(dec.decode(value).replace(/\n/g, "\r\n")); }
      };
      await Promise.all([pump(proc.stdout), pump(proc.stderr)]);
      await proc.exited;
    } catch (e) {
      this.write(`\r\n${(e as Error).message}\r\n`);
    } finally {
      this.proc = null;
      this.prompt();
    }
  }

  close(): void { try { this.proc?.kill(); } catch {} this.events.put({ kind: "term_exit", id: this.id }); }
}

export class Terminals {
  private terms = new Map<string, Term>();
  private n = 0;
  constructor(private events: EventHub) {}

  open(cwd: string): { id: string } {
    const id = "t" + (++this.n);
    this.terms.set(id, new Term(id, cwd, this.events));
    return { id };
  }
  input(id: string, data: string): void { void this.terms.get(id)?.input(data); }
  resize(): void { /* line mode ignores size */ }
  close(id: string): void { const t = this.terms.get(id); if (t) { t.close(); this.terms.delete(id); } }
}
