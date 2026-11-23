"""Model providers. Each one keeps its own conversation history and runs the agent loop:
ask the model -> run the tools it calls -> send results back -> repeat until it answers.

- ClaudeProvider: the official Anthropic SDK (streaming, adaptive thinking, refusal fallbacks).
- OpenAIProvider: the OpenAI SDK; also works with any OpenAI-compatible server
  (OpenRouter, Groq, Together, LM Studio, Ollama, vLLM, ...) via base_url.
"""

from __future__ import annotations

import json
from typing import Callable, Protocol

from .tools import TOOLS

# run_tool(name, args) -> (result_text, is_error)
RunTool = Callable[[str, dict], "tuple[str, bool]"]


class UI(Protocol):
    def text(self, chunk: str) -> None: ...
    def thinking(self, chunk: str) -> None: ...
    def end_block(self) -> None: ...
    def notice(self, msg: str) -> None: ...


class Provider(Protocol):
    label: str
    def turn(self, user_text: str, run_tool: RunTool, ui: UI) -> None: ...
    def clear(self) -> None: ...


MAX_STEPS = 60  # tool-call rounds per user message before the loop stops


# --------------------------------------------------------------------------- Claude

# Models that accept the server-side refusal fallback ("default" routes to a suitable model).
_FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-2", "claude-fable-5-1", "claude-sonnet-5-5"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class ClaudeProvider:
    def __init__(self, model: str, system: str, effort: str = "high", api_key: str | None = None):
        import os

        import anthropic

        if not (api_key or os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            raise ValueError("No Claude API key. Paste it in the API key box and press Apply")
        self._anthropic = anthropic
        self.client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
        self.model = model
        self.effort = effort
        self.system = system
        self.messages: list = []
        self.label = f"claude:{model}"
        # eager_input_streaming streams large tool inputs (file bodies) as they are generated;
        # inputs are validated in run_tool before anything executes.
        self.tools = [
            {"name": t["name"], "description": t["description"], "input_schema": t["schema"],
             "eager_input_streaming": True}
            for t in TOOLS
        ]

    def clear(self) -> None:
        self.messages = []

    def _request(self) -> dict:
        req = dict(
            model=self.model,
            max_tokens=64000,
            system=[{"type": "text", "text": self.system}],
            tools=self.tools,
            messages=self.messages,
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": self.effort},
            cache_control={"type": "ephemeral"},  # caches the growing conversation prefix
        )
        if self.model in _FALLBACK_MODELS:
            req["betas"] = [FALLBACK_BETA]
            req["fallbacks"] = "default"
        return req

    def _stream(self, ui: UI):
        """One streamed model call. Returns the final message, or None if the stream's JSON was unusable."""
        try:
            with self.client.beta.messages.stream(**self._request()) as stream:
                current = None
                for event in stream:
                    if event.type == "content_block_start":
                        current = event.content_block.type
                    elif event.type == "content_block_delta":
                        if event.delta.type == "text_delta":
                            ui.text(event.delta.text)
                        elif event.delta.type == "thinking_delta":
                            ui.thinking(event.delta.thinking)
                    elif event.type == "content_block_stop" and current in ("text", "thinking"):
                        ui.end_block()
                return stream.get_final_message()
        except ValueError as e:  # the SDK could not parse a streamed tool input
            ui.notice(f"Model sent malformed tool input ({e}); retrying.")
            return None

    def turn(self, user_text: str, run_tool: RunTool, ui: UI) -> None:
        self.messages.append({"role": "user", "content": user_text})
        bad_json_retries = 0
        for _ in range(MAX_STEPS):
            msg = self._stream(ui)
            if msg is None:
                bad_json_retries += 1
                if bad_json_retries > 2:
                    ui.notice("Stopped: repeated malformed tool input.")
                    return
                continue

            for block in msg.content:
                if block.type == "fallback":
                    ui.notice(f"{block.from_.model} declined; continued on {block.to.model}.")

            # Keep the full content (thinking, fallback and tool_use blocks) unchanged in history.
            self.messages.append({"role": "assistant", "content": msg.content})
            tool_uses = [b for b in msg.content if b.type == "tool_use"]

            if msg.stop_reason == "refusal":
                d = msg.stop_details
                ui.notice(f"The model declined this request{f' ({d.category})' if d and d.category else ''}.")
                self._answer_unrun(tool_uses, "Not run: the turn ended in a refusal.")
                return
            if msg.stop_reason == "pause_turn":
                continue
            if msg.stop_reason == "max_tokens":
                if not tool_uses:
                    ui.notice("Reply hit the output limit.")
                    return
                # A cut-off tool input can look valid; never run it.
                self._answer_unrun(tool_uses, "Not run: your output hit max_tokens mid tool call. "
                                              "Retry with smaller steps (e.g. write the file in parts).")
                continue
            if not tool_uses:
                return  # end_turn: the model answered

            results = []
            for tu in tool_uses:
                text, is_error = run_tool(tu.name, tu.input)
                results.append({"type": "tool_result", "tool_use_id": tu.id, "content": text, "is_error": is_error})
            self.messages.append({"role": "user", "content": results})  # all results in ONE message
        ui.notice(f"Stopped after {MAX_STEPS} tool rounds. Say 'continue' to keep going.")

    def _answer_unrun(self, tool_uses, reason: str) -> None:
        if tool_uses:
            self.messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": t.id, "content": reason, "is_error": True} for t in tool_uses
            ]})

    def close_interrupted(self) -> None:
        """After Ctrl-C: answer any pending tool calls so the history stays valid."""
        if self.messages and self.messages[-1]["role"] == "assistant":
            pending = [b for b in self.messages[-1]["content"] if getattr(b, "type", None) == "tool_use"]
            self._answer_unrun(pending, "Interrupted by the user before this ran.")
        elif self.messages and self.messages[-1]["role"] == "user" and isinstance(self.messages[-1]["content"], str):
            self.messages.pop()  # the model never answered this message


