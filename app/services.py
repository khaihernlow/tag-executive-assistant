"""Wiring: builds the real assistant from the environment, once."""

from __future__ import annotations

import os
from dataclasses import dataclass

from agent.actions import Actions
from agent.assistant import Assistant
from agent.calendar import calendar_tools
from agent.changes import change_kinds, change_tools
from agent.events import create_event_kind, create_event_tool
from agent.filing import file_kind, rule_kind
from agent.junk import junk_kind, junk_tools
from agent.mail import mail_tools
from agent.opportunities import Directory, opportunity_kinds, opportunity_tools
from agent.timesheets import timesheet_tools
from agent.renewals import renewal_tools
from agent.projects import Picklists, project_kinds, project_tools
from agent.quotes import quote_tools
from connectors.tagdata import TagData
from connectors.apify import ApifyClient
from agent.linkedin import LinkedIn, linkedin_tools
from connectors.autotask import AutotaskClient
from agent.requests import request_kinds
from agent.memory import Memory, memory_tools
from agent.people import people_tools
from agent.scheduling import scheduling_tools
from agent.tools import ToolRegistry
from connectors.graph import GraphClient
from connectors.teams import Teams
from agent.lunch import Lunch
from agent.mealeo_order import fill_checkout
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
    autotask: Directory | None = None


def build_services() -> Services:
    graph = GraphClient.from_env()
    store = Store()
    # Autotask is optional: without credentials the assistant just has no CRM tools.
    autotask = AutotaskClient.from_env() if os.environ.get("AUTOTASK_USERNAME") else None
    picks = Picklists(autotask) if autotask else None
    actions = Actions(store, [create_event_kind(graph), junk_kind(graph, store), *request_kinds(graph, store),
                              file_kind(graph, store), rule_kind(graph, store), *change_kinds(graph),
                              *(opportunity_kinds(autotask) + project_kinds(autotask, picks) if autotask else [])])
    directory = Directory(autotask) if autotask else None
    tag_data = TagData.from_env()  # Matt's nightly copy of TAG's systems; read-only
    apify = ApifyClient.from_env()
    linkedin = LinkedIn(apify, store) if apify else None
    memory = Memory(store)
    llm = HatzAIProvider()
    # The assistant's own account (assistant@), for things it does as itself: Teams messages.
    own = GraphClient.as_account(os.environ["ASSISTANT_ACCOUNT"]) if os.environ.get("ASSISTANT_ACCOUNT") else None
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
        + change_tools(graph, actions)
        + (opportunity_tools(directory, actions) if directory else [])
        + (timesheet_tools(autotask, store) if autotask else [])
        + (renewal_tools(tag_data, directory, actions) if tag_data and directory else [])
        + (project_tools(autotask, directory, actions, picks) if autotask else [])
        + (quote_tools(autotask, directory) if autotask else [])
        + (linkedin_tools(linkedin, graph, directory) if linkedin else [])
    )
    assistant = Assistant(llm, registry, store, actions, memory)
    lunch = Lunch(graph, Teams(own, os.environ["ASSISTANT_ACCOUNT"], store), llm, store, checkout=fill_checkout) if own else None
    worker = Worker(graph, llm, store, searcher=llm, fast_llm=fast_llm, actions=actions, autotask=directory,
                    linkedin=linkedin, lunch=lunch)
    return Services(graph, store, actions, assistant, memory, worker, llm, directory)
