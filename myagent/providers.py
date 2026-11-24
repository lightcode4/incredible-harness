"""Model providers. Each keeps its own conversation history and runs the agent loop:
ask the model -> run the tools it calls -> send results back -> repeat until it answers.

- ClaudeProvider: official Anthropic SDK (streaming, adaptive thinking, refusal fallbacks, prompt caching,
  server-side compaction, web search/fetch, MCP connectors).
- OpenAIProvider: OpenAI SDK; works with any OpenAI-compatible server (OpenRouter, Groq, Ollama, LM Studio...).

User messages are lists of neutral parts:
  {"type": "text", "text"} | {"type": "image", "media_type", "data"} | {"type": "pdf", "name", "data"}
  | {"type": "file", "name", "text"}
"""

from __future__ import annotations

import json
import time
import os
from typing import Callable, Protocol

from .tools import WORKSPACE_TOOLS

RunTool = Callable[[str, dict], "tuple[str, bool]"]
MAX_STEPS = 80  # tool rounds per user message


class UI(Protocol):
    def text(self, chunk: str) -> None: ...
    def thinking(self, chunk: str) -> None: ...
    def end_block(self) -> None: ...
    def notice(self, msg: str) -> None: ...
    def server_tool(self, summary: str) -> None: ...
    def usage(self, model: str, usage: dict) -> None: ...
    def before_request(self) -> None: ...  # may raise to stop (budget reached, Stop pressed)


# $ per million tokens: (input, output, cache read). Cache writes bill at 1.25x input.
PRICES = {
    "claude-fable-5-2": (8, 40, 0.20), "claude-fable-5-1": (10, 50, 1.0), "claude-fable-5": (10, 50, 1.0),
    "claude-opus-5-5": (4, 20, 0.20), "claude-opus-5": (5, 25, 0.50), "claude-opus-4-8": (5, 25, 0.50),
    "claude-opus-4-7": (5, 25, 0.50), "claude-opus-4-6": (5, 25, 0.50),
    "claude-sonnet-5-5": (2, 10, 0.10), "claude-sonnet-5": (2, 10, 0.20), "claude-sonnet-4-6": (3, 15, 0.30),
    "claude-haiku-5-5": (0.10, 0.50, 0.01), "claude-haiku-4-5": (1, 5, 0.10),
}


def cost_of(model: str, u: dict) -> float | None:
    p = PRICES.get(model)
    if not p:
        return None
    return (u.get("input", 0) * p[0] + u.get("output", 0) * p[1] + u.get("cache_read", 0) * p[2]
            + u.get("cache_write", 0) * p[0] * 1.25) / 1_000_000


def _pdf_text(p: dict) -> str:
    if p.get("text"):
        return p["text"]
    import base64

    from .tools import ToolError, pdf_extract
    try:
        return pdf_extract(base64.b64decode(p["data"]))[0][:200_000]
    except ToolError as e:
        return f"(could not read this PDF: {e})"


PARALLEL_TOOLS = {"task"}


def run_calls(run_tool, calls: list) -> list:
    """Run one round of tool calls. Several sub-agent (task) calls run at the same time; everything else runs in
    order. Results come back in the original order."""
    results: list = [None] * len(calls)
    par = [i for i, (n, _) in enumerate(calls) if n in PARALLEL_TOOLS]
    futures = {}
    if len(par) > 1:
        from concurrent.futures import ThreadPoolExecutor
        pool = ThreadPoolExecutor(max_workers=min(len(par), 4))
        futures = {i: pool.submit(run_tool, *calls[i]) for i in par}
        pool.shutdown(wait=False)
    try:
        for i, (n, a) in enumerate(calls):
            if i not in futures:
                results[i] = run_tool(n, a)
        for i, f in futures.items():
            results[i] = f.result()
    finally:
        for f in futures.values():
            f.cancel()
    return results


def _result_text(content) -> str:
    """Text-only form of a tool result (for providers without native image/PDF support in tool results)."""
    if isinstance(content, str):
        return content
    return "\n".join(p["text"] if p["type"] in ("text", "pdf") else "" for p in content)


