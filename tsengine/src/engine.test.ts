import { test, expect } from "bun:test";
import { mkdtempSync } from "fs";
import { tmpdir } from "os";
import { join } from "path";
import { Store } from "./config";
import { EventHub } from "./events";
import { ChatManager } from "./chats";
import { Terminals } from "./terminal";
import { Scheduler } from "./scheduler";
import { McpManager } from "./mcp";
import { route, KNOWN_ROUTES } from "./routes";
import { scan } from "./security";

function ctx() {
  const store = new Store();
  const events = new EventHub();
  const mcp = new McpManager();
  store.mcp = mcp;
  const chats = new ChatManager(store, events, mcp);
  const terms = new Terminals(events);
  const scheduler = new Scheduler(store, chats, events);
  return { store, events, chats, terms, scheduler, mcp };
}

test("public config hides secrets and exposes UI fields", () => {
  const s = new Store();
  s.config.api_key = "sk-secret";
  const pub = s.publicConfig();
  expect(pub.api_key).toBe(true);
  expect(typeof pub.theme).toBe("string");
  expect(Array.isArray(pub.mcp_status)).toBe(true);
});

test("config route writes whitelisted keys and ignores unknown", async () => {
  const c = ctx();
  await route("/api/config", { theme: "black", scene: "kakashi", nope: "x" }, c);
  expect(c.store.config.theme).toBe("black");
  expect(c.store.config.scene).toBe("kakashi");
  expect("nope" in c.store.config).toBe(false);
});

test("every UI endpoint is routable", async () => {
  const c = ctx();
  for (const p of ["/api/artifacts/recent", "/api/jobs", "/api/teams", "/api/git"]) {
    expect(await route(p, {}, c)).toBeDefined();
  }
  expect(KNOWN_ROUTES.length).toBeGreaterThan(100);
});

test("github status reports connection + signin availability", async () => {
  const st = (await route("/api/github", {}, ctx())) as any;
  expect(st.connected).toBe(false);
  expect(st.signin).toBe(true);
  expect(st.token_url).toContain("github.com");
});

test("github connect rejects an empty token", async () => {
  await expect(route("/api/github/connect", { token: "" }, ctx())).rejects.toThrow();
});

test("chats: new session creates and selects; list reflects it", async () => {
  const c = ctx();
  const before = ((await route("/api/chats", {}, c)) as any).chats.length;
  const created = (await route("/api/chat/new", { kind: "code" }, c)) as any;
  expect(created.id).toBeTruthy();
  const after = (await route("/api/chats", {}, c)) as any;
  expect(after.chats.length).toBe(before + 1);
  expect(after.current).toBe(created.id);
});

test("cyber catalog excludes jailbreak repos from actionable tools", async () => {
  const cat = (await route("/api/security/tools", {}, ctx())) as any;
  const repos: string[] = [];
  for (const g of cat.groups) for (const t of g.tools || []) if (t.repo) repos.push(t.repo);
  expect(repos).not.toContain("elder-plinius/L1B3RT4S");
  expect(repos).not.toContain("SnailSploit/Claude-Red");
  expect(cat.count).toBeGreaterThan(30);
});

test("security scanner flags a hard-coded AWS key and os.system", () => {
  const dir = mkdtempSync(join(tmpdir(), "scan-"));
  require("fs").writeFileSync(join(dir, "bad.py"), 'import os\nAWS = "AKIAIOSFODNN7EXAMPLE"\nos.system(cmd)\n');
  const r = scan(dir);
  const ids = r.findings.map((f) => f.id);
  expect(ids).toContain("secret.aws_key");
  expect(ids).toContain("code.os_system");
  expect(r.counts.high).toBeGreaterThan(0);
});

test("mcp: configure registers servers and reports status", () => {
  const { McpManager, toolName } = require("./mcp");
  const m = new McpManager();
  m.configure([{ name: "fs", command: "echo", args: ["hi"], enabled: true }, { name: "off", command: "x", enabled: false }]);
  const st = m.status();
  expect(st.length).toBe(1); // disabled one is skipped
  expect(st[0].name).toBe("fs");
  expect(toolName("fs", "read file!")).toBe("mcp_fs_read_file_");
  m.stop();
});

test("projects save and list; schedules persist", async () => {
  const c = ctx();
  const saved = (await route("/api/project/save", { project: { name: "Demo" } }, c)) as any;
  expect(saved.project.id).toBeTruthy();
  const list = (await route("/api/projects", {}, c)) as any;
  expect(list.projects.some((p: any) => p.name === "Demo")).toBe(true);
  await route("/api/schedules", { schedules: [{ id: "s1", name: "N", prompt: "x", every: 60, enabled: true }] }, c);
  expect((c.store.config.schedules as any[]).length).toBe(1);
});

test("fork creates a separate session and selects it", async () => {
  const c = ctx();
  const n0 = ((await route("/api/chats", {}, c)) as any).chats.length;
  const f = (await route("/api/fork", { idx: 0 }, c)) as any;
  const after = (await route("/api/chats", {}, c)) as any;
  expect(after.chats.length).toBe(n0 + 1);
  expect(after.current).toBe(f.id);
});

test("context report returns a window and token breakdown", async () => {
  const c = ctx();
  const r = (await route("/api/context", {}, c)) as any;
  expect(r.window).toBeGreaterThan(0);
  expect(Array.isArray(r.parts)).toBe(true);
  expect(r.parts.some((p: any) => p.name === "Messages")).toBe(true);
  const cp = (await route("/api/checkpoints", {}, c)) as any;
  expect(Array.isArray(cp.checkpoints)).toBe(true);
});

test("queue: send while busy enqueues, clear empties, replay carries queue", async () => {
  const c = ctx();
  const s = c.chats.current;
  const seen: any[] = [];
  const unsub = c.events.subscribe((e) => seen.push(e));
  s.busy = true;                     // simulate an in-progress turn
  await s.send("first queued");      // should enqueue, not throw
  await s.send("second queued");
  expect(s.queue.map((q) => q.text)).toEqual(["first queued", "second queued"]);
  const queuedEvents = seen.filter((e) => e.kind === "queued");
  expect(queuedEvents.length).toBe(2);
  expect(queuedEvents.at(-1).items).toEqual(["first queued", "second queued"]);
  // replay (used to restore the UI on reconnect) carries the pending queue
  const replay = s.replayEvent() as any;
  expect(replay.queue).toEqual(["first queued", "second queued"]);
  // clearing empties it and announces an empty queue
  s.queueClear();
  expect(s.queue.length).toBe(0);
  expect(seen.filter((e) => e.kind === "queued").at(-1).items).toEqual([]);
  unsub();
});

test("queue routes: /api/queue/now and /api/queue/clear are wired to the session", async () => {
  const c = ctx();
  expect(((await route("/api/queue/now", {}, c)) as any).ok).toBe(true);
  expect(((await route("/api/queue/clear", {}, c)) as any).ok).toBe(true);
});

test("providers catalog lists known services with key flags", async () => {
  const r = (await route("/api/providers", {}, ctx())) as any;
  const ids = r.providers.map((p: any) => p.id);
  expect(ids).toContain("claude");
  expect(ids).toContain("deepseek");
  expect(r.providers.find((p: any) => p.id === "ollama").has_key).toBe(true); // nokey
});
