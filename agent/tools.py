"""Tool registry: the only way the model's requests turn into real calls."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from llm.provider import ToolCall, ToolSpec


@dataclass(frozen=True)
class Tool:
    spec: ToolSpec
    handler: Callable[[dict[str, Any]], Any]


class ToolRegistry:
    def __init__(self, tools: list[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        if tool.spec.name in self._tools:
            raise ValueError(f"Duplicate tool name: {tool.spec.name}")
        self._tools[tool.spec.name] = tool

    def specs(self) -> list[ToolSpec]:
        return [tool.spec for tool in self._tools.values()]

    def run(self, call: ToolCall) -> tuple[str, bool]:
        """Returns (content for the model, is_error). Errors go back to the
        model as tool results so it can correct itself instead of crashing the turn."""
        tool = self._tools.get(call.name)
        if tool is None:
            return f"Unknown tool: {call.name}", True
        try:
            result = tool.handler(call.input)
        except Exception as e:  # noqa: BLE001 - every failure must reach the model
            return f"{type(e).__name__}: {e}", True
        return (result if isinstance(result, str) else json.dumps(result, default=str)), False
