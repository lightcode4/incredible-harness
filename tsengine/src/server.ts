// HTTP + SSE server. Faithful to gui.py's handler: templates the token into the
// page, streams events on /api/events, and auth-gates the /api/* POST surface.

import { join } from "path";
import { existsSync } from "fs";
import { randomBytes } from "crypto";
import { EventHub, type Event } from "./events";
import { Store } from "./config";
import { Session } from "./session";
import { route } from "./routes";

const STATIC_DIR =
  process.env.LC4_STATIC || join(import.meta.dir, "../../myagent/static");

const MIME: Record<string, string> = {
  ".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css",
  ".json": "application/json", ".webp": "image/webp", ".png": "image/png", ".jpg": "image/jpeg",
  ".svg": "image/svg+xml", ".woff2": "font/woff2", ".woff": "font/woff", ".ttf": "font/ttf",
  ".ico": "image/x-icon", ".map": "application/json",
};
const mime = (p: string) => MIME[p.slice(p.lastIndexOf("."))] || "application/octet-stream";

export type Engine = { url: string; token: string; stop: () => void };

export function startEngine(opts: { port?: number; host?: string } = {}): Engine {
  const token = process.env.LC4_TOKEN || randomBytes(16).toString("hex");
  const store = new Store();
  const events = new EventHub();
  const session = new Session(store, events);
  const ctx = { store, events, session };

  async function serveStatic(path: string): Promise<Response | null> {
    const rel = path === "/" ? "/index.html" : path;
    const file = Bun.file(join(STATIC_DIR, rel));
    if (!(await file.exists())) return null;
    if (rel === "/index.html") {
      const html = (await file.text()).replaceAll("__TOKEN__", token);
      return new Response(html, { headers: { "content-type": "text/html; charset=utf-8" } });
    }
    return new Response(file, { headers: { "content-type": mime(rel), "cache-control": "max-age=3600" } });
  }

  function sseStream(): Response {
    let unsub = () => {};
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        const enc = new TextEncoder();
        const send = (ev: Event) => controller.enqueue(enc.encode(`data: ${JSON.stringify(ev)}\n\n`));
        // initial state the UI expects on connect
        send({ kind: "config", config: store.publicConfig() });
        send(session.replayEvent());
        unsub = events.subscribe(send);
        const ping = setInterval(() => controller.enqueue(enc.encode(": ping\n\n")), 15000);
        (controller as any)._cleanup = () => { clearInterval(ping); unsub(); };
      },
      cancel() { (this as any)._cleanup?.(); unsub(); },
    });
    return new Response(body, {
      headers: { "content-type": "text/event-stream", "cache-control": "no-cache", connection: "keep-alive" },
    });
  }

  const server = Bun.serve({
    port: opts.port ?? Number(process.env.LC4_PORT || 0),
    hostname: opts.host ?? "127.0.0.1",
    idleTimeout: 0,
    async fetch(req) {
      const url = new URL(req.url);
      const path = url.pathname;

      if (path === "/api/events") {
        if (url.searchParams.get("token") !== token) return new Response("forbidden", { status: 403 });
        return sseStream();
      }

      if (path.startsWith("/api/")) {
        if (req.method !== "POST") return json({ error: "POST required" }, 405);
        if (req.headers.get("x-token") !== token) return json({ error: "forbidden" }, 403);
        let body: any = {};
        try { body = await req.json(); } catch { body = {}; }
        try {
          const result = await route(path, body, ctx);
          return json(result ?? {});
        } catch (e) {
          return json({ error: (e as Error).message }, 400);
        }
      }

      const asset = await serveStatic(path);
      return asset ?? new Response("not found", { status: 404 });
    },
  });

  const url = `http://${server.hostname}:${server.port}/?token=${token}`;
  return { url, token, stop: () => server.stop(true) };
}

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json" } });
}
