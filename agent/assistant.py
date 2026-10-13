"""One chat turn, end to end: history in, model + tools, cards out, saved."""

from __future__ import annotations

import json
import re
import os
from datetime import datetime
from typing import Any

from agent.actions import Actions, current_conversation
from agent.calendar import local_zone
from agent.cards import build_cards
from agent.factcheck import (
    known_addresses, known_numbers, no_long_dashes, verify_addresses, verify_dollars, verify_names,
)
from agent.loop import _run_tools, run_turn, run_turn_stream, system_prompt
from agent.tools import ToolRegistry
from llm.provider import LLMProvider
from store.db import Store

MAX_USER_TURNS = 10        # model context: last N exchanges of the conversation
FRESH_RESULT_TURNS = 2     # ...but full tool results only for the most recent ones
STALE_RESULT = "[Earlier result omitted. Calendar and mail may have changed: call the tool again if you need this.]"


def _staff_names(trace: list[Any]) -> set[str]:
    """People named in this turn's time-entry reports: the names a reply about them must use."""
    names: set[str] = set()
    for t in trace:
        if t.name == "check_time_entries" and not t.is_error:
            try:
                data = json.loads(t.output)
            except ValueError:
                continue
            names |= {p["name"] for p in data.get("people", [])}
            silent = re.search(r"no time at all \(new, or gone\?\): ([^.]+)\.", data.get("note", ""))
            if silent:
                names |= {n.strip() for n in silent.group(1).split(",")}
    return names


def trim_history(messages: list[dict[str, Any]], max_turns: int = MAX_USER_TURNS,
                 fresh_turns: int = FRESH_RESULT_TURNS) -> list[dict[str, Any]]:
    """Keep the last `max_turns` exchanges, cut only at a real user message
    (string content) so a tool_use is never separated from its tool_result.

    Tool results older than `fresh_turns` exchanges are replaced with a
    placeholder: the model keeps the thread of the conversation but has to
    re-check facts instead of trusting stale data (it once re-answered from
    an old result instead of searching again)."""
    starts = [i for i, m in enumerate(messages) if is_dave(m)]
    if len(starts) > max_turns:
        cut = starts[-max_turns]
        messages, starts = messages[cut:], [s - cut for s in starts[-max_turns:]]
    if len(starts) <= fresh_turns:
        return messages
    stale_before = starts[-fresh_turns]
    return [_without_results(m) if i < stale_before else m for i, m in enumerate(messages)]


