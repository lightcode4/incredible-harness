# LightCode⁴ — TypeScript/Bun engine + Tauri shell

A port of the Python agent engine to **TypeScript on Bun**, wrapped in a **Tauri** desktop
shell. The existing UI (`myagent/static/index.html`) is reused unchanged — only the backend
moves. This runs alongside the Python app (`myagent/`) until it reaches parity.

## Architecture

```
┌─────────────────────────── Tauri window (Rust, system webview) ───────────────────────────┐
│  main.rs: spawn the engine sidecar, read its URL, open the window pointed at it            │
└───────────────────────────────────────────────────────────────────────────────────────────┘
                                   │ localhost + token
┌──────────────────────────────── Bun engine (TypeScript) ──────────────────────────────────┐
│  server.ts   HTTP + SSE, token auth, serves the existing UI                                │
│  events.ts   event bus → SSE (same `kind` events the UI consumes)                          │
│  config.ts   settings load/save + public_config                                           │
│  session.ts  agent turn loop (text / tool / tool_result / turn_end events)                 │
│  providers.ts Anthropic + OpenAI-compatible streaming                                      │
│  tools.ts    read / write / edit / list / shell                                           │
│  routes.ts   /api/* table — core implemented, the long tail stubbed                        │
└───────────────────────────────────────────────────────────────────────────────────────────┘
```

Why Tauri + a Bun sidecar: Bun can't open a native window, and Tauri gives a tiny
system-webview window (~a few MB, low RAM) instead of bundling Chromium — matching the
performance goals. The engine stays a normal local HTTP server, exactly like the Python one,
so the UI talks to it with no changes.

## Develop

```bash
bun install
bun run dev          # starts the engine; open the printed http://127.0.0.1:…/?token=… URL
bun run typecheck
```

## Build the desktop app (CI does this for macOS + Windows)

```bash
bun run prepare:tauri   # compile the engine to a sidecar binary + stage the UI
bun run tauri build     # produces a .dmg (macOS) / NSIS setup .exe (Windows)
```

CI: `.github/workflows/build-ts.yml` publishes under the `ts-build-<n>` release tag.

## Port status

**Working:** UI served with token; SSE event stream (`config` + `replay` on connect);
settings read/write; a real streaming chat turn with the file/shell tools and the
agent loop; Anthropic + OpenAI-compatible providers. The full UI boots with no console errors.

**Stubbed (return empty, ported next):** GitHub, teams, projects, sync, marketplace,
security scan + cyber catalog, terminal, built-in browser, LSP, schedules, checkpoints,
computer-use, update/self-update, MCP. See `src/routes.ts` for the list.

Each stub maps 1:1 to a Python module (`myagent/*.py`) and is ported in turn, with the
Python app kept working throughout.
