"""Wiring: builds the real assistant from the environment, once."""

from __future__ import annotations

import os
from dataclasses import dataclass

from agent.actions import Actions
from agent.assistant import Assistant
from agent.calendar import calendar_tools
from agent.events import create_event_kind, create_event_tool
from agent.junk import junk_kind, junk_tools
from agent.mail import mail_tools
from agent.requests import request_kinds
from agent.memory import Memory, memory_tools
from agent.people import people_tools
from agent.scheduling import scheduling_tools
from agent.tools import ToolRegistry
from connectors.graph import GraphClient
from llm.hatzai import HatzAIProvider
from app.worker import Worker
from store.db import Store


@dataclass
class Services:
    graph: GraphClient
    store: Store
    actions: Actions
    assistant: Assistant
    memory: Memory
    worker: Worker
    llm: HatzAIProvider


def build_services() -> Services:
    graph = GraphClient.from_env()
    store = Store()
    actions = Actions(store, [create_event_kind(graph), junk_kind(graph, store), *request_kinds(graph, store)])
    memory = Memory(store)
    llm = HatzAIProvider()
    # Quick, high-volume judgments (junk triage) use a faster, cheaper model.
    fast_llm = HatzAIProvider(model=os.environ.get("HATZAI_FAST_MODEL", "anthropic.claude-haiku-4-5"))
    registry = ToolRegistry(
        calendar_tools(graph, memory)
        + people_tools(graph, memory)
        + mail_tools(graph)
        + scheduling_tools(graph, memory)
        + memory_tools(memory)
        + junk_tools(graph, fast_llm, actions, store)
        + [create_event_tool(graph, actions)]
    )
    assistant = Assistant(llm, registry, store, actions, memory)
    worker = Worker(graph, llm, store, searcher=llm, fast_llm=fast_llm, actions=actions)
    return Services(graph, store, actions, assistant, memory, worker, llm)
