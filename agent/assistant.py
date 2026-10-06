"""One chat turn, end to end: history in, model + tools, cards out, saved."""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any

from agent.actions import Actions, current_conversation
from agent.calendar import local_zone
from agent.cards import build_cards
from agent.factcheck import known_addresses, verify_addresses
from agent.loop import run_turn, system_prompt
from agent.tools import ToolRegistry
from llm.provider import LLMProvider
from store.db import Store

MAX_USER_TURNS = 10        # model context: last N exchanges of the conversation
FRESH_RESULT_TURNS = 2     # ...but full tool results only for the most recent ones
STALE_RESULT = "[Earlier result omitted. Calendar and mail may have changed: call the tool again if you need this.]"


def trim_history(messages: list[dict[str, Any]], max_turns: int = MAX_USER_TURNS,
                 fresh_turns: int = FRESH_RESULT_TURNS) -> list[dict[str, Any]]:
    """Keep the last `max_turns` exchanges, cut only at a real user message
    (string content) so a tool_use is never separated from its tool_result.

    Tool results older than `fresh_turns` exchanges are replaced with a
    placeholder: the model keeps the thread of the conversation but has to
    re-check facts instead of trusting stale data (it once re-answered from
    an old result instead of searching again)."""
    starts = [i for i, m in enumerate(messages) if m["role"] == "user" and isinstance(m["content"], str)]
    if len(starts) > max_turns:
        cut = starts[-max_turns]
        messages, starts = messages[cut:], [s - cut for s in starts[-max_turns:]]
    if len(starts) <= fresh_turns:
        return messages
    stale_before = starts[-fresh_turns]
    return [_without_results(m) if i < stale_before else m for i, m in enumerate(messages)]


def _without_results(message: dict[str, Any]) -> dict[str, Any]:
    if message["role"] != "user" or isinstance(message["content"], str):
        return message
    return {**message, "content": [
        {**block, "content": STALE_RESULT} if block.get("type") == "tool_result" else block
        for block in message["content"]
    ]}


class Assistant:
    def __init__(self, llm: LLMProvider, registry: ToolRegistry, store: Store, actions: Actions,
                 memory: Any = None) -> None:
        self.memory = memory
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
            memory = self.memory.prompt_section() if self.memory else ""
            result = run_turn(self.llm, self.registry, history, system_prompt(datetime.now(local_zone()), memory))
        finally:
            current_conversation.reset(token)

        reply, fixes = verify_addresses(result.text, known_addresses(*_ground_truth(result.messages)))
        if fixes:
            print(f"factcheck corrected reply in {conversation_id}: {fixes}")

        cards = build_cards(result.trace)
        display = conversation["display"] + [
            {"role": "user", "text": text},
            {"role": "assistant", "text": reply, "cards": cards},
        ]
        self.store.save_conversation(conversation_id, result.messages, display)
        return {"conversation_id": conversation_id, "text": reply, "cards": cards}


def _ground_truth(messages: list[dict[str, Any]]) -> list[str]:
    """Text that came from Dave or from tools, never the model's own words
    (an earlier slip in its own reply must not vouch for itself)."""
    texts = [os.environ.get("GRAPH_MAILBOX", "")]
    for m in messages:
        if m["role"] != "user":
            continue
        if isinstance(m["content"], str):
            texts.append(m["content"])
        else:
            texts += [b["content"] if isinstance(b.get("content"), str) else json.dumps(b.get("content"))
                      for b in m["content"] if b.get("type") == "tool_result"]
    return texts
