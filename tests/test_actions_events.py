import json
from datetime import datetime

import pytest

from agent.actions import ActionKind, Actions, current_conversation
from agent.cards import build_cards
from agent.events import create_event_kind, create_event_tool, event_body, validate_event
from agent.loop import ToolTrace
from agent.tools import ToolRegistry
from llm.provider import ToolCall
from store.db import Store


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("ASSISTANT_TIMEZONE", "America/New_York")
    monkeypatch.setenv("GRAPH_MAILBOX", "dvener@tagsolutions.com")


@pytest.fixture
def store():
    return Store(":memory:")


# ── approval policy ──────────────────────────────────────────────────────────

def recorder():
    calls = []

    def execute(payload, action_id):
        calls.append((payload, action_id))
        return {"ok": True}

    return calls, ActionKind("demo", execute)


def test_propose_waits_for_approval_and_executes_once(store):
    calls, kind = recorder()
    actions = Actions(store, [kind], auto=set())
    token = current_conversation.set("c1")
    try:
        proposed = actions.propose("demo", "Do the thing", {"x": 1})
    finally:
        current_conversation.reset(token)

    assert proposed["status"] == "pending" and proposed["conversation_id"] == "c1"
    assert calls == []

    first = actions.approve(proposed["id"], decided_by="dave")
    second = actions.approve(proposed["id"], decided_by="dave")  # double tap
    assert first["status"] == second["status"] == "executed"
    assert first["result"] == {"ok": True} and first["decided_by"] == "dave"
    assert len(calls) == 1


def test_rejected_actions_never_run(store):
    calls, kind = recorder()
    actions = Actions(store, [kind], auto=set())
    aid = actions.propose("demo", "x", {})["id"]
    assert actions.reject(aid, "dave")["status"] == "rejected"
    assert actions.approve(aid, "dave")["status"] == "rejected"
    assert calls == []


def test_auto_kinds_execute_immediately_and_failures_are_recorded(store):
    def boom(payload, action_id):
        raise RuntimeError("graph down")

    actions = Actions(store, [ActionKind("demo", boom)], auto={"demo"})
    action = actions.propose("demo", "x", {})
    assert action["status"] == "failed"
    assert action["decided_by"] == "auto"
    assert "graph down" in action["error"]


# ── create_event ─────────────────────────────────────────────────────────────

class FakeGraph:
    mailbox = "dvener@tagsolutions.com"

    def __init__(self, busy_items=None):
        self.busy_items = busy_items or []
        self.posts = []

    def post(self, path, body, headers=None):
        self.posts.append((path, body))
        if path.endswith("getSchedule"):
            return {"value": [{"scheduleId": s, "scheduleItems": self.busy_items if i == 0 else []}
                              for i, s in enumerate(body["schedules"])]}
        return {"id": "evt1", "webLink": "https://outlook/evt1", "onlineMeeting": {"joinUrl": "https://teams/j"}}


NOW = datetime.fromisoformat("2026-10-06T09:00:00-04:00")


def test_validate_event_normalizes_and_rejects_bad_input():
    payload = validate_event({"subject": "Renewals", "start": "2026-10-08T11:00", "duration_minutes": 30,
                              "attendees": [{"email": "GSmith@tagsolutions.com", "name": "Garrett Smith"}]}, now=NOW)
    assert payload["start"] == "2026-10-08T11:00:00-04:00"
    assert payload["end"] == "2026-10-08T11:30:00-04:00"
    assert payload["attendees"] == [{"email": "gsmith@tagsolutions.com", "name": "Garrett Smith"}]
    assert payload["teams"] is True

    with pytest.raises(ValueError, match="past"):
        validate_event({"subject": "x", "start": "2026-10-01T11:00"}, now=NOW)
    with pytest.raises(ValueError, match="find_person"):
        validate_event({"subject": "x", "start": "2026-10-08T11:00", "attendees": ["Garrett"]}, now=NOW)
    with pytest.raises(ValueError, match="duration"):
        validate_event({"subject": "x", "start": "2026-10-08T11:00", "duration_minutes": 0}, now=NOW)


