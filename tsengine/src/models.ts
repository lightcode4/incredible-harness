// Model discovery + connection test. TS port of list_models()/test_connection().

import type { Config } from "./config";
import providerCatalog from "./data/providers.json";

const SKIP = ["embed", "image", "tts", "whisper", "moderation", "dall-e", "transcribe"];
const BY_ID: Record<string, any> = Object.fromEntries((providerCatalog as any).providers.map((p: any) => [p.id, p]));

function key(cfg: Config): string {
  const keys = (cfg.keys as Record<string, string>) || {};
  return (cfg.api_key as string) || (cfg.service ? keys[cfg.service as string] : "") || keys[cfg.provider as string] || "";
}
function base(cfg: Config): string {
  const b = ((cfg.base_url as string) || "").replace(/\/+$/, "");
  return b || "https://api.openai.com/v1";
}

export async function listModels(cfg: Config): Promise<string[]> {
  let ids: string[] = [];
  if (cfg.provider === "claude") {
    const res = await fetch("https://api.anthropic.com/v1/models?limit=100", {
      headers: { "x-api-key": key(cfg), "anthropic-version": "2023-06-01" },
    });
    if (!res.ok) throw new Error(`Anthropic ${res.status}: ${(await res.text()).slice(0, 200)}`);
    ids = ((await res.json()) as any).data?.map((m: any) => m.id) || [];
  } else {
    const res = await fetch(base(cfg) + "/models", { headers: { authorization: `Bearer ${key(cfg)}` } });
    if (!res.ok) throw new Error(`${base(cfg)} answered ${res.status}. Use the service's API address (pick a preset under Service).`);
    const j = (await res.json()) as any;
    if (!Array.isArray(j.data)) throw new Error("That address answered, but not like an AI API. Pick a preset under Service.");
    ids = j.data.map((m: any) => m.id);
  }
  return ids.filter((i) => !SKIP.some((x) => i.includes(x))).sort();
}

export async function testConnection(cfg: Config): Promise<string> {
  if (cfg.provider === "claude") {
    const res = await fetch("https://api.anthropic.com/v1/messages", {
      method: "POST",
      headers: { "content-type": "application/json", "x-api-key": key(cfg), "anthropic-version": "2023-06-01" },
      body: JSON.stringify({ model: cfg.model, max_tokens: 16, messages: [{ role: "user", content: "Reply with: ok" }] }),
    });
    if (!res.ok) throw new Error(`Anthropic ${res.status}: ${(await res.text()).slice(0, 200)}`);
    const j = (await res.json()) as any;
    return (j.content?.map((b: any) => b.text || "").join("") || "ok").trim();
  }
  const res = await fetch(base(cfg) + "/chat/completions", {
    method: "POST",
    headers: { "content-type": "application/json", authorization: `Bearer ${key(cfg)}` },
    body: JSON.stringify({ model: cfg.model, max_tokens: 16, messages: [{ role: "user", content: "Reply with: ok" }] }),
  });
  if (!res.ok) throw new Error(`Provider ${res.status}: ${(await res.text()).slice(0, 200)}`);
  const j = (await res.json()) as any;
  return (j.choices?.[0]?.message?.content || "ok").trim();
}

// Merge the stored config with the overrides the UI sends when testing a provider.
export function mergeCfg(stored: Config, body: any): Config {
  const o: Config = { ...stored };
  for (const k of ["provider", "base_url", "service", "model"]) if (body?.[k] != null) o[k] = body[k];
  if (body?.key) o.api_key = body.key;
  // Resolve the catalog entry for the chosen service so the right API is used
  // (otherwise an OpenRouter/DeepSeek/etc. key wrongly hits the Anthropic endpoint).
  const svc = BY_ID[String(o.service || "")];
  if (svc) {
    o.provider = svc.kind;                              // "claude" | "openai" | "local"
    if (!o.base_url && svc.base) o.base_url = svc.base;  // keep an explicit base_url if given
  }
  return o;
}

// Which API a saved config talks to (used by send turns too).
export function resolveService(cfg: Config): Config {
  return mergeCfg(cfg, {});
}
