import json

import pytest

from agent.calendar import calendar_tools
from agent.cards import build_cards
from agent.loop import ToolTrace
from agent.memory import Memory, memory_tools
from agent.people import people_tools
from agent.tools import ToolRegistry
from llm.provider import ToolCall
from store.db import Store


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("GRAPH_MAILBOX", "dave@tag.example")
    monkeypatch.setenv("ASSISTANT_TIMEZONE", "America/New_York")


@pytest.fixture
def memory():
    return Memory(Store(":memory:"))


def run(registry, name, args):
    content, is_error = registry.run(ToolCall("t", name, args))
    return (content if is_error else json.loads(content)), is_error


def test_alias_makes_find_person_confident_without_searching(memory):
    class NoGraph:
        def get_all(self, *args, **kwargs):
            raise AssertionError("should not search when an alias is known")

    registry = ToolRegistry(memory_tools(memory) + people_tools(NoGraph(), memory))
    saved, _ = run(registry, "remember", {"kind": "alias", "name": "Kai", "email": "KLow@tag.example",
                                          "full_name": "Khaihern Low"})
    assert saved == {"remembered": '"Kai" means Khaihern Low <klow@tag.example>'}

    found, _ = run(registry, "find_person", {"name": "  kai "})
    assert found["note"] == "Confident match."
    assert found["matches"][0]["email"] == "klow@tag.example"
    assert found["matches"][0]["internal"] is True


def test_preferences_become_free_time_defaults(memory):
    class Calendar:
        def calendar_view(self, start, end):
            return []

    registry = ToolRegistry(memory_tools(memory) + calendar_tools(Calendar(), memory))
    run(registry, "remember", {"kind": "pref", "key": "day_start", "value": "09:30"})
    run(registry, "remember", {"kind": "pref", "key": "meeting_minutes", "value": "45"})

    slots, _ = run(registry, "find_free_time", {"start_date": "2026-10-13", "end_date": "2026-10-13"})
    assert slots["free_windows"] == [{"from": "Tue Oct 13 9:30 AM", "to": "Tue Oct 13 5:00 PM"}]

    # An explicit value from the model still wins over the preference.
    slots, _ = run(registry, "find_free_time", {"start_date": "2026-10-13", "end_date": "2026-10-13",
                                                "earliest": "08:00"})
    assert slots["free_windows"][0]["from"] == "Tue Oct 13 8:00 AM"


def test_bad_input_is_rejected(memory):
    registry = ToolRegistry(memory_tools(memory))
    assert run(registry, "remember", {"kind": "alias", "name": "Kai", "email": "Khaihern"})[1]
    assert run(registry, "remember", {"kind": "pref", "key": "day_start", "value": "early"})[1]
    assert run(registry, "remember", {"kind": "pref", "key": "favourite_colour", "value": "blue"})[1]


def test_notes_reach_the_prompt_and_forget_removes_them(memory):
    registry = ToolRegistry(memory_tools(memory))
    run(registry, "remember", {"kind": "note", "text": "Keep Friday afternoons free for family"})
    run(registry, "remember", {"kind": "alias", "name": "Kai", "email": "klow@tag.example", "full_name": "Khaihern Low"})
    section = memory.prompt_section()
    assert "Keep Friday afternoons free for family" in section
    assert '"Kai" means Khaihern Low <klow@tag.example>' in section

    forgot, _ = run(registry, "forget", {"what": "friday afternoons"})
    assert forgot["forgot"] == ["Keep Friday afternoons free for family"]
    assert "Friday" not in memory.prompt_section()
    assert run(registry, "forget", {"what": "nothing like this"})[0]["note"] == "Nothing matched."


def test_memory_card_confirms_what_was_saved():
    cards = build_cards([ToolTrace("remember", {}, json.dumps({"remembered": "Default meeting length in minutes: 45"}), False)])
    assert cards == [{"type": "memory", "text": "Remembered: Default meeting length in minutes: 45"}]
