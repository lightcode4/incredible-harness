// Local MCP servers: programs on this computer that expose tools over the Model Context
// Protocol (JSON-RPC 2.0 over stdin/stdout). Their tools are offered to the agent as
// mcp_<server>_<tool>. TS port of mcp_local.py (local/stdio servers; remote+OAuth is separate).

import type { Subprocess } from "bun";
import type { ToolSpec } from "./providers";

const PROTOCOL = "2025-06-18";

export function toolName(server: string, tool: string): string {
  return ("mcp_" + `${server}_${tool}`.replace(/[^A-Za-z0-9_]/g, "_")).slice(0, 64);
}

type Entry = { name: string; command: string; args?: string[] | string; env?: Record<string, string>; enabled?: boolean };

class McpServer {
  proc: Subprocess<"pipe", "pipe", "pipe"> | null = null;
  tools: any[] = [];
  error = "";
  private nextId = 1;
  private pending = new Map<number, { resolve: (v: any) => void; reject: (e: any) => void }>();
  private stderrTail: string[] = [];

  constructor(public name: string, private command: string, private args: string[], private env: Record<string, string>) {}

  async start(): Promise<void> {
    this.proc = Bun.spawn([this.command, ...this.args], {
      stdin: "pipe", stdout: "pipe", stderr: "pipe",
      env: { ...process.env, ...this.env },
    }) as Subprocess<"pipe", "pipe", "pipe">;
    this.readLoop();
    this.readErr();
    await this.request("initialize", { protocolVersion: PROTOCOL, capabilities: {}, clientInfo: { name: "lightcode4", version: "1" } }, 60000);
    this.send({ jsonrpc: "2.0", method: "notifications/initialized" });
    let cursor: string | undefined, tools: any[] = [];
    for (;;) {
      const res = await this.request("tools/list", cursor ? { cursor } : {}, 60000);
      tools = tools.concat(res.tools || []);
      cursor = res.nextCursor;
      if (!cursor) break;
    }
    this.tools = tools;
  }

  private send(msg: unknown): void {
    if (!this.proc) throw new Error(`MCP server '${this.name}' is not running. ${this.stderrTail.slice(-3).join(" ")}`);
    this.proc.stdin.write(JSON.stringify(msg) + "\n");
    this.proc.stdin.flush();
  }

  request(method: string, params: unknown, timeout = 120000): Promise<any> {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      const t = setTimeout(() => { this.pending.delete(id); reject(new Error(`MCP server '${this.name}' did not answer ${method} in time.`)); }, timeout);
      const done = (fn: (v: any) => void) => (v: any) => { clearTimeout(t); fn(v); };
      this.pending.set(id, { resolve: done(resolve), reject: done(reject) });
      try { this.send({ jsonrpc: "2.0", id, method, params }); }
      catch (e) { clearTimeout(t); this.pending.delete(id); reject(e); }
    }).then((msg: any) => {
      if (msg.error) throw new Error(`${this.name}: ${msg.error.message || JSON.stringify(msg.error)}`);
      return msg.result || {};
    });
  }

  private async readLoop(): Promise<void> {
    const reader = this.proc!.stdout.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      let chunk; try { chunk = await reader.read(); } catch { break; }
      if (chunk.done) break;
      buf += dec.decode(chunk.value, { stream: true });
      let i: number;
      while ((i = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, i).trim(); buf = buf.slice(i + 1);
        if (!line) continue;
        let msg: any; try { msg = JSON.parse(line); } catch { continue; }
        if (msg.id != null && this.pending.has(msg.id) && ("result" in msg || "error" in msg)) {
          this.pending.get(msg.id)!.resolve(msg); this.pending.delete(msg.id);
        } else if (msg.id != null && msg.method) {
          try { this.send({ jsonrpc: "2.0", id: msg.id, error: { code: -32601, message: "Not supported" } }); } catch {}
        }
      }
    }
    for (const p of this.pending.values()) p.resolve({ error: { message: "server exited" } });
    this.pending.clear();
  }

  private async readErr(): Promise<void> {
    try {
      const reader = this.proc!.stderr.getReader(); const dec = new TextDecoder();
      for (;;) { const c = await reader.read(); if (c.done) break; this.stderrTail = this.stderrTail.concat(dec.decode(c.value).split("\n")).slice(-20); }
    } catch {}
  }

  async call(tool: string, args: unknown): Promise<string> {
    const res = await this.request("tools/call", { name: tool, arguments: args }, 300000);
    const parts: string[] = [];
    for (const c of res.content || []) {
      if (c.type === "text") parts.push(c.text);
      else if (c.type === "image") parts.push(`[image ${c.mimeType || "image/png"}]`);
      else if (c.type === "resource") parts.push(c.resource?.text || `[resource ${c.resource?.uri}]`);
    }
    if (!parts.length && res.structuredContent) parts.push(JSON.stringify(res.structuredContent).slice(0, 30000));
    return parts.join("\n") || "(no output)";
  }

  running(): boolean { return !!this.proc && this.proc.killed === false; }
  stop(): void { try { this.proc?.kill(); } catch {} this.proc = null; }
}

export class McpManager {
  private servers = new Map<string, McpServer>();
  private names = new Map<string, [string, string]>();
  private started = new Set<string>();
  private configKey = "";

  configure(entries: Entry[]): void {
    const list = (entries || []).filter((e) => e.name && e.command && e.enabled !== false);
    const key = JSON.stringify(list);
    if (key === this.configKey) return;
    this.stop();
    this.configKey = key;
    this.names.clear(); this.started.clear();
    for (const e of list) {
      let args = e.args || [];
      if (typeof args === "string") args = args.split(/\s+/).filter(Boolean);
      args = (args as string[]).map((a) => (a.startsWith("~") ? (process.env.HOME || "~") + a.slice(1) : a));
      this.servers.set(e.name, new McpServer(e.name, e.command, args as string[], e.env || {}));
    }
  }

  // Lazily start servers and return their tools as agent tool specs.
  async toolSpecs(): Promise<ToolSpec[]> {
    const specs: ToolSpec[] = [];
    for (const srv of this.servers.values()) {
      if (!this.started.has(srv.name)) {
        this.started.add(srv.name);
        try { await srv.start(); srv.error = ""; } catch (e) { srv.error = (e as Error).message; }
      }
      for (const t of srv.tools) {
        const n = toolName(srv.name, t.name);
        this.names.set(n, [srv.name, t.name]);
        const schema = t.inputSchema || { type: "object", properties: {} };
        if (!schema.type) schema.type = "object";
        specs.push({ name: n, description: `[${srv.name}] ${t.description || t.name}`.slice(0, 1000), parameters: schema });
      }
    }
    return specs;
  }

  has(name: string): boolean { return this.names.has(name); }

  async call(name: string, args: unknown): Promise<string> {
    const pair = this.names.get(name);
    if (!pair) throw new Error(`Unknown MCP tool ${name}.`);
    return this.servers.get(pair[0])!.call(pair[1], args);
  }

  status(): { name: string; running: boolean; tools: number; error: string; remote: boolean }[] {
    return [...this.servers.values()].map((s) => ({ name: s.name, running: s.running(), tools: s.tools.length, error: s.error, remote: false }));
  }

  stop(): void { for (const s of this.servers.values()) s.stop(); this.servers.clear(); }
}