def _text_of(parts) -> str:
    if isinstance(parts, str):
        return parts
    return "\n".join(p["text"] for p in parts if p["type"] == "text")


# --------------------------------------------------------------------------- staying connected
TRANSIENT_NAMES = {"APIConnectionError", "APITimeoutError", "ConnectError", "ConnectTimeout", "ReadError", "ReadTimeout",
                   "WriteError", "RemoteProtocolError", "ProtocolError", "InternalServerError", "RateLimitError",
                   "OverloadedError", "ServiceUnavailableError", "IncompleteRead", "ChunkedEncodingError"}
RETRY_FOR = 30 * 60  # keep trying for up to 30 minutes of lost connection


def is_transient(e: BaseException) -> bool:
    """Errors worth waiting out: no internet, timeouts, dropped streams, rate limits, busy or failing servers."""
    code = getattr(e, "status_code", None)
    if code in (408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524, 529):
        return True
    if type(e).__name__ in TRANSIENT_NAMES or isinstance(e, (ConnectionError, TimeoutError)):
        return True
    return "overloaded" in str(e).lower()


def with_reconnect(ui, call):
    """Run one model request; if the connection drops, wait (2s, 4s, 8s ... up to 60s) and repeat that same step.
    Nothing is added to the conversation until a request completes, so a retry picks up exactly where it stopped."""
    started, attempt = time.time(), 0
    while True:
        try:
            result = call()
            if attempt and hasattr(ui, "reconnected"):
                ui.reconnected()
            return result
        except Exception as e:
            if not is_transient(e) or time.time() - started > RETRY_FOR:
                raise
            attempt += 1
            wait = min(60, 2 ** attempt)
            if hasattr(ui, "reconnecting"):
                ui.reconnecting(wait, attempt, e)
            for _ in range(wait * 2):
                ui.before_request()  # Stop still works while waiting
                time.sleep(0.5)


# --------------------------------------------------------------------------- Claude

_FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-2", "claude-fable-5-1", "claude-sonnet-5-5"}


