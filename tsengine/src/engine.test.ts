import { test, expect } from "bun:test";
import { mkdtempSync } from "fs";
import { tmpdir } from "os";
import { join } from "path";
import { Store } from "./config";
import { EventHub } from "./events";
import { ChatManager } from "./chats";
import { route, KNOWN_ROUTES } from "./routes";
import { scan } from "./security";

function ctx() {
  const store = new Store();
  const events = new EventHub();
  const chats = new ChatManager(store, events);
  return { store, events, chats };
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

test("providers catalog lists known services with key flags", async () => {
  const r = (await route("/api/providers", {}, ctx())) as any;
  const ids = r.providers.map((p: any) => p.id);
  expect(ids).toContain("claude");
  expect(ids).toContain("deepseek");
  expect(r.providers.find((p: any) => p.id === "ollama").has_key).toBe(true); // nokey
});
