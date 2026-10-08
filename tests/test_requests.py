from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from agent.actions import Actions
from agent.requests import (
    candidates, draft_reply, open_requests_view, propose_booking, propose_reply, request_kinds, scan, suggest_slots,
)
from llm.provider import LLMResponse
from store.db import Store

NY = ZoneInfo("America/New_York")
DAVE = "dave@tag.example"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    from agent import requests as req

    monkeypatch.setenv("GRAPH_MAILBOX", DAVE)
    monkeypatch.setenv("ASSISTANT_TIMEZONE", "America/New_York")
    req._style_cache.clear()
    req._skip_folder_ids.clear()


def mail(mid, sender, subject, preview="", thread=None, received="2026-10-08T14:00:00Z", kind=None):
    m = {"id": mid, "subject": subject, "bodyPreview": preview, "conversationId": thread or mid,
         "receivedDateTime": received, "webLink": f"https://outlook/{mid}",
         "from": {"emailAddress": {"name": sender.split("@")[0].title(), "address": sender}}}
    if kind:
        m["@odata.type"] = kind
    return m


INBOX = [
    mail("req", "mark@vendor.example", "AI audit follow-up", "Could we find time next week for a call?", thread="t1"),
    mail("old", "mark@vendor.example", "AI audit", "Can we meet?", thread="t1", received="2026-10-07T14:00:00Z"),
    mail("invite", "kai@tag.example", "Accepted: Sync", "Let's meet", kind="#microsoft.graph.eventMessage"),
    mail("mine", DAVE, "Lunch", "Lunch next week?"),
    mail("news", "news@shop.example", "October deals", "Save 20% on everything"),
    {**mail("spam", "x@spam.example", "Meet hot singles", "Free call now"), "parentFolderId": "junk-folder"},
    mail("staff", "garrett@tag.example", "Quick sync", "Do you have time Thursday morning to sync on renewals?"),
]


class FakeGraph:
    mailbox = DAVE

    def __init__(self, busy=None, sent_after=None):
        self.busy = busy or []
        self.sent_after = sent_after
        self.posts = []

    def get(self, path, params=None, headers=None):
        return {"id": "junk-folder"} if path.endswith("/junkemail") else {"id": path.rsplit("/", 1)[-1]}

    def get_all(self, path, params=None, limit=500, headers=None):
        if path == f"/users/{DAVE}/messages":
            return INBOX
        if path.endswith("/sentitems/messages"):
            if "conversationId" in (params or {}).get("$filter", ""):
                return [{"id": "s1", "sentDateTime": self.sent_after}] if self.sent_after else []
            return [{"bodyPreview": "Sounds great, let's do it. Dave Vener President"}, {"bodyPreview": "Yes"}]
        return []

    def post(self, path, body, headers=None):
        self.posts.append((path, body))
        if path.endswith("getSchedule"):
            return {"value": [{"scheduleId": s, "scheduleItems": self.busy} for s in body["schedules"]]}
        if path.endswith("/events"):
            return {"id": "evt", "webLink": "https://outlook/evt", "onlineMeeting": {"joinUrl": "https://teams/j"}}
        return {}


class FakeLLM:
    def __init__(self, tool, payload):
        self.tool, self.payload, self.calls = tool, payload, []

    def complete(self, messages, system=None, tools=None, max_tokens=2048, temperature=None, tool_choice=None):
        self.calls.append(messages[0]["content"])
        return LLMResponse(content=[{"type": "tool_use", "id": "x", "name": self.tool, "input": self.payload}],
                           stop_reason="tool_use")


# ── spotting ─────────────────────────────────────────────────────────────────

def test_candidates_skip_invites_own_mail_junk_older_thread_messages_and_non_meeting_mail():
    assert [m["id"] for m in candidates(FakeGraph(), set())] == ["req", "staff"]


def test_scan_saves_requests_and_ignores_the_rest_without_reclassifying():
    llm = FakeLLM("record_requests", {"emails": [
        {"n": 0, "kind": "asks_for_times", "purpose": "AI audit follow-up", "earliest_date": "2026-10-12",
         "latest_date": "2026-10-16", "format": "teams"},
        {"n": 1, "kind": "not_a_request"},
    ]})
    store = Store(":memory:")
    assert scan(FakeGraph(), llm, store) == 1
    [row] = store.open_requests()
    assert row["message_id"] == "req" and row["request"]["from_email"] == "mark@vendor.example"
    assert store.get_request("staff")["status"] == "ignored"

    scan(FakeGraph(), llm, store)
    assert len(llm.calls) == 1  # both already seen: no second model call


def test_dave_replying_in_outlook_retires_the_request():
    store = Store(":memory:")
    store.save_request("req", "t1", "2026-10-08T14:00:00Z", "new", {"from_name": "Mark"})
    scan(FakeGraph(sent_after="2026-10-08T15:00:00Z"), FakeLLM("record_requests", {"emails": []}), store)
    assert store.get_request("req")["status"] == "replied"


# ── suggesting times ─────────────────────────────────────────────────────────

