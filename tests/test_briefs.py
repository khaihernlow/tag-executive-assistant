import json
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from agent.briefs import fingerprint, needs_brief, prepare_brief, topic_words
from agent.calendar import Event
from llm.provider import LLMResponse
from store.db import Store

NY = ZoneInfo("America/New_York")
DAVE = "dave@tag.example"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("GRAPH_MAILBOX", DAVE)
    monkeypatch.setenv("ASSISTANT_TIMEZONE", "America/New_York")


def event(subject="Intro call", attendees=(DAVE, "owner@client.example"), hour=11, **kw):
    start = datetime(2026, 10, 8, hour, 0, tzinfo=NY)
    return Event(subject=subject, start=start, end=start.replace(hour=hour + 1), all_day=kw.get("all_day", False),
                 show_as="busy", location="", attendees=list(attendees), organizer="Dave", online=True,
                 attendee_emails=tuple(attendees), id=kw.get("id", "evt1"), organizer_email=DAVE,
                 response=kw.get("response", "organizer"), description=kw.get("description", ""))


# ── which meetings get a brief ───────────────────────────────────────────────

def test_external_meetings_and_interviews_get_briefs_internal_and_personal_dont():
    assert needs_brief(event(), DAVE, "tag.example")
    assert needs_brief(event("Interview: NOC Engineer", attendees=(DAVE, "dom@tag.example")), DAVE, "tag.example")
    assert not needs_brief(event("RJ 1:1", attendees=(DAVE, "rj@tag.example")), DAVE, "tag.example")
    assert not needs_brief(event(response="declined"), DAVE, "tag.example")
    assert not needs_brief(event(all_day=True), DAVE, "tag.example")
    assert not needs_brief(event("Dinner at the lake", hour=19), DAVE, "tag.example")
    assert not needs_brief(event(attendees=()), DAVE, "tag.example")


def test_fingerprint_changes_with_the_invite_and_topic_words_skip_filler():
    assert fingerprint(event()) == fingerprint(event())
    assert fingerprint(event()) != fingerprint(event(description="New agenda"))
    assert topic_words("Hasroon Pervez In Person Interview with TAG Solutions") == ["Hasroon", "Pervez"]


# ── generation ───────────────────────────────────────────────────────────────

class FakeGraph:
    mailbox = DAVE

    def get_all(self, path, params=None, limit=500, headers=None):
        if path.endswith("/messages") and "participants:owner@client.example" in (params or {}).get("$search", ""):
            return [{"id": "m1", "receivedDateTime": "2026-10-01T12:00:00Z"}]
        if path.endswith("/attachments"):
            return [{"id": "a1", "name": "Proposal.docx", "contentType": "", "size": 100}]
        return []

    def get(self, path, params=None, headers=None):
        return {"id": "m1", "subject": "Re: proposal", "receivedDateTime": "2026-10-01T12:00:00Z",
                "from": {"emailAddress": {"name": "Pat Owner", "address": "owner@client.example"}},
                "uniqueBody": {"content": "Looking forward to Thursday."}, "hasAttachments": True}

    def get_bytes(self, path):
        import io, zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("word/document.xml", "<w:p><w:t>Scope: managed IT for 40 seats</w:t></w:p>")
        return buf.getvalue()


class BriefLLM:
    def __init__(self):
        self.material = None

    def complete(self, messages, system=None, tools=None, max_tokens=2048, temperature=None, tool_choice=None):
        self.material = messages[0]["content"]
        return LLMResponse(content=[{"type": "tool_use", "id": "b", "name": "write_brief", "input": {
            "headline": "Proposal review with Pat (contact pat.0wner@client.example)",
            "who": [{"name": "Pat Owner", "role": "Client"}],
            "context": ["Pat expects to review the proposal [Email 1]"],
            "background": ["40 seats of managed IT in scope [Attachment 1]"],
            "prep": ["Confirm timeline"], "gaps": [], "sources": ["Email 1", "Attachment 1"],
        }}], stop_reason="tool_use")


def test_prepare_brief_gathers_mail_and_attachments_and_fact_checks():
    store, llm = Store(":memory:"), BriefLLM()
    assert prepare_brief(FakeGraph(), llm, store, event()) is True
    row = store.get_brief("evt1")
    assert row["status"] == "ready"
    assert "Scope: managed IT for 40 seats" in llm.material          # attachment text reached the writer
    assert row["brief"]["material"]["attachments"] == [{"label": "Attachment 1", "name": "Proposal.docx"}]
    assert "owner@client.example" in row["brief"]["headline"]         # mistyped address corrected
    assert prepare_brief(FakeGraph(), llm, store, event()) is False   # unchanged invite: nothing to do
    assert prepare_brief(FakeGraph(), llm, store, event(description="moved")) is True  # changed: refresh


def test_failures_are_recorded_and_stuck_briefs_can_retry():
    class Broken(BriefLLM):
        def complete(self, *a, **k):
            raise RuntimeError("gateway down")

    store = Store(":memory:")
    prepare_brief(FakeGraph(), Broken(), store, event())
    assert store.get_brief("evt1")["status"] == "failed"

    assert store.claim_brief("evt2", "x", "t", "fp")
    assert not store.claim_brief("evt2", "x", "t", "fp")  # already preparing
    store.reset_stuck_briefs()
    assert store.claim_brief("evt2", "x", "t", "fp")


# ── API + chat about a brief ─────────────────────────────────────────────────

def test_brief_endpoints_and_topic_conversation(monkeypatch):
    monkeypatch.setenv("REQUIRE_AUTH", "False")
    from agent.actions import Actions
    from agent.assistant import Assistant
    from agent.tools import ToolRegistry
    from app.main import app

    store = Store(":memory:")
    prepare_brief(FakeGraph(), BriefLLM(), store, event())

    seen = {}

    class ChatLLM:
        def complete(self, messages, system=None, **kwargs):
            seen["system"] = system
            return LLMResponse(content=[{"type": "text", "text": "Focus on the timeline."}], stop_reason="end_turn")

    prepared = []
    worker = SimpleNamespace(prepare_now=lambda e, force=False: prepared.append((e.id, force)))
    graph = SimpleNamespace(mailbox=DAVE, get=lambda path, params=None, headers=None: {
        "id": "evt9", "subject": "Later", "start": {"dateTime": "2026-10-09T15:00:00", "timeZone": "UTC"},
        "end": {"dateTime": "2026-10-09T16:00:00", "timeZone": "UTC"}})
    actions = Actions(store, [], auto=set())
    app.state.services = SimpleNamespace(graph=graph, store=store, actions=actions, worker=worker,
                                         assistant=Assistant(ChatLLM(), ToolRegistry([]), store, actions))
    try:
        http = TestClient(app)
        got = http.get("/api/briefs/evt1").json()
        assert got["status"] == "ready" and got["conversation_id"] is None
        assert http.get("/api/briefs/nope").status_code == 404

        assert http.post("/api/briefs/evt9/prepare?refresh=true").json()["status"] == "preparing"
        assert prepared == [("evt9", True)]

        reply = http.post("/api/chat", json={"message": "What should I focus on?", "brief_event_id": "evt1"}).json()
        assert "Proposal review with Pat" in seen["system"]          # the brief was in the model's view
        assert http.get("/api/briefs/evt1").json()["conversation_id"] == reply["conversation_id"]
        assert http.get(f"/api/conversations/{reply['conversation_id']}").json()["title"] == "Brief: Intro call"
    finally:
        app.state.services = None
