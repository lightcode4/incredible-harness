// API route table. Core endpoints are implemented; the long tail of endpoints
// from gui.py is stubbed to return {} so the existing UI degrades gracefully
// while they are ported one by one.

import type { Store } from "./config";
import type { EventHub } from "./events";
import type { Session } from "./session";
import * as gh from "./github";
import { listModels, testConnection, mergeCfg } from "./models";
import { homedir } from "os";
import { join } from "path";

export type Ctx = { store: Store; events: EventHub; session: Session };
type Handler = (body: any, ctx: Ctx) => unknown | Promise<unknown>;

// keys the UI is allowed to write through /api/config
const WRITABLE = new Set([
  "provider", "model", "effort", "base_url", "api_key", "cwd", "web", "compaction",
  "budget", "diagnostics", "theme", "scene", "effects", "cyber_mode", "auto_update",
  "onboarded", "name", "mode", "sound", "plan", "auto", "service", "language",
  "ui_language", "fast_model", "style", "style_custom", "sync_chats",
]);

// cancellation flag for an in-flight GitHub device-flow poll
let ghStop = { done: true };

const handlers: Record<string, Handler> = {
  "/api/config": (body, { store, events }) => {
    let changed = false;
    for (const [k, v] of Object.entries(body || {})) {
      if (k === "client" || k === "chat") continue;
      if (WRITABLE.has(k)) { store.config[k] = v; changed = true; }
    }
    if (changed) { store.save(); events.put({ kind: "config", config: store.publicConfig() }); }
    return store.publicConfig();
  },

  "/api/send": (body, { session }) => {
    const text = String(body?.text || "").trim();
    if (!text) throw new Error("Nothing to send.");
    // run the turn in the background; progress streams over SSE
    void session.send(text).catch(() => {});
    return { ok: true };
  },

  "/api/stop": (_b, { session }) => { session.stop(); return { ok: true }; },

  "/api/chats": (_b, { store }) => ({ chats: [], current: (store.config.last_chat as string) || "" }),

  "/api/models": async (body, { store }) => ({ models: await listModels(mergeCfg(store.config, body)) }),
  "/api/test-connection": async (body, { store }) => {
    try { return { ok: true, reply: await testConnection(mergeCfg(store.config, body)) }; }
    catch (e) { return { ok: false, error: (e as Error).message }; }
  },
  "/api/provider/check": async (body, { store }) => {
    try {
      const cfg = mergeCfg(store.config, body);
      if (body?.what === "models") return { ok: true, models: await listModels(cfg) };
      return { ok: true, reply: await testConnection(cfg) };
    } catch (e) { return { ok: false, error: (e as Error).message }; }
  },
  "/api/providers": (_b, { store }) => ({ providers: [], current: store.config.service || "" }),

  "/api/provider/key": (body, { store, events }) => {
    if (body?.key) store.config.api_key = body.key;
    if (body?.provider) store.config.provider = body.provider;
    if (body?.model) store.config.model = body.model;
    store.save();
    events.put({ kind: "config", config: store.publicConfig() });
    return store.publicConfig();
  },

  "/api/whatsnew": () => ({ html: "<p>What's new will appear here.</p>" }),

  // ---- GitHub (Settings > GitHub) ----
  "/api/github": (_b, { store }) => ({
    connected: Boolean(store.config.github_token),
    user: store.config.github_user || {},
    token_url: gh.TOKEN_URL,
    signin: Boolean((store.config as any).github_client_id || gh.CLIENT_ID),
  }),

  "/api/github/connect": async (body, { store, events }) => {
    const token = String(body?.token || "").trim();
    if (!token) throw new Error("Paste a GitHub token first.");
    const user = await gh.whoami(token);
    store.config.github_token = token;
    store.config.github_user = user;
    store.save();
    events.put({ kind: "config", config: store.publicConfig() });
    return { connected: true, user, token_url: gh.TOKEN_URL, signin: true };
  },

  "/api/github/signin": async (_b, { store, events }) => {
    const cid = (store.config as any).github_client_id || gh.CLIENT_ID;
    const r = await gh.deviceStart(cid);
    ghStop.done = true; // cancel any previous poll
    const myStop = { done: false };
    ghStop = myStop;
    const info = { code: r.user_code, url: r.verification_uri || "https://github.com/login/device" };
    events.put({ kind: "gh_device", ...info });
    // poll in the background; finish by itself once approved
    (async () => {
      try {
        const token = await gh.devicePoll(cid, r.device_code, r.interval || 5, r.expires_in || 900, () => myStop.done);
        const user = await gh.whoami(token);
        store.config.github_token = token;
        store.config.github_user = user;
        store.save();
        events.put({ kind: "gh_device", done: true, user });
        events.put({ kind: "config", config: store.publicConfig() });
      } catch (e) {
        if (!myStop.done) events.put({ kind: "gh_device", error: (e as Error).message });
      }
    })();
    return info;
  },

  "/api/github/disconnect": (_b, { store, events }) => {
    ghStop.done = true;
    store.config.github_token = "";
    store.config.github_user = {};
    store.save();
    events.put({ kind: "config", config: store.publicConfig() });
    return {};
  },

  "/api/github/repos": async (_b, { store }) => ({ repos: await gh.repos(String(store.config.github_token || "")) }),

  "/api/github/clone": async (body, { store }) => {
    const token = String(store.config.github_token || "");
    const full = String(body?.repo || "");
    if (!/^[\w.-]+\/[\w.-]+$/.test(full)) throw new Error("That doesn't look like a repository name (owner/repo).");
    const name = full.split("/")[1]!;
    const parent = String((store.config as any).github_folder || join(homedir(), "LightCode4"));
    const dest = join(parent, name);
    const url = `${gh.WEB.replace("https://", `https://x-access-token:${token}@`)}/${full}.git`;
    const proc = Bun.spawn(["git", "clone", url, dest], { stdout: "pipe", stderr: "pipe" });
    const err = await new Response(proc.stderr).text();
    await proc.exited;
    if (proc.exitCode !== 0 && !/already exists/.test(err)) throw new Error("Clone failed: " + err.slice(-300).replaceAll(token, "***"));
    return { path: dest };
  },

  // list-returning endpoints the UI reads fields off of on boot — shaped empties
  "/api/artifacts/recent": () => ({ artifacts: [] }),
  "/api/jobs": () => ({ jobs: [] }),
  "/api/schedules": () => ({ schedules: [] }),
  "/api/projects": () => ({ projects: [] }),
  "/api/teams": () => ({ teams: [] }),
  "/api/commands": () => ({ commands: [] }),
  "/api/context": () => ({ files: [], rules: { allow: [], deny: [] } }),
  "/api/git": () => ({ branch: "", files: [], branches: [] }),
  "/api/chats/search": () => ({ results: [] }),
};

