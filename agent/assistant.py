"""One chat turn, end to end: history in, model + tools, cards out, saved."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from agent.actions import Actions, current_conversation
from agent.calendar import local_zone
from agent.cards import build_cards
from agent.loop import run_turn, system_prompt
from agent.tools import ToolRegistry
from llm.provider import LLMProvider
from store.db import Store

MAX_USER_TURNS = 12  # model context: last N exchanges


def trim_history(messages: list[dict[str, Any]], max_turns: int = MAX_USER_TURNS) -> list[dict[str, Any]]:
    """Cut at a real user message (string content), never between a
    tool_use and its tool_result, which the API would reject."""
    starts = [i for i, m in enumerate(messages) if m["role"] == "user" and isinstance(m["content"], str)]
    if len(starts) <= max_turns:
        return messages
    return messages[starts[-max_turns]:]


class Assistant:
    def __init__(self, llm: LLMProvider, registry: ToolRegistry, store: Store, actions: Actions) -> None:
        self.llm = llm
        self.registry = registry
        self.store = store
        self.actions = actions

    def chat(self, text: str, conversation_id: str | None = None) -> dict[str, Any]:
        text = text.strip()
        if not text:
            raise ValueError("Empty message")
        conversation = self.store.get_conversation(conversation_id) if conversation_id else None
        if conversation is None:
            conversation_id = self.store.create_conversation(title=text[:80])
            conversation = self.store.get_conversation(conversation_id)

        history = trim_history(conversation["llm_messages"]) + [{"role": "user", "content": text}]
        token = current_conversation.set(conversation_id)
        try:
            result = run_turn(self.llm, self.registry, history, system_prompt(datetime.now(local_zone())))
        finally:
            current_conversation.reset(token)

        cards = build_cards(result.trace)
        display = conversation["display"] + [
            {"role": "user", "text": text},
            {"role": "assistant", "text": result.text, "cards": cards},
        ]
        self.store.save_conversation(conversation_id, result.messages, display)
        return {"conversation_id": conversation_id, "text": result.text, "cards": cards}