class ClaudeProvider:
    def __init__(self, model: str, system: str, effort: str = "high", api_key: str | None = None,
                 tools: list[dict] = WORKSPACE_TOOLS, web: bool = True, compaction: bool = True,
                 mcp_servers: list[dict] | None = None):
        import anthropic

        if not (api_key or os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            raise ValueError("No Claude API key. Paste it in Settings")
        self.client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
        self.model, self.effort, self.system = model, effort, system
        self.web, self.compaction = web, compaction
        self.mcp_servers = [m for m in (mcp_servers or []) if m.get("name") and m.get("url")]
        self.messages: list = []
        self.label = f"claude:{model}"
        # Claude has built-in web search/fetch, so the client-side fetch tool is left out when web is on.
        self.tools = [
            {"name": t["name"], "description": t["description"], "input_schema": t["schema"], "eager_input_streaming": True}
            for t in tools if not (web and t.get("client_web"))
        ]

    def clear(self) -> None:
        self.messages = []

    def _request(self) -> dict:
        tools = list(self.tools)
        betas = []
        req = dict(model=self.model, max_tokens=64000, system=[{"type": "text", "text": self.system}],
                   messages=self.messages, thinking={"type": "adaptive", "display": "summarized"},
                   output_config={"effort": self.effort}, cache_control={"type": "ephemeral"})
        if self.web:
            tools += [{"type": "web_search_20260209", "name": "web_search"},
                      {"type": "web_fetch_20260209", "name": "web_fetch"}]
        if self.mcp_servers:
            betas.append("mcp-client-2025-11-20")
            req["mcp_servers"] = [{"type": "url", "url": m["url"], "name": m["name"],
                                   **({"authorization_token": m["token"]} if m.get("token") else {})}
                                  for m in self.mcp_servers]
            tools += [{"type": "mcp_toolset", "mcp_server_name": m["name"]} for m in self.mcp_servers]
        if self.compaction:
            betas.append("compact-2026-01-12")
            req["context_management"] = {"edits": [{"type": "compact_20260112"}]}
        if self.model in _FALLBACK_MODELS:
            betas.append("server-side-fallback-2026-07-01")
            req["fallbacks"] = "default"
        req["tools"] = tools
        if betas:
            req["betas"] = betas
        return req

    @staticmethod
    def _content(parts) -> list | str:
        if isinstance(parts, str):
            return parts
        blocks = []
        for p in parts:
            if p["type"] == "image":
                blocks.append({"type": "image", "source": {"type": "base64", "media_type": p["media_type"], "data": p["data"]}})
            elif p["type"] == "pdf":
                blocks.append({"type": "document", "title": p["name"][:200],
                               "source": {"type": "base64", "media_type": "application/pdf", "data": p["data"]}})
            elif p["type"] == "file":
                blocks.append({"type": "text", "text": f"Attached file {p['name']}:\n```\n{p['text']}\n```"})
        text = _text_of(parts)
        if text:
            blocks.append({"type": "text", "text": text})
        return blocks

    @staticmethod
    def _result_block(p: dict) -> dict:
        if p["type"] == "text":
            return {"type": "text", "text": p["text"]}
        if p["type"] == "pdf":  # Claude reads the real pages (layout, tables, scans)
            return {"type": "document", "title": p["name"][:200],
                    "source": {"type": "base64", "media_type": "application/pdf", "data": p["data"]}}
        return {"type": "image", "source": {"type": "base64", "media_type": p["media_type"], "data": p["data"]}}

    def _stream(self, ui: UI):
        try:
            with self.client.beta.messages.stream(**self._request()) as stream:
                current = None
                for event in stream:
                    if event.type == "content_block_start":
                        current = getattr(event.content_block, "type", None)
                    elif event.type == "content_block_delta":
                        if event.delta.type == "text_delta":
                            ui.text(event.delta.text)
                        elif event.delta.type == "thinking_delta":
                            ui.thinking(event.delta.thinking)
                    elif event.type == "content_block_stop":
                        block = getattr(event, "content_block", None)
                        kind = getattr(block, "type", None) or current
                        if kind in ("text", "thinking"):
                            ui.end_block()
                        elif kind == "server_tool_use":
                            inp = block.input if isinstance(block.input, dict) else {}
                            ui.server_tool(f"{block.name.replace('_', ' ')}: {inp.get('query') or inp.get('url') or ''}")
                        elif kind == "mcp_tool_use":
                            ui.server_tool(f"{block.server_name} · {block.name}")
                        elif kind == "compaction":
                            ui.notice("Earlier conversation was summarized to save context.")
                return stream.get_final_message()
        except ValueError as e:  # the SDK could not parse a streamed tool input
            ui.notice(f"Model sent malformed tool input ({e}); retrying.")
            return None

    def turn(self, parts, run_tool: RunTool, ui: UI) -> None:
        self.messages.append({"role": "user", "content": self._content(parts)})
        self._loop(run_tool, ui)

    def resume(self, run_tool: RunTool, ui: UI) -> bool:
        """Carry on a reply that was cut off (app closed, crash). False when there is nothing left to do."""
        if not self.messages:
            return False
        last = self.messages[-1]
        if last["role"] == "assistant":
            pending = [b for b in last["content"] if (getattr(b, "type", None) or (b.get("type") if isinstance(b, dict) else None)) == "tool_use"]
            if not pending:
                return False
            self.messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": getattr(b, "id", None) or b["id"], "is_error": True,
                 "content": "Not run: the app closed before this ran. Run it again if it is still needed."} for b in pending]})
        self._loop(run_tool, ui)
        return True

    def _loop(self, run_tool: RunTool, ui: UI) -> None:
        bad = 0
        for _ in range(MAX_STEPS):
            ui.before_request()
            msg = with_reconnect(ui, lambda: self._stream(ui))
            if msg is None:
                bad += 1
                if bad > 2:
                    ui.notice("Stopped: repeated malformed tool input.")
                    return
                continue
            u = msg.usage
            ui.usage(msg.model, {"input": u.input_tokens or 0, "output": u.output_tokens or 0,
                                 "cache_read": u.cache_read_input_tokens or 0,
                                 "cache_write": u.cache_creation_input_tokens or 0})
            for block in msg.content:
                if block.type == "fallback":
                    ui.notice(f"{block.from_.model} declined; continued on {block.to.model}.")
            self.messages.append({"role": "assistant", "content": msg.content})  # keep every block unchanged
            tool_uses = [b for b in msg.content if b.type == "tool_use"]

            if msg.stop_reason == "refusal":
                d = msg.stop_details
                ui.notice(f"The model declined this request{f' ({d.category})' if d and d.category else ''}.")
                self._answer_unrun(tool_uses, "Not run: the turn ended in a refusal.")
                return
            if msg.stop_reason in ("pause_turn", "compaction"):
                continue
            if msg.stop_reason == "max_tokens":
                if not tool_uses:
                    ui.notice("Reply hit the output limit.")
                    return
                self._answer_unrun(tool_uses, "Not run: output hit max_tokens mid tool call. Retry in smaller steps.")
                continue
            if not tool_uses:
                return
            results = []
            done = run_calls(run_tool, [(tu.name, tu.input) for tu in tool_uses])
            for tu, (out, is_error) in zip(tool_uses, done):
                if not isinstance(out, str):  # text + screenshot parts
                    out = [self._result_block(p) for p in out]
                results.append({"type": "tool_result", "tool_use_id": tu.id, "content": out, "is_error": is_error})
            self.messages.append({"role": "user", "content": results})
        ui.notice(f"Stopped after {MAX_STEPS} tool rounds. Say 'continue' to keep going.")

    def _answer_unrun(self, tool_uses, reason: str) -> None:
        if tool_uses:
            self.messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": t.id, "content": reason, "is_error": True} for t in tool_uses]})

    def close_interrupted(self) -> None:
        if not self.messages:
            return
        last = self.messages[-1]
        if last["role"] == "assistant":
            pending = [b for b in last["content"] if getattr(b, "type", None) == "tool_use"
                       or (isinstance(b, dict) and b.get("type") == "tool_use")]
            for b in pending:
                bid = b.id if hasattr(b, "id") else b["id"]
                self.messages.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": bid, "content": "Interrupted by the user.", "is_error": True}]})
        elif last["role"] == "user" and not any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in (last["content"] if isinstance(last["content"], list) else [])):
            self.messages.pop()  # the model never answered this message

    def last_text(self) -> str:
        for m in reversed(self.messages):
            if m["role"] == "assistant":
                return "\n".join(getattr(b, "text", "") or (b.get("text", "") if isinstance(b, dict) else "")
                                 for b in m["content"] if (getattr(b, "type", None) or (b.get("type") if isinstance(b, dict) else None)) == "text")
        return ""

    def serialize(self) -> list:
        out = []
        for m in self.messages:
            c = m["content"]
            if isinstance(c, list):
                c = [b.to_dict() if hasattr(b, "to_dict") else b for b in c]
            out.append({"role": m["role"], "content": c})
        return out

    def restore(self, messages: list) -> None:
        self.messages = messages

    def reset_with_summary(self, summary: str) -> None:
        self.compactions = getattr(self, "compactions", 0) + 1
        self.messages = [{"role": "user", "content": f"Summary of our conversation so far:\n{summary}"},
                         {"role": "assistant", "content": [{"type": "text", "text": "Understood. I'll continue from there."}]}]