def is_dave(message: dict[str, Any]) -> bool:
    """Dave's own message (text, perhaps with attached files), not tool results coming back."""
    if message["role"] != "user":
        return False
    content = message["content"]
    return isinstance(content, str) or not any(b.get("type") == "tool_result" for b in content)


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

    def _begin(self, text: str, conversation_id: str | None, topic: dict | None, attachments: list[str] | None = None):
        text = text.strip()
        files = [self.store.get_upload(a) for a in attachments or []]
        if any(f is None for f in files):
            raise ValueError("An attached file is no longer available. Attach it again.")
        if not text and not files:
            raise ValueError("Empty message")
        if not text:
            text = "What's in this?" if len(files) == 1 else "What's in these?"
        conversation = self.store.get_conversation(conversation_id) if conversation_id else None
        if conversation is None:
            # Cut at a word, not mid-word ("...meeting from Christ").
            title = text if len(text) <= 80 else text[:80].rsplit(" ", 1)[0].rstrip(",.;:") + "…"
            if files and text in ("What's in this?", "What's in these?"):
                title = ", ".join(f["name"] for f in files)[:80]
            if topic and topic.get("brief"):
                row = self.store.get_brief(topic["brief"])
                title = f"Brief: {row['subject']}" if row else title
            conversation_id = self.store.create_conversation(title=title, topic=topic)
            conversation = self.store.get_conversation(conversation_id)
        # Files first, then the question about them.
        content: Any = text if not files else [b for f in files for b in f["blocks"]] + [{"type": "text", "text": text}]
        history = trim_history(conversation["llm_messages"]) + [{"role": "user", "content": content}]
        extra = self.memory.prompt_section() if self.memory else ""
        extra = "\n\n".join(filter(None, [extra, self._topic_section(conversation.get("topic"))]))
        shown = [{"name": f["name"], "kind": f["kind"], "summary": f["summary"], "thumb": f["thumb"]} for f in files]
        return text, conversation, history, system_prompt(datetime.now(local_zone()), extra), shown

    def _finish(self, text: str, conversation: dict[str, Any], result: Any, files: list | None = None) -> dict[str, Any]:
        truth = _ground_truth(result.messages)
        reply, fixes = verify_addresses(result.text, known_addresses(*truth))
        reply, name_fixes = verify_names(reply, _staff_names(result.trace))
        # Amounts read off a picture Dave attached can't be checked against any text, so with an
        # image in the conversation the dollar check would only mislabel them "(unverified)".
        reply, money_fixes = (reply, []) if _has_image(result.messages) else verify_dollars(reply, known_numbers(*truth))
        fixes += name_fixes + money_fixes
        reply = no_long_dashes(reply)
        if fixes:
            print(f"factcheck corrected reply in {conversation['id']}: {fixes}")
        cards = build_cards(result.trace)
        display = conversation["display"] + [
            {"role": "user", "text": text, **({"files": files} if files else {})},
            {"role": "assistant", "text": reply, "cards": cards},
        ]
        self.store.save_conversation(conversation["id"], result.messages, display)
        return {"conversation_id": conversation["id"], "text": reply, "cards": cards}

    def chat(self, text: str, conversation_id: str | None = None, topic: dict | None = None,
             attachments: list[str] | None = None) -> dict[str, Any]:
        text, conversation, history, system, files = self._begin(text, conversation_id, topic, attachments)
        token = current_conversation.set(conversation["id"])
        try:
            result = run_turn(self.llm, self.registry, history, system)
        finally:
            current_conversation.reset(token)
        return self._finish(text, conversation, result, files)

    def chat_stream(self, text: str, conversation_id: str | None = None, topic: dict | None = None,
                    attachments: list[str] | None = None):
        """Events for the app while the turn runs; the last is {"type": "done", ...}.

        The streamed text is a preview: the final text in "done" is the
        fact-checked version and replaces it."""
        text, conversation, history, system, files = self._begin(text, conversation_id, topic, attachments)
        yield {"type": "start", "conversation_id": conversation["id"]}

        def run_tools(calls):
            # Set the conversation around the tools themselves: a streaming response
            # may resume the generator on a different thread between events.
            token = current_conversation.set(conversation["id"])
            try:
                return _run_tools(self.registry, calls)
            finally:
                current_conversation.reset(token)

        result = None
        for event in run_turn_stream(self.llm, self.registry, history, system, run_tools=run_tools):
            if event["type"] == "result":
                result = event["result"]
            else:
                yield event
        yield {"type": "done", **self._finish(text, conversation, result, files)}

    def _topic_section(self, topic: dict | None) -> str:
        """A conversation opened from a brief starts with that brief in view."""
        if not topic or not topic.get("brief"):
            return ""
        row = self.store.get_brief(topic["brief"])
        if not row or not row.get("brief"):
            return ""
        return ("This conversation is about one meeting. Its prepared brief (from the invite, emails and "
                "attachments; labels like [Email 2] refer to them) is below. Answer from it first; use tools "
                "for anything it doesn't cover.\n" + _brief_text(row["brief"]))


def _brief_text(brief: dict[str, Any]) -> str:
    return json.dumps({k: brief.get(k) for k in ("meeting", "headline", "who", "context", "background", "prep",
                                                 "gaps", "material")}, ensure_ascii=False)


def _has_image(messages: list[dict[str, Any]]) -> bool:
    return any(is_dave(m) and not isinstance(m["content"], str) and any(b.get("type") == "image" for b in m["content"])
               for m in messages)


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
            texts += [b["text"] for b in m["content"] if b.get("type") == "text"]   # attached documents
    return texts
