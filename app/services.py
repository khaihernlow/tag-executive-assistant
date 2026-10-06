"""Wiring: builds the real assistant from the environment, once."""

from __future__ import annotations

from dataclasses import dataclass

from agent.actions import Actions
from agent.assistant import Assistant
from agent.calendar import calendar_tools
from agent.events import create_event_kind, create_event_tool
from agent.mail import mail_tools
from agent.people import people_tools
from agent.scheduling import scheduling_tools
from agent.tools import ToolRegistry
from connectors.graph import GraphClient
from llm.hatzai import HatzAIProvider
from store.db import Store


@dataclass
class Services:
    graph: GraphClient
    store: Store
    actions: Actions
    assistant: Assistant


def build_services() -> Services:
    graph = GraphClient.from_env()
    store = Store()
    actions = Actions(store, [create_event_kind(graph)])
    registry = ToolRegistry(
        calendar_tools(graph)
        + people_tools(graph)
        + mail_tools(graph)
        + scheduling_tools(graph)
        + [create_event_tool(graph, actions)]
    )
    return Services(graph, store, actions, Assistant(HatzAIProvider(), registry, store, actions))
