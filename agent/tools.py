"""Tool registry: the only way the model's requests turn into real calls."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from agent.dates import date_guide, today_local
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
        if isinstance(result, dict):
            guide = date_guide(result)
            if guide:
                # Weekdays and "tomorrow"/"in 9 days" for every date mentioned, worked out in code.
                result = {**result, "dates": {"today": today_local().strftime("%A, %b %d, %Y"), **guide}}
        if isinstance(result, dict) and call.name in LIST_CARD_TOOLS:
            # Said in the result itself, first: the system prompt and tool descriptions
            # alone didn't stop the model re-typing whole tables the card already shows.
            result = {"shown_to_dave": SHOWN_ON_CARD, **result}
        return (result if isinstance(result, str) else json.dumps(result, default=str)), False


# Tools whose results Dave sees in full on a list card under the reply.
LIST_CARD_TOOLS = {"find_renewals", "find_tasks", "onboarding_status", "check_time_entries", "find_opportunities",
                   "find_quotes", "hubspot_query"}
SHOWN_ON_CARD = ("Dave already sees every row below on a card. Don't restate rows or make a table, and don't "
                 "mention the card. Reply in 2-4 sentences: the totals and the one or two things that stand out. "
                 "Check each claim against the rows (e.g. don't call an expired quote live).")