def test_suggest_slots_one_per_day_inside_hours_and_checks_staff_calendars():
    graph = FakeGraph(busy=[{"status": "busy", "start": {"dateTime": "2026-10-12T12:00:00", "timeZone": "UTC"},
                             "end": {"dateTime": "2026-10-12T15:00:00", "timeZone": "UTC"}}])  # Mon 8-11 local
    now = datetime(2026, 10, 8, 16, 0, tzinfo=NY)
    picks = suggest_slots(graph, {"earliest_date": "2026-10-12", "latest_date": "2026-10-14",
                                  "from_email": "garrett@tag.example", "time_of_day": "morning"}, now=now)
    assert [p.strftime("%a %H:%M") for p in picks] == ["Mon 11:00", "Tue 08:00", "Wed 08:00"]
    schedule_call = [b for p, b in graph.posts if p.endswith("getSchedule")][0]
    assert schedule_call["schedules"] == [DAVE, "garrett@tag.example"]


# ── replying and booking ─────────────────────────────────────────────────────

REQUEST = {"kind": "asks_for_times", "purpose": "AI audit follow-up", "from_name": "Mark Greco",
           "from_email": "mark@vendor.example", "subject": "AI audit follow-up", "preview": "Next week?",
           "format": "teams"}
SLOTS = [datetime(2099, 10, 13, 10, 0, tzinfo=NY), datetime(2099, 10, 14, 14, 30, tzinfo=NY)]


def test_draft_uses_the_model_but_falls_back_when_a_time_is_missing():
    good = FakeLLM("write_reply", {"comment": "Hi Mark — happy to.\n10:00 AM Tue\n2:30 PM Wed\nDave"})
    assert draft_reply(good, FakeGraph(), REQUEST, SLOTS).startswith("Hi Mark, happy to.")
    assert "Sounds great, let's do it." in good.calls[0]  # Dave's own emails as style samples

    bad = FakeLLM("write_reply", {"comment": "Hi Mark, how about Tuesday?"})
    fallback = draft_reply(bad, FakeGraph(), REQUEST, SLOTS)
    assert "Tuesday, Oct 13 at 10:00 AM ET" in fallback and "Wednesday, Oct 14 at 2:30 PM ET" in fallback


def test_reply_waits_for_approval_edits_only_the_text_and_sends_in_thread():
    graph, store = FakeGraph(), Store(":memory:")
    actions = Actions(store, request_kinds(graph, store), auto=set())
    store.save_request("req", "t1", "2026-10-08T14:00:00Z", "new", REQUEST)
    llm = FakeLLM("write_reply", {"comment": "Hi Mark,\n10:00 AM\n2:30 PM\nDave"})

    proposal = propose_reply(graph, llm, store, actions, "req", [s.isoformat() for s in SLOTS])
    assert proposal["status"] == "pending" and proposal["email"]["to"] == "mark@vendor.example"
    assert not [p for p, _ in graph.posts if p.endswith("/reply")]

    done = actions.approve(proposal["action_id"], "dave", edits={"comment": "Hi Mark,\n10:00 <b>works</b>\nDave",
                                                                 "to": "attacker@evil.example"})
    assert done["status"] == "executed"
    path, body = [p for p in graph.posts if p[0].endswith("/reply")][0]
    assert path == f"/users/{DAVE}/messages/req/reply"
    assert body == {"comment": "Hi Mark,<br>10:00 &lt;b&gt;works&lt;/b&gt;<br>Dave"}
    assert done["payload"]["to"] == "mark@vendor.example"  # recipient can't be edited
    assert store.get_request("req")["status"] == "replied"
    assert store.open_requests() == []


def test_booking_a_proposed_time_creates_an_invite_after_approval():
    graph, store = FakeGraph(), Store(":memory:")
    actions = Actions(store, request_kinds(graph, store), auto=set())
    store.save_request("req", "t1", "2026-10-08T14:00:00Z", "new", REQUEST)

    proposal = propose_booking(graph, store, actions, "req", SLOTS[0].isoformat())
    assert "AI audit follow-up" in proposal["summary"] and "Mark Greco" in proposal["summary"]
    actions.approve(proposal["action_id"], "dave")
    assert any(p.endswith("/events") for p, _ in graph.posts)
    assert store.get_request("req")["status"] == "booked"

    with pytest.raises(ValueError, match="no longer open"):
        propose_reply(graph, FakeLLM("write_reply", {}), store, actions, "req", [SLOTS[0].isoformat()])


def test_today_view_lists_open_requests_with_times_and_pending_slip():
    graph, store = FakeGraph(), Store(":memory:")
    store.save_request("req", "t1", "2026-10-08T14:00:00Z", "new",
                       {**REQUEST, "kind": "proposes_time", "proposed_start": "2099-10-13T10:00"})
    [view] = open_requests_view(graph, store, store)
    assert view["from"] == "Mark Greco" and view["proposed"]["label"] == "Tuesday, Oct 13 at 10:00 AM ET"
    assert view["action"] is None