// Everything else the UI may call — stubbed empty until ported. Prevents UI errors.
const STUBS = [
  "/api/approve", "/api/chrome", "/api/chrome/folder", "/api/github", "/api/github/connect",
  "/api/github/signin", "/api/github/disconnect", "/api/github/repos", "/api/github/clone",
  "/api/services", "/api/security/scan", "/api/security/tools", "/api/context", "/api/files/find",
  "/api/queue/now", "/api/queue/clear", "/api/checkpoints", "/api/checkpoints/restore", "/api/focus",
  "/api/mode", "/api/activate", "/api/chat/open", "/api/chats/order", "/api/chat/rename",
  "/api/chat/pin", "/api/chat/delete", "/api/worktree/finish", "/api/undo", "/api/jobs",
  "/api/jobs/stop", "/api/files/list", "/api/files/read", "/api/files/write", "/api/side/send",
  "/api/side/reset", "/api/side/stop", "/api/artifacts/recent", "/api/chats/search", "/api/projects",
  "/api/project/save", "/api/project/share", "/api/project/join", "/api/sync", "/api/teams",
  "/api/team/create", "/api/team/accept", "/api/team/invite", "/api/team/remove", "/api/team/sync",
  "/api/team/project", "/api/team/message", "/api/team/wallpaper", "/api/team/file/save",
  "/api/team/note", "/api/team/share-chat", "/api/team/open-chat", "/api/project/delete",
  "/api/project/file", "/api/project/export", "/api/project/chat", "/api/quick/send", "/api/quick/hide",
  "/api/quick/toggle", "/api/market", "/api/artifact/publish", "/api/market/source",
  "/api/market/install-url", "/api/market/install", "/api/gallery", "/api/gallery/skill", "/api/lsp",
  "/api/lsp/install", "/api/usage", "/api/artifact", "/api/continue", "/api/rewind", "/api/fork",
  "/api/compact", "/api/remote", "/api/instructions", "/api/memory", "/api/commands", "/api/schedules",
  "/api/git", "/api/export", "/api/transcribe", "/api/term/open", "/api/term/input", "/api/term/resize",
  "/api/term/close", "/api/browser/view", "/api/browser/input", "/api/browser/install",
  "/api/provider/check", "/api/mcp/login", "/api/selfcheck", "/api/update", "/api/update/install",
  "/api/share", "/api/report", "/api/logs/open", "/api/settings/export", "/api/settings/import",
  "/api/window/new", "/api/open-url", "/api/editor/ask",
];
for (const p of STUBS) if (!handlers[p]) handlers[p] = () => ({});

export function route(path: string, body: any, ctx: Ctx): unknown | Promise<unknown> {
  const h = handlers[path];
  if (!h) throw new Error(`Unknown endpoint: ${path}`);
  return h(body, ctx);
}

export const KNOWN_ROUTES = Object.keys(handlers);
