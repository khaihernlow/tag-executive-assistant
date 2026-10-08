from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from agent.actions import Actions
from agent.changes import (
    answer_invite, change_kinds, change_tools, pending_invites, propose_cancel, propose_move, propose_response,
)
from agent.cards import build_cards
from agent.loop import ToolTrace
from store.db import Store

NY = ZoneInfo("America/New_York")
DAVE = "dave@tag.example"
NOW = datetime(2026, 10, 8, 9, 0, tzinfo=NY)


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("GRAPH_MAILBOX", DAVE)
    monkeypatch.setenv("ASSISTANT_TIMEZONE", "America/New_York")


def event(eid, subject, start, end, organizer=DAVE, response="organizer", attendees=(), show_as="busy", online=False):
    """start/end are UTC 'YYYY-MM-DDTHH:MM' (Eastern + 4h in October)."""
    return {"id": eid, "subject": subject, "showAs": show_as, "isOnlineMeeting": online, "webLink": f"https://outlook/{eid}",
            "start": {"dateTime": start + ":00", "timeZone": "UTC"}, "end": {"dateTime": end + ":00", "timeZone": "UTC"},
            "organizer": {"emailAddress": {"name": organizer.split("@")[0].title(), "address": organizer}},
            "responseStatus": {"response": response},
            "attendees": [{"emailAddress": {"name": a.split("@")[0].title(), "address": a}} for a in attendees]}


SYNC = event("sync", "Renewals sync", "2026-10-12T18:00", "2026-10-12T18:30", attendees=(DAVE, "jordan@tag.example"))
SOLO = event("solo", "Focus block", "2026-10-13T13:00", "2026-10-13T14:00")
VENDOR = event("vendor", "Vendor QBR", "2026-10-14T15:00", "2026-10-14T16:00", organizer="am@vendor.example",
               response="none", attendees=(DAVE, "am@vendor.example"), online=True)
CLASH = event("clash", "Board prep", "2026-10-14T15:30", "2026-10-14T16:30", attendees=(DAVE,))
LUNCH = event("lunch", "Lunch", "2026-10-13T16:00", "2026-10-13T17:00")


class FakeGraph:
    mailbox = DAVE

    def __init__(self, events=(SYNC, SOLO, VENDOR, CLASH, LUNCH), busy=None):
        self.events = {e["id"]: e for e in events}
        self.busy = busy or {}
        self.calls = []

    def get(self, path, params=None, headers=None):
        eid = path.rsplit("/", 1)[-1]
        if eid not in self.events:
            raise RuntimeError("404")
        return self.events[eid]

    def calendar_view(self, start, end):
        return list(self.events.values())

    def post(self, path, body, headers=None):
        self.calls.append(("POST", path, body))
        if path.endswith("getSchedule"):
            return {"value": [{"scheduleId": s, "scheduleItems": self.busy.get(s, [])} for s in body["schedules"]]}
        return {}

    def patch(self, path, body):
        self.calls.append(("PATCH", path, body))
        return {}

    def delete(self, path):
        self.calls.append(("DELETE", path, None))


def setup(graph=None):
    graph = graph or FakeGraph()
    store = Store(":memory:")
    return graph, Actions(store, change_kinds(graph), auto=set())


def test_unanswered_invites_show_with_what_they_clash_with():
    [invite] = pending_invites(FakeGraph(), NOW)
    assert invite["id"] == "vendor" and invite["organizer"] == "Am" and invite["where"] == "Teams"
    assert invite["when"] == "Wed, Oct 14 · 11:00 AM to 12:00 PM"
    assert invite["clashes"] == ["Board prep, 11:30 AM to 12:30 PM"]


def test_tapping_accept_on_today_sends_it_straight_away():
    graph, actions = setup()
    result = answer_invite(graph, actions, "vendor", "accept")
    assert result["status"] == "executed"
    assert ("POST", f"/users/{DAVE}/events/vendor/accept", {"comment": "", "sendResponse": True}) in graph.calls


def test_chat_proposes_invite_replies_and_refuses_ones_dave_organizes():
    graph, actions = setup()
    slip = propose_response(graph, actions, "vendor", "decline", "Traveling that week")
    assert slip["status"] == "pending" and slip["summary"].startswith("Decline “Vendor QBR”")
    assert slip["email"]["comment"] == "Traveling that week" and slip["email"]["to"] == "Am"
    assert not [c for c in graph.calls if c[0] == "POST" and "/decline" in c[1]]  # nothing sent before approval
    with pytest.raises(ValueError, match="organizes"):
        propose_response(graph, actions, "sync", "accept")


def test_moving_checks_the_new_time_and_patches_once_approved():
    busy = {"jordan@tag.example": [{"status": "busy", "start": {"dateTime": "2026-10-13T16:00:00", "timeZone": "UTC"},
                                    "end": {"dateTime": "2026-10-13T16:30:00", "timeZone": "UTC"}}]}
    graph, actions = setup(FakeGraph(busy=busy))
    slip = propose_move(graph, actions, "sync", "2026-10-13T12:00", now=NOW)
    assert slip["summary"].startswith("Move “Renewals sync” from Mon Oct 12 2:00 PM to Tue Oct 13 12:00 PM–12:30 PM")
    assert "1 attendee gets the update" in slip["summary"]
    assert "⚠ you have Lunch at 12:00 PM" in slip["summary"] and "⚠ Jordan is busy" in slip["summary"]
    actions.approve(slip["action_id"], decided_by="test")
    [(verb, path, body)] = [c for c in graph.calls if c[0] == "PATCH"]
    assert path == f"/users/{DAVE}/events/sync"
    assert body["start"] == {"dateTime": "2026-10-13T16:00:00", "timeZone": "UTC"}  # 12:00 Eastern
    assert body["end"] == {"dateTime": "2026-10-13T16:30:00", "timeZone": "UTC"}    # same length


def test_moving_someone_elses_meeting_is_refused_with_a_way_forward():
    graph, actions = setup()
    with pytest.raises(ValueError, match="can't move it"):
        propose_move(graph, actions, "vendor", "2026-10-15T10:00", now=NOW)


@pytest.mark.parametrize("eid, call", [
    ("sync", ("POST", f"/users/{DAVE}/events/sync/cancel")),      # Dave's meeting with people: they're told
    ("solo", ("DELETE", f"/users/{DAVE}/events/solo")),           # just his own block
    ("vendor", ("POST", f"/users/{DAVE}/events/vendor/decline")), # someone else's: he declines
])
def test_cancelling_does_the_right_thing_for_whose_meeting_it_is(eid, call):
    graph, actions = setup()
    slip = propose_cancel(graph, actions, eid)
    actions.approve(slip["action_id"], decided_by="test")
    assert [c[:2] for c in graph.calls if c[0] in ("POST", "DELETE")] == [call]


def test_chat_tools_return_pending_slips_that_become_cards():
    graph, actions = setup()
    tools = {t.spec.name: t for t in change_tools(graph, actions)}
    result = tools["cancel_event"].handler({"event_id": "sync", "message": "Something came up"})
    assert result["status"] == "pending" and "approval" in result["note"]
    trace = ToolTrace(name="cancel_event", input={}, output=__import__("json").dumps(result), is_error=False)
    [card] = build_cards([trace])
    assert card["type"] == "action" and card["summary"].startswith("Cancel “Renewals sync”")
