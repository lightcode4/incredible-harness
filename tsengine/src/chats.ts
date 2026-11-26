// Chat manager: multiple persistent sessions (the sidebar's Sessions list + New session).
// TS port of the chat-management half of gui.py (list_chats / open_chat / new / rename / pin / delete).

import { readdirSync, rmSync, mkdirSync } from "fs";
import { join } from "path";
import type { Store } from "./config";
import type { EventHub } from "./events";
import { Session, CHATS_DIR, type Emitter } from "./session";
import type { McpManager } from "./mcp";

export class ChatManager {
  private chats = new Map<string, Session>();
  private sideChats = new Map<string, Session>();
  current: Session;

  constructor(private store: Store, private events: EventHub, private mcp?: McpManager) {
    // load saved chats
    try {
      mkdirSync(CHATS_DIR, { recursive: true });
      for (const f of readdirSync(CHATS_DIR)) {
        if (!f.endsWith(".json")) continue;
        const s = Session.load(store, events, f.slice(0, -5));
        if (s) { s.mcp = mcp; this.chats.set(s.id, s); }
      }
    } catch { /* first run */ }

    const last = String(store.config.last_chat || "");
    this.current = (last && this.chats.get(last)) || [...this.chats.values()].sort((a, b) => b.updated - a.updated)[0] || this.make();
  }

  private make(cwd?: string, kind?: string): Session {
    const s = new Session(this.store, this.events, { cwd: cwd || String(this.store.config.cwd), kind: kind || String(this.store.config.mode || "code"), mcp: this.mcp });
    this.chats.set(s.id, s);
    return s;
  }

  list(): ReturnType<Session["meta"]>[] {
    return [...this.chats.values()]
      .map((s) => s.meta())
      .sort((a, b) => Number(b.pinned) - Number(a.pinned) || b.updated - a.updated);
  }

  private select(s: Session): void {
    this.current = s;
    this.store.config.last_chat = s.id;
    this.store.save();
    this.events.put({ kind: "config", config: this.store.publicConfig() });
    this.events.put(s.replayEvent());
    this.events.put({ kind: "status", busy: s.busy });
  }

  newChat(cwd?: string, kind?: string): { id: string } {
    const s = this.make(cwd, kind);
    this.select(s);
    this.events.put({ kind: "chats_changed" });
    return { id: s.id };
  }

  // A side chat alongside the current one: ephemeral, seeded with the main transcript.
  // Its events are tagged chat:"side-<mainid>" so the UI routes them to the side panel.
  sideSend(text: string): { side: string } {
    const main = this.current;
    const sideId = "side-" + main.id;
    let side = this.sideChats.get(main.id);
    if (!side) {
      const tagged: Emitter = { put: (ev) => this.events.put({ ...ev, chat: sideId }) };
      side = new Session(this.store, tagged, {
        id: sideId, cwd: main.cwd, kind: main.kind, ephemeral: true,
        transcript: main.transcript.map((m) => ({ ...m })), mcp: this.mcp,
      });
      this.sideChats.set(main.id, side);
    }
    void side.send(text).catch(() => {});
    return { side: sideId };
  }
  sideReset(): void { const s = this.sideChats.get(this.current.id); s?.stop(); this.sideChats.delete(this.current.id); }
  sideStop(): void { this.sideChats.get(this.current.id)?.stop(); }

  fork(idx: number, text = ""): { id: string } {
    const meta = this.current.forkMeta(idx);
    const s = new Session(this.store, this.events, { ...meta, mcp: this.mcp });
    this.chats.set(s.id, s);
    this.select(s);
    this.events.put({ kind: "chats_changed" });
    if (text.trim()) void s.send(text).catch(() => {});
    return { id: s.id };
  }

  open(id: string): { id: string } {
    let s = this.chats.get(id) || Session.load(this.store, this.events, id);
    if (!s) throw new Error("That chat no longer exists.");
    s.mcp = this.mcp;
    this.chats.set(s.id, s);
    this.select(s);
    return { id: s.id };
  }

  rename(id: string, title: string): void {
    const s = this.chats.get(id);
    if (!s) return;
    s.title = title.slice(0, 120);
    s.save();
    this.events.put({ kind: "chats_changed" });
    if (this.current.id === id) this.events.put({ kind: "title", chat: id, title: s.title });
  }

  pin(id: string, on: boolean): void {
    const s = this.chats.get(id);
    if (!s) return;
    s.pinned = on;
    s.save();
    this.events.put({ kind: "chats_changed" });
  }

  delete(id: string): void {
    this.chats.delete(id);
    try { rmSync(join(CHATS_DIR, id + ".json")); } catch {}
    if (this.current.id === id) this.current = this.list()[0] ? this.chats.get(this.list()[0]!.id)! : this.make();
    this.events.put({ kind: "chats_changed" });
    this.select(this.current);
  }
}
