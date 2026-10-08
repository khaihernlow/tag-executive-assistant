"""HatzAI adapter over the Anthropic-compatible Messages gateway.

HatzAI's native `/v1/chat/completions` only runs Hatz's own server-side tools
(`tools_to_use`) and silently drops client-defined `tools`. Client-managed
tool calling lives on `/v1/anthropic/messages` (used here) and
`/v1/openai/responses`. See https://api-docs.hatz.ai/anthropic-messages
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Iterator

import requests

from llm.provider import LLMResponse, ToolSpec

BASE_URL = "https://ai.hatz.ai/v1"
MESSAGES_URL = f"{BASE_URL}/anthropic/messages"

_RETRY_STATUS_CODES = {429, 500, 502, 503, 504}


class HatzAIError(Exception):
    pass


class HatzAIProvider:
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        session: requests.Session | None = None,
        max_retries: int = 3,
        timeout: float = 120.0,
    ) -> None:
        self.api_key = api_key or os.environ["HATZAI_API_KEY"]
        self.model = model or os.environ.get("HATZAI_MODEL", "anthropic.claude-sonnet-4-6")
        self.max_retries = max_retries
        self.timeout = timeout
        self._session = session or requests.Session()
        self._session.headers.update({
            "X-API-Key": self.api_key,
            "Content-Type": "application/json",
        })

    def list_models(self) -> list[dict]:
        resp = self._session.get(f"{BASE_URL}/chat/models", timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data

    def build_payload(
        self,
        messages: list[dict[str, Any]],
        system: str | None = None,
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 2048,
        temperature: float | None = None,
        tool_choice: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = [tool.to_anthropic() for tool in tools]
        if temperature is not None:
            payload["temperature"] = temperature
        if tool_choice:
            # Force a specific tool: the way to get schema-shaped output.
            payload["tool_choice"] = {"type": "tool", "name": tool_choice}
        return payload

    def complete(
        self,
        messages: list[dict[str, Any]],
        system: str | None = None,
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 2048,
        temperature: float | None = None,
        tool_choice: str | None = None,
    ) -> LLMResponse:
        return parse_response(self.post_raw(
            self.build_payload(messages, system, tools, max_tokens, temperature, tool_choice)))

    def stream(
        self,
        messages: list[dict[str, Any]],
        system: str | None = None,
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 2048,
    ) -> Iterator[tuple[str, Any]]:
        """Streamed completion: yields ("text", delta) as the model writes, then
        ("response", LLMResponse) with the assembled message (text and tool calls)."""
        payload = self.build_payload(messages, system, tools, max_tokens)
        payload["stream"] = True
        blocks: list[dict[str, Any]] = []
        partial_json: dict[int, str] = {}
        stop_reason = None
        with self._session.post(MESSAGES_URL, json=payload, stream=True, timeout=self.timeout) as resp:
            if not resp.ok:
                raise HatzAIError(f"HatzAI API error {resp.status_code}: {resp.text[:300]}")
            for line in resp.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data:"):
                    continue
                try:
                    event = json.loads(line[5:].strip())
                except ValueError:
                    continue
                kind = event.get("type")
                if kind == "content_block_start":
                    block = dict(event.get("content_block") or {})
                    if block.get("type") == "tool_use":
                        block["input"] = {}
                    blocks.append(block)
                elif kind == "content_block_delta" and blocks:
                    delta = event.get("delta") or {}
                    index = event.get("index", len(blocks) - 1)
                    if delta.get("type") == "text_delta":
                        blocks[index]["text"] = blocks[index].get("text", "") + delta.get("text", "")
                        yield "text", delta.get("text", "")
                    elif delta.get("type") == "input_json_delta":
                        partial_json[index] = partial_json.get(index, "") + delta.get("partial_json", "")
                elif kind == "content_block_stop":
                    index = event.get("index", len(blocks) - 1)
                    if index in partial_json:
                        try:
                            blocks[index]["input"] = json.loads(partial_json.pop(index) or "{}")
                        except ValueError as e:
                            raise HatzAIError("Streamed tool input was not valid JSON") from e
                elif kind == "message_delta":
                    stop_reason = (event.get("delta") or {}).get("stop_reason", stop_reason)
                elif kind == "error":
                    raise HatzAIError(f"HatzAI stream error: {event.get('error')}")
        yield "response", parse_response({"content": blocks, "stop_reason": stop_reason})

    def web_answer(self, prompt: str, tool: str = "firecrawl_search", max_tokens: int = 1500) -> str:
        """Ask with one of HatzAI's server-side tools (e.g. web search) enabled.

        These tools only run on the Hatz-native /chat/completions endpoint; HatzAI
        executes them itself and returns the final text."""
        payload = {"model": self.model, "messages": [{"role": "user", "content": prompt}],
                   "tools_to_use": [tool], "max_tokens": max_tokens}
        resp = self._session.post(f"{BASE_URL}/chat/completions", json=payload, timeout=self.timeout * 2)
        if not resp.ok:
            raise HatzAIError(f"HatzAI web search error {resp.status_code}: {resp.text[:300]}")
        try:
            return resp.json()["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError, ValueError) as e:
            raise HatzAIError(f"Unexpected web search response: {resp.text[:300]}") from e

    def post_raw(self, payload: dict[str, Any]) -> dict[str, Any]:
        last_error: Exception = HatzAIError("Unknown error")
        for attempt in range(self.max_retries):
            try:
                resp = self._session.post(MESSAGES_URL, json=payload, timeout=self.timeout)
                if resp.ok:
                    return resp.json()
                if resp.status_code in _RETRY_STATUS_CODES:
                    last_error = HatzAIError(
                        f"HatzAI API error {resp.status_code} (attempt {attempt + 1}/{self.max_retries}): {resp.text[:300]}"
                    )
                    time.sleep(2 ** attempt)
                    continue
                raise HatzAIError(f"HatzAI API error {resp.status_code}: {resp.text}")
            except requests.exceptions.RequestException as e:
                last_error = e
                time.sleep(2 ** attempt)
        raise HatzAIError(f"Failed after {self.max_retries} attempts: {last_error}") from last_error


def parse_response(data: dict[str, Any]) -> LLMResponse:
    content = data.get("content") if isinstance(data, dict) else None
    if not isinstance(content, list):
        raise HatzAIError(f"Unexpected response shape: {data}")
    for block in content:
        if block.get("type") == "tool_use" and not isinstance(block.get("input", {}), dict):
            raise HatzAIError(f"Tool call input is not an object: {block!r}")
    return LLMResponse(content=content, stop_reason=data.get("stop_reason"), raw=data)
