from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agent.actions import ActionKind, Actions
from agent.assistant import Assistant, trim_history
from agent.tools import Tool, ToolRegistry
from llm.provider import LLMResponse, ToolSpec
from store.db import Store


class ScriptedLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.seen = []

    def complete(self, messages, system=None, tools=None, max_tokens=2048, temperature=None):
        self.seen.append(list(messages))
        return self.responses.pop(0)


def text(value):
    return LLMResponse(content=[{"type": "text", "text": value}], stop_reason="end_turn")


class FakeGraph:
    mailbox = "dave@tag.example"

    def calendar_view(self, start, end):
        soon = (datetime.now(timezone.utc) + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S")
        later = (datetime.now(timezone.utc) + timedelta(minutes=35)).strftime("%Y-%m-%dT%H:%M:%S")
        return [{"subject": "Sales meeting", "start": {"dateTime": soon, "timeZone": "UTC"},
                 "end": {"dateTime": later, "timeZone": "UTC"}, "showAs": "busy",
                 "onlineMeeting": {"joinUrl": "https://teams/x"}}]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("REQUIRE_AUTH", "False")
    monkeypatch.setenv("ASSISTANT_TIMEZONE", "America/New_York")
    from app.main import app

    store = Store(":memory:")
    ran = []
    actions = Actions(store, [ActionKind("demo", lambda payload, aid: ran.append(aid) or {"done": True})], auto=set())

    def propose(args):
        action = actions.propose("demo", "Book something", {})
        return {"action_id": action["id"], "kind": "demo", "status": action["status"], "summary": action["summary"]}

    registry = ToolRegistry([Tool(ToolSpec("propose", "p", {"type": "object"}), handler=propose)])
    llm = ScriptedLLM([
        LLMResponse(content=[{"type": "tool_use", "id": "t1", "name": "propose", "input": {}}], stop_reason="tool_use"),
        text("Ready for your approval."),
        text("Second reply."),
    ])
    app.state.services = SimpleNamespace(graph=FakeGraph(), store=store, actions=actions,
                                         assistant=Assistant(llm, registry, store, actions))
    yield TestClient(app), ran, llm
    app.state.services = None


def test_chat_returns_cards_and_persists_conversation(client):
    http, ran, llm = client
    first = http.post("/api/chat", json={"message": "Book it"}).json()
    assert first["text"] == "Ready for your approval."
    assert first["cards"] == []  # the demo tool isn't a card type; real create_event is covered elsewhere

    second = http.post("/api/chat", json={"message": "Thanks", "conversation_id": first["conversation_id"]}).json()
    assert second["conversation_id"] == first["conversation_id"]
    assert llm.seen[-1][0] == {"role": "user", "content": "Book it"}  # history carried over

    convo = http.get(f"/api/conversations/{first['conversation_id']}").json()
    assert [m["role"] for m in convo["messages"]] == ["user", "assistant", "user", "assistant"]


def test_today_shows_agenda_and_pending_signoffs_then_approval_runs_once(client):
    http, ran, _ = client
    http.post("/api/chat", json={"message": "Book it"})

    day = http.get("/api/today").json()
    assert day["events"][0]["subject"] == "Sales meeting"
    assert day["events"][0]["join_url"] == "https://teams/x"
    [pending] = day["pending"]

    approved = http.post(f"/api/actions/{pending['action_id']}/approve").json()
    again = http.post(f"/api/actions/{pending['action_id']}/approve").json()
    assert approved["status"] == again["status"] == "executed"
    assert len(ran) == 1
    assert http.get("/api/today").json()["pending"] == []
    assert http.get("/api/activity").json()["actions"][0]["decided_by"] == "local@localhost"


def test_empty_message_and_unknown_action(client):
    http, _, _ = client
    assert http.post("/api/chat", json={"message": "  "}).status_code == 400
    assert http.post("/api/actions/nope/approve").status_code == 404


def test_trim_history_cuts_only_at_real_user_turns():
    msgs = []
    for i in range(4):
        msgs += [{"role": "user", "content": f"q{i}"},
                 {"role": "assistant", "content": [{"type": "tool_use", "id": f"t{i}", "name": "x", "input": {}}]},
                 {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": "r"}]},
                 {"role": "assistant", "content": [{"type": "text", "text": "a"}]}]
    trimmed = trim_history(msgs, max_turns=2)
    assert trimmed[0] == {"role": "user", "content": "q2"}
    assert len(trimmed) == 8


def test_old_tool_results_are_dropped_but_pairs_stay_intact():
    from agent.assistant import STALE_RESULT

    msgs = []
    for i in range(3):
        msgs += [{"role": "user", "content": f"q{i}"},
                 {"role": "assistant", "content": [{"type": "tool_use", "id": f"t{i}", "name": "x", "input": {}}]},
                 {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": f"data{i}"}]},
                 {"role": "assistant", "content": [{"type": "text", "text": f"a{i}"}]}]
    trimmed = trim_history(msgs, max_turns=10, fresh_turns=2)
    results = [m["content"][0]["content"] for m in trimmed
               if isinstance(m["content"], list) and m["content"][0]["type"] == "tool_result"]
    assert results == [STALE_RESULT, "data1", "data2"]
    assert [m["content"][0]["tool_use_id"] for m in trimmed
            if isinstance(m["content"], list) and m["content"][0]["type"] == "tool_result"] == ["t0", "t1", "t2"]
    assert msgs[2]["content"][0]["content"] == "data0"  # caller's history untouched
