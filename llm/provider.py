"""Provider-neutral types for the agent loop.

Messages use the Anthropic Messages format (content blocks, `tool_use` from
the assistant, `tool_result` blocks in the next user turn), because HatzAI's
`/v1/anthropic/messages` gateway speaks it with client-managed tools. Any
other provider adapter translates to and from this shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]  # JSON Schema object

    def to_anthropic(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class LLMResponse:
    content: list[dict[str, Any]]
    stop_reason: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return "".join(block.get("text", "") for block in self.content if block.get("type") == "text")

    @property
    def tool_calls(self) -> list[ToolCall]:
        return [
            ToolCall(id=block["id"], name=block["name"], input=block.get("input") or {})
            for block in self.content
            if block.get("type") == "tool_use"
        ]

    def assistant_message(self) -> dict[str, Any]:
        """The message to append to history so the next turn sees this one."""
        return {"role": "assistant", "content": self.content}


def tool_result_block(call: ToolCall, content: str, is_error: bool = False) -> dict[str, Any]:
    block: dict[str, Any] = {"type": "tool_result", "tool_use_id": call.id, "content": content}
    if is_error:
        block["is_error"] = True
    return block


def tool_results_message(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    """All results for one assistant turn go back together in a single user message."""
    return {"role": "user", "content": blocks}


class LLMProvider(Protocol):
    def complete(
        self,
        messages: list[dict[str, Any]],
        system: str | None = None,
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 2048,
        temperature: float | None = None,
        tool_choice: str | None = None,
    ) -> LLMResponse: ...
