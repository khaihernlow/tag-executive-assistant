"""HatzAI adapter over the Anthropic-compatible Messages gateway.

HatzAI's native `/v1/chat/completions` only runs Hatz's own server-side tools
(`tools_to_use`) and silently drops client-defined `tools`. Client-managed
tool calling lives on `/v1/anthropic/messages` (used here) and
`/v1/openai/responses`. See https://api-docs.hatz.ai/anthropic-messages
"""

from __future__ import annotations

import os
import time
from typing import Any

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