# --------------------------------------------------------------------------- OpenAI / compatible / local

COMPACT_AT = 100_000  # prompt tokens before the history is summarized


class OpenAIProvider:
    def __init__(self, model: str, system: str, base_url: str | None = None, api_key: str | None = None,
                 label_prefix: str = "openai", tools: list[dict] = WORKSPACE_TOOLS, compaction: bool = True, **_):
        import openai

        kwargs = {}
        if base_url:
            kwargs["base_url"] = base_url
        if api_key:
            kwargs["api_key"] = api_key
        self.client = openai.OpenAI(**kwargs)
        self.model, self.system, self.compaction = model, system, compaction
        self.messages: list[dict] = [{"role": "system", "content": system}]
        self.label = f"{label_prefix}:{model}"
        self.last_prompt_tokens = 0
        self._usage_ok = True
        self.tools = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                        "parameters": t["schema"]}} for t in tools]

    def clear(self) -> None:
        self.messages = [{"role": "system", "content": self.system}]

    @staticmethod
    def _content(parts):
        if isinstance(parts, str):
            return parts
        out = []
        for p in parts:
            if p["type"] == "image":
                out.append({"type": "image_url", "image_url": {"url": f"data:{p['media_type']};base64,{p['data']}"}})
            elif p["type"] == "pdf":
                out.append({"type": "text", "text": f"Attached PDF {p['name']}:\n{_pdf_text(p)}"})
            elif p["type"] == "file":
                out.append({"type": "text", "text": f"Attached file {p['name']}:\n```\n{p['text']}\n```"})
        text = _text_of(parts)
        if text:
            out.append({"type": "text", "text": text})
        return text if len(out) == 1 and out[0]["type"] == "text" else out

    def _create(self, **kw):
        import openai

        if self._usage_ok:
            try:
                return self.client.chat.completions.create(stream_options={"include_usage": True}, **kw)
            except openai.BadRequestError:
                self._usage_ok = False  # some local servers reject stream_options
        return self.client.chat.completions.create(**kw)

    def _stream(self, ui: UI):
        stream = self._create(model=self.model, messages=self.messages, stream=True,
                              **({"tools": self.tools} if self.tools else {}))
        text, calls, finish = [], {}, None
        for chunk in stream:
            if getattr(chunk, "usage", None):
                u = chunk.usage
                cached = getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0) or 0
                self.last_prompt_tokens = u.prompt_tokens or 0
                ui.usage(self.model, {"input": (u.prompt_tokens or 0) - cached, "output": u.completion_tokens or 0,
                                      "cache_read": cached, "cache_write": 0})
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            reasoning = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
            if reasoning:
                ui.thinking(reasoning)
            if delta.content:
                text.append(delta.content)
                ui.text(delta.content)
            for tc in delta.tool_calls or []:
                slot = calls.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
                if tc.id:
                    slot["id"] = tc.id
                if tc.function and tc.function.name:
                    slot["name"] += tc.function.name
                if tc.function and tc.function.arguments:
                    slot["arguments"] += tc.function.arguments
            if choice.finish_reason:
                finish = choice.finish_reason
        ui.end_block()
        ordered = [calls[i] for i in sorted(calls)]
        for n, c in enumerate(ordered):
            c["id"] = c["id"] or f"call_{len(self.messages)}_{n}"
        return "".join(text), ordered, finish

    def _maybe_compact(self, ui: UI) -> None:
        if not self.compaction or self.last_prompt_tokens < COMPACT_AT or len(self.messages) < 6:
            return
        ui.notice("Summarizing earlier conversation to save context…")
        convo = json.dumps(self.messages[1:], default=str)[-400_000:]
        r = self.client.chat.completions.create(model=self.model, messages=[
            {"role": "system", "content": "Summarize this agent conversation so the work can continue: the user's goals, "
                                          "decisions, files changed, current state and next steps. Be specific and complete."},
            {"role": "user", "content": convo}])
        summary = r.choices[0].message.content or ""
        self.compactions = getattr(self, "compactions", 0) + 1
        self.messages = [{"role": "system", "content": self.system},
                         {"role": "user", "content": f"Summary of our conversation so far:\n{summary}"},
                         {"role": "assistant", "content": "Understood. I'll continue from there."}]
        self.last_prompt_tokens = 0
        self.compactions = getattr(self, "compactions", 0) + 1

    def turn(self, parts, run_tool: RunTool, ui: UI) -> None:
        with_reconnect(ui, lambda: self._maybe_compact(ui))
        self.messages.append({"role": "user", "content": self._content(parts)})
        self._loop(run_tool, ui)

    def resume(self, run_tool: RunTool, ui: UI) -> bool:
        """Carry on a reply that was cut off (app closed, crash). False when there is nothing left to do."""
        last = self.messages[-1] if self.messages else None
        if not last or last["role"] == "system":
            return False
        if last["role"] == "assistant":
            if not last.get("tool_calls"):
                return False
            answered = {m.get("tool_call_id") for m in self.messages if m["role"] == "tool"}
            for tc in last["tool_calls"]:
                if tc["id"] not in answered:
                    self.messages.append({"role": "tool", "tool_call_id": tc["id"],
                                          "content": "ERROR: not run, the app closed before this ran. Run it again if it is still needed."})
        self._loop(run_tool, ui)
        return True

    def _loop(self, run_tool: RunTool, ui: UI) -> None:
        for _ in range(MAX_STEPS):
            ui.before_request()
            text, calls, finish = with_reconnect(ui, lambda: self._stream(ui))
            assistant: dict = {"role": "assistant", "content": text or None}
            if calls:
                assistant["tool_calls"] = [{"id": c["id"], "type": "function",
                                            "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}} for c in calls]
            self.messages.append(assistant)
            if not calls:
                if finish == "length":
                    ui.notice("Reply hit the output limit.")
                return
            images = []
            parsed = []
            for c in calls:
                try:
                    parsed.append(json.loads(c["arguments"] or "{}"))
                except json.JSONDecodeError:
                    parsed.append(None)
            good = [i for i, a in enumerate(parsed) if a is not None and finish != "length"]
            ran = dict(zip(good, run_calls(run_tool, [(calls[i]["name"], parsed[i]) for i in good])))
            for i, c in enumerate(calls):
                if finish == "length":
                    out, err = "Not run: output hit the length limit mid tool call. Retry in smaller steps.", True
                elif i in ran:
                    out, err = ran[i]
                else:
                    out, err = json.dumps({"INVALID_JSON": c["arguments"][:2000]}), True
                if not isinstance(out, str):  # tool messages are text-only here; images follow as a user message
                    images += [p for p in out if p["type"] == "image"]
                    out = _result_text(out)
                self.messages.append({"role": "tool", "tool_call_id": c["id"], "content": f"ERROR: {out}" if err else out})
            if images:
                self.messages.append({"role": "user", "content": [{"type": "text", "text": "Screenshot from the browser tool:"}] + [
                    {"type": "image_url", "image_url": {"url": f"data:{p['media_type']};base64,{p['data']}"}} for p in images]})
        ui.notice(f"Stopped after {MAX_STEPS} tool rounds. Say 'continue' to keep going.")

    def close_interrupted(self) -> None:
        last = self.messages[-1]
        if last["role"] == "assistant" and last.get("tool_calls"):
            answered = {m.get("tool_call_id") for m in self.messages if m["role"] == "tool"}
            for tc in last["tool_calls"]:
                if tc["id"] not in answered:
                    self.messages.append({"role": "tool", "tool_call_id": tc["id"], "content": "ERROR: interrupted by the user."})
        elif last["role"] == "user":
            self.messages.pop()

    def last_text(self) -> str:
        for m in reversed(self.messages):
            if m["role"] == "assistant" and m.get("content"):
                return m["content"]
        return ""

    def serialize(self) -> list:
        return self.messages

    def restore(self, messages: list) -> None:
        self.messages = messages or [{"role": "system", "content": self.system}]

    def reset_with_summary(self, summary: str) -> None:
        self.messages = [{"role": "system", "content": self.system},
                         {"role": "user", "content": f"Summary of our conversation so far:\n{summary}"},
                         {"role": "assistant", "content": "Understood. I'll continue from there."}]
        self.last_prompt_tokens = 0
