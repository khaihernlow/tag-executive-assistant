import pytest

from agent.invite_context import invite_context, key_words
from store.db import Store

DAVE = "dave@tag.example"


def event(subject, attendees, organizer="garrett@tag.example"):
    return {"id": "inv1", "subject": subject, "showAs": "tentative", "responseStatus": {"response": "notResponded"},
            "start": {"dateTime": "2026-10-28T15:30:00", "timeZone": "UTC"}, "end": {"dateTime": "2026-10-28T16:00:00", "timeZone": "UTC"},
            "organizer": {"emailAddress": {"name": "Garrett Smith", "address": organizer}},
            "attendees": [{"emailAddress": {"name": n, "address": a}} for n, a in attendees], "bodyPreview": ""}


def mail(mid, subject, preview, received="2026-10-14T13:00:00Z", sender="Dave Vener"):
    return {"id": mid, "subject": subject, "bodyPreview": preview, "receivedDateTime": received, "webLink": f"https://outlook/{mid}",
            "from": {"emailAddress": {"name": sender, "address": "x@y.example"}}}


class Graph:
    mailbox = DAVE

    def __init__(self, ev, by_person=(), by_words=()):
        self.ev, self.by_person, self.by_words = ev, list(by_person), list(by_words)
        self.searches = []

    def get(self, path, params=None, headers=None):
        if (params or {}).get("$select") == "body":
            return {"body": {"content": "Agenda: review the CrushBank proposal.\n________________\nMicrosoft Teams meeting\nJoin: https://teams/x"}}
        return self.ev

    def get_all(self, path, params=None, limit=500, headers=None):
        query = (params or {}).get("$search", "")
        self.searches.append(query)
        return self.by_person if query.startswith('"participants:') else self.by_words


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("GRAPH_MAILBOX", DAVE)
    monkeypatch.setenv("ASSISTANT_TIMEZONE", "America/New_York")


def test_an_internal_client_meeting_shows_emails_and_notes_that_name_the_client():
    assert key_words("CrushBank - Prep Meeting") == ["CrushBank"]
    store = Store(":memory:")
    store.add_note(kind="meeting", title="CrushBank intro", source="maestro", started_at="2026-10-01T10:00", people=[],
                   status="ready", digest={"headline": "They want an AI pilot."})
    store.add_note(kind="meeting", title="Garrett one on one", source="maestro", started_at="2026-10-13T10:00", people=[],
                   status="ready", digest={"headline": "Pipeline review."})
    graph = Graph(event("CrushBank - Prep Meeting", [("Garrett Smith", "garrett@tag.example"), ("Dave", DAVE)]),
                  by_words=[mail("a", "Accepted: CrushBank - Prep Meeting", "CrushBank"),          # a calendar reply: skipped
                            mail("b", "CrushBank - Prep Meeting", "CrushBank invite"),             # the invite itself: skipped
                            mail("c", "Re: AI pilot", "CrushBank sent the pricing over."),
                            mail("d", "Lunch?", "Nothing about them")])                            # doesn't name them: skipped
    c = invite_context(graph, store, "inv1")
    assert c["about"] == "Agenda: review the CrushBank proposal."                                  # without the Teams boilerplate
    assert [e["subject"] for e in c["emails"]] == ["Re: AI pilot"]
    assert [n["title"] for n in c["last_notes"]] == ["CrushBank intro"]                            # not the latest with Garrett
    assert [p["outside"] for p in c["people"]] == [False]                                          # Dave himself left out


def test_with_outside_people_it_reads_the_mail_with_them():
    graph = Graph(event("Tom Amell & Dave Vener", [("Thomas Amell", "tom@bank.example"), ("Garrett Smith", "garrett@tag.example")]),
                  by_person=[mail("x", "Re: Meeting", "Thanks for coordinating Tom's calendar.")])
    c = invite_context(graph, Store(":memory:"), "inv1")
    assert graph.searches[0] == '"participants:tom@bank.example"'
    assert [e["subject"] for e in c["emails"]] == ["Re: Meeting"]
    assert [(p["name"], p["outside"]) for p in c["people"]] == [("Thomas Amell", True), ("Garrett Smith", False)]
