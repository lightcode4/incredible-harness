// Model providers: stream a completion from Anthropic or any OpenAI-compatible endpoint.
// This is the TS port of the streaming half of providers.py (tool-use loop lives in session.ts).

import type { Config } from "./config";

export type Role = "system" | "user" | "assistant" | "tool";
export type Msg = { role: Role; content: string; tool_call_id?: string; name?: string };

export type ToolSpec = {
  name: string;
  description: string;
  parameters: Record<string, unknown>; // JSON schema
};

export type ToolCall = { id: string; name: string; args: Record<string, unknown> };

export type StreamResult = {
  text: string;
  toolCalls: ToolCall[];
  usage: { input: number; output: number };
};

export type StreamCallbacks = {
  onText?: (delta: string) => void;
  onThinking?: (delta: string) => void;
};

function apiKey(cfg: Config): string {
  const keys = (cfg.keys as Record<string, string>) || {};
  return (
    (cfg.api_key as string) ||
    (cfg.service ? keys[cfg.service as string] : "") ||
    keys[cfg.provider as string] ||
    ""
  );
}

function openAiBase(cfg: Config): string {
  const b = (cfg.base_url as string) || "";
  if (b) return b.replace(/\/+$/, "");
  if (cfg.provider === "openai") return "https://api.openai.com/v1";
  return "https://api.openai.com/v1";
}

// ---- Anthropic (claude) ----
async function streamAnthropic(
  cfg: Config, system: string, msgs: Msg[], tools: ToolSpec[], cb: StreamCallbacks,
): Promise<StreamResult> {
  const body = {
    model: cfg.model,
    max_tokens: 4096,
    system,
    stream: true,
    messages: msgs.filter((m) => m.role !== "system").map((m) => ({ role: m.role === "assistant" ? "assistant" : "user", content: m.content })),
    ...(tools.length
      ? { tools: tools.map((t) => ({ name: t.name, description: t.description, input_schema: t.parameters })) }
      : {}),
  };
  const res = await fetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-api-key": apiKey(cfg),
      "anthropic-version": "2023-06-01",
    },
    body: JSON.stringify(body),
  });
  if (!res.ok || !res.body) throw new Error(`Anthropic ${res.status}: ${(await res.text()).slice(0, 300)}`);

  const out: StreamResult = { text: "", toolCalls: [], usage: { input: 0, output: 0 } };
  const tool: { id?: string; name?: string; json: string } = { json: "" };
  for await (const ev of sse(res.body)) {
    const d = safeJson(ev.data);
    if (!d) continue;
    if (d.type === "content_block_start" && d.content_block?.type === "tool_use") {
      tool.id = d.content_block.id; tool.name = d.content_block.name; tool.json = "";
    } else if (d.type === "content_block_delta") {
      if (d.delta?.type === "text_delta") { out.text += d.delta.text; cb.onText?.(d.delta.text); }
      else if (d.delta?.type === "input_json_delta") tool.json += d.delta.partial_json || "";
      else if (d.delta?.type === "thinking_delta") cb.onThinking?.(d.delta.thinking || "");
    } else if (d.type === "content_block_stop" && tool.id) {
      out.toolCalls.push({ id: tool.id, name: tool.name!, args: safeJson(tool.json) || {} });
      tool.id = undefined;
    } else if (d.type === "message_delta" && d.usage) {
      out.usage.output += d.usage.output_tokens || 0;
    } else if (d.type === "message_start" && d.message?.usage) {
      out.usage.input += d.message.usage.input_tokens || 0;
    }
  }
  return out;
}

// ---- OpenAI-compatible (openai, deepseek, openrouter, grok, ollama, together, ...) ----
async function streamOpenAI(
  cfg: Config, system: string, msgs: Msg[], tools: ToolSpec[], cb: StreamCallbacks,
): Promise<StreamResult> {
  const messages = [
    ...(system ? [{ role: "system", content: system }] : []),
    ...msgs.map((m) => ({ role: m.role, content: m.content, ...(m.tool_call_id ? { tool_call_id: m.tool_call_id } : {}), ...(m.name ? { name: m.name } : {}) })),
  ];
  const body: Record<string, unknown> = { model: cfg.model, stream: true, messages };
  if (tools.length) {
    body.tools = tools.map((t) => ({ type: "function", function: { name: t.name, description: t.description, parameters: t.parameters } }));
  }
  const res = await fetch(openAiBase(cfg) + "/chat/completions", {
    method: "POST",
    headers: { "content-type": "application/json", authorization: `Bearer ${apiKey(cfg)}` },
    body: JSON.stringify(body),
  });
  if (!res.ok || !res.body) throw new Error(`Provider ${res.status}: ${(await res.text()).slice(0, 300)}`);

  const out: StreamResult = { text: "", toolCalls: [], usage: { input: 0, output: 0 } };
  const calls: Record<number, { id: string; name: string; args: string }> = {};
  for await (const ev of sse(res.body)) {
    if (ev.data === "[DONE]") break;
    const d = safeJson(ev.data);
    const delta = d?.choices?.[0]?.delta;
    if (delta?.content) { out.text += delta.content; cb.onText?.(delta.content); }
    for (const tc of delta?.tool_calls || []) {
      const slot = (calls[tc.index] ||= { id: tc.id || `call_${tc.index}`, name: "", args: "" });
      if (tc.id) slot.id = tc.id;
      if (tc.function?.name) slot.name += tc.function.name;
      if (tc.function?.arguments) slot.args += tc.function.arguments;
    }
    if (d?.usage) { out.usage.input += d.usage.prompt_tokens || 0; out.usage.output += d.usage.completion_tokens || 0; }
  }
  for (const c of Object.values(calls)) if (c.name) out.toolCalls.push({ id: c.id, name: c.name, args: safeJson(c.args) || {} });
  return out;
}

export async function streamTurn(
  cfg: Config, system: string, msgs: Msg[], tools: ToolSpec[], cb: StreamCallbacks,
): Promise<StreamResult> {
  if (cfg.provider === "claude") return streamAnthropic(cfg, system, msgs, tools, cb);
  return streamOpenAI(cfg, system, msgs, tools, cb);
}

// ---- small helpers ----
function safeJson(s: string): any {
  try { return JSON.parse(s); } catch { return null; }
}

// Parse a Server-Sent-Events stream into {event, data} records.
async function* sse(body: ReadableStream<Uint8Array>): AsyncGenerator<{ event: string; data: string }> {
  const reader = body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i: number;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
      let event = "message"; const data: string[] = [];
      for (const line of chunk.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) data.push(line.slice(5).trim());
      }
      if (data.length) yield { event, data: data.join("\n") };
    }
  }
}