# --------------------------------------------------------------------------- OpenAI / compatible / local

class OpenAIProvider:
    def __init__(self, model: str, system: str, base_url: str | None = None, api_key: str | None = None,
                 label_prefix: str = "openai"):
        import openai

        kwargs = {}
        if base_url:
            kwargs["base_url"] = base_url
        if api_key:
            kwargs["api_key"] = api_key
        self.client = openai.OpenAI(**kwargs)
        self.model = model
        self.system = system
        self.messages: list[dict] = [{"role": "system", "content": system}]
        self.label = f"{label_prefix}:{model}"
        self.tools = [
            {"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["schema"]}}
            for t in TOOLS
        ]

    def clear(self) -> None:
        self.messages = [{"role": "system", "content": self.system}]

    def _stream(self, ui: UI) -> tuple[str, list[dict], str | None]:
        stream = self.client.chat.completions.create(
            model=self.model, messages=self.messages, tools=self.tools, stream=True,
        )
        text, calls, finish = [], {}, None
        for chunk in stream:
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
        if text:
            ui.end_block()
        ordered = [calls[i] for i in sorted(calls)]
        for n, c in enumerate(ordered):  # some local servers omit ids
            c["id"] = c["id"] or f"call_{len(self.messages)}_{n}"
        return "".join(text), ordered, finish

    def turn(self, user_text: str, run_tool: RunTool, ui: UI) -> None:
        self.messages.append({"role": "user", "content": user_text})
        for _ in range(MAX_STEPS):
            text, calls, finish = self._stream(ui)
            assistant: dict = {"role": "assistant", "content": text or None}
            if calls:
                assistant["tool_calls"] = [
                    {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}}
                    for c in calls
                ]
            self.messages.append(assistant)
            if not calls:
                if finish == "length":
                    ui.notice("Reply hit the output limit.")
                return
            for c in calls:
                if finish == "length":
                    out, err = "Not run: output hit the length limit mid tool call. Retry in smaller steps.", True
                else:
                    try:
                        args = json.loads(c["arguments"] or "{}")
                        out, err = run_tool(c["name"], args)
                    except json.JSONDecodeError:
                        out, err = json.dumps({"INVALID_JSON": c["arguments"][:2000]}), True
                self.messages.append({"role": "tool", "tool_call_id": c["id"], "content": out if not err else f"ERROR: {out}"})
        ui.notice(f"Stopped after {MAX_STEPS} tool rounds. Say 'continue' to keep going.")

    def close_interrupted(self) -> None:
        last = self.messages[-1]
        if last["role"] == "assistant" and last.get("tool_calls"):
            answered = {m.get("tool_call_id") for m in self.messages if m["role"] == "tool"}
            for tc in last["tool_calls"]:
                if tc["id"] not in answered:
                    self.messages.append({"role": "tool", "tool_call_id": tc["id"],
                                          "content": "ERROR: interrupted by the user before this ran."})
        elif last["role"] == "user":
            self.messages.pop()
