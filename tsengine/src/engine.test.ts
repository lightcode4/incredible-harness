import { test, expect } from "bun:test";
import { Store } from "./config";
import { EventHub } from "./events";
import { Session } from "./session";
import { route, KNOWN_ROUTES } from "./routes";

test("public config hides secrets and exposes UI fields", () => {
  const s = new Store();
  s.config.api_key = "sk-secret";
  const pub = s.publicConfig();
  expect(pub.api_key).toBe(true); // boolean, never the real key
  expect(typeof pub.theme).toBe("string");
  expect(Array.isArray(pub.mcp_status)).toBe(true);
  expect(pub.version).toBeDefined();
});

test("config route writes whitelisted keys and ignores unknown", async () => {
  const store = new Store();
  const events = new EventHub();
  const ctx = { store, events, session: new Session(store, events) };
  await route("/api/config", { theme: "black", scene: "kakashi", nope: "x" }, ctx);
  expect(store.config.theme).toBe("black");
  expect(store.config.scene).toBe("kakashi");
  expect("nope" in store.config).toBe(false);
});

test("every UI endpoint is routable (implemented or stubbed)", async () => {
  const store = new Store();
  const events = new EventHub();
  const ctx = { store, events, session: new Session(store, events) };
  for (const p of ["/api/artifacts/recent", "/api/jobs", "/api/teams", "/api/git"]) {
    const r = (await route(p, {}, ctx)) as Record<string, unknown>;
    expect(r).toBeDefined();
  }
  expect(KNOWN_ROUTES.length).toBeGreaterThan(100);
});

test("send without an API key emits a setup event and throws", async () => {
  const store = new Store();
  store.config.api_key = "";
  (store.config as any).keys = {};
  const events = new EventHub();
  const seen: string[] = [];
  events.subscribe((e) => seen.push(e.kind));
  const session = new Session(store, events);
  await expect(session.send("hi")).rejects.toThrow();
  expect(seen).toContain("setup");
});