def test_event_body_is_utc_with_teams_and_idempotency_key():
    payload = validate_event({"subject": "Renewals", "start": "2026-10-08T11:00",
                              "attendees": [{"email": "gsmith@tagsolutions.com"}]}, now=NOW)
    body = event_body(payload, "action-123")
    assert body["start"] == {"dateTime": "2026-10-08T15:00:00", "timeZone": "UTC"}
    assert body["isOnlineMeeting"] is True and body["onlineMeetingProvider"] == "teamsForBusiness"
    assert body["transactionId"] == "action-123"
    assert body["attendees"][0]["emailAddress"]["address"] == "gsmith@tagsolutions.com"


def test_create_event_tool_proposes_with_conflicts_and_books_only_on_approval(store, monkeypatch):
    graph = FakeGraph()  # nobody busy
    actions = Actions(store, [create_event_kind(graph)], auto=set())
    registry = ToolRegistry([create_event_tool(graph, actions)])

    content, is_error = registry.run(ToolCall("t", "create_event", {
        "subject": "Renewals review", "start": "2099-10-08T11:00",
        "attendees": [{"email": "gsmith@tagsolutions.com", "name": "Garrett Smith"}]}))
    result = json.loads(content)
    assert not is_error
    assert result["status"] == "pending" and "Never say it's booked" in result["note"]
    assert "Garrett Smith" in result["summary"]
    assert not [p for p in graph.posts if p[0].endswith("/events")]

    executed = actions.approve(result["action_id"], "dave")
    assert executed["status"] == "executed"
    assert executed["result"]["join_url"] == "https://teams/j"
    assert graph.posts[-1][0] == "/users/dvener@tagsolutions.com/events"


def test_conflicts_appear_in_summary(store, monkeypatch):
    import agent.events as events

    monkeypatch.setattr(events, "validate_event", lambda args: validate_event(args, now=NOW))
    graph = FakeGraph([{"status": "busy", "start": {"dateTime": "2026-10-08T15:15:00", "timeZone": "UTC"},
                        "end": {"dateTime": "2026-10-08T15:45:00", "timeZone": "UTC"}}])
    actions = Actions(store, [create_event_kind(graph)], auto=set())
    content, _ = ToolRegistry([create_event_tool(graph, actions)]).run(
        ToolCall("t", "create_event", {"subject": "x", "start": "2026-10-08T11:00"}))
    assert "⚠ conflicts: Thu Oct 8 11:15 AM" in json.loads(content)["summary"]


# ── cards ────────────────────────────────────────────────────────────────────

def trace(name, inp, out, is_error=False):
    return ToolTrace(name, inp, json.dumps(out), is_error)


def test_cards_come_from_tool_output_not_model_text():
    cards = build_cards([
        trace("find_person", {"name": "Garrett"}, {"matches": [], "note": "Confident match."}),
        trace("search_mail", {}, {"messages": [{"id": "m1", "subject": "Renewals"}]}),
        trace("read_email", {"message_id": "m1"}, {"subject": "Renewals", "from": {"name": "Garrett"},
                                                    "received": "Mon", "attachments": []}),
        trace("find_mutual_time", {"duration_minutes": 45, "attendees": ["g@x.com"]},
              {"free_windows": [{"from": "Thu Oct 8 11:00 AM", "to": "Thu Oct 8 12:00 PM"}]}),
        trace("find_mutual_time", {}, {"free_windows": []}),  # refined search replaces the first
        trace("create_event", {}, {"action_id": "a1", "kind": "create_event", "status": "pending", "summary": "s"}),
        trace("list_calendar_events", {}, {}, is_error=True),
    ])
    assert [c["type"] for c in cards] == ["email", "slots", "action"]
    assert cards[1]["slots"] == []  # the last search wins
