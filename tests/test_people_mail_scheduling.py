import json
from datetime import datetime, timezone

import pytest

from agent.mail import build_search, mail_tools, search_mail
from agent.people import Person, find_people, match_note, name_score
from agent.scheduling import scheduling_tools
from agent.tools import ToolRegistry
from llm.provider import ToolCall


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("GRAPH_MAILBOX", "dvener@tagsolutions.com")
    monkeypatch.setenv("ASSISTANT_TIMEZONE", "America/New_York")


class FakeGraph:
    """Answers Graph paths from a dict of {(path, marker): items}."""

    mailbox = "dvener@tagsolutions.com"

    def __init__(self, routes=None, posts=None, single=None):
        self.routes = routes or {}
        self.posts = posts or {}
        self.single = single or {}
        self.calls = []

    def get_all(self, path, params=None, limit=500, headers=None):
        self.calls.append((path, params, headers))
        search = (params or {}).get("$search")
        return self.routes.get((path, search), self.routes.get((path, None), []))

    def get(self, path, params=None, headers=None):
        self.calls.append((path, params, headers))
        return self.single[path]

    def post(self, path, body, headers=None):
        self.calls.append((path, body, headers))
        return self.posts[path]


# ── people ────────────────────────────────────────────────────────────────────

def people_entry(name, email, **extra):
    return {"displayName": name, "scoredEmailAddresses": [{"address": email}], **extra}


def test_name_score_prefers_exact_and_first_name_matches():
    assert name_score("Kai Low", "Kai Low", "klow@x.com") == 1.0
    assert name_score("kai", "Kai Low", "klow@x.com") == 0.95
    assert name_score("Khai", "Kai Low", "klow@x.com") > 0.6
    assert name_score("Bob", "Kai Low", "klow@x.com") < 0.5


def test_find_people_merges_sources_and_marks_internal():
    graph = FakeGraph({
        ("/me/people", '"Kai"'): [people_entry("Kai Low", "KLow@tagsolutions.com", jobTitle="Engineer")],
        ("/users", '"displayName:Kai" OR "mail:Kai"'): [
            {"displayName": "Kai Low", "mail": "klow@tagsolutions.com"},
            {"displayName": "Kai Smith", "mail": "ksmith@tagsolutions.com", "accountEnabled": False},
        ],
    })
    people = find_people(graph, "Kai")
    assert [(p.email, p.internal, p.title) for p in people] == [("klow@tagsolutions.com", True, "Engineer")]
    directory_call = [c for c in graph.calls if c[0] == "/users"][0]
    assert directory_call[2] == {"ConsistencyLevel": "eventual"}


def test_nickname_beats_short_prefix_and_relay_addresses_are_dropped():
    graph = FakeGraph({
        ("/me/people", '"Kai"'): [people_entry("Kaitlyn Ward", "kaitlyn.ward@vendor.example"),
                                  people_entry("Kai Relay", "kai=tagsolutions.com@hs-send.com")],
        ("/users", None): [{"displayName": "Khaihern Low", "mail": "klow@tagsolutions.com"}],
    })
    people = find_people(graph, "Kai")
    assert [p.email for p in people] == ["klow@tagsolutions.com", "kaitlyn.ward@vendor.example"]
    assert match_note(people).startswith("Best guess only (Khaihern Low)")


def test_match_note_only_flags_real_ties():
    def p(name, score, internal):
        return Person(name=name, email=name.lower() + "@x.com", internal=internal, score=score)

    assert match_note([p("Garrett Smith", 0.95, True), p("Garrett Smith", 0.95, False)]) == "Confident match."
    assert match_note([p("Joe Yetto", 0.95, True), p("Joe Barone", 0.95, True)]).startswith("Several")
    assert match_note([]).startswith("No one found")


def test_find_people_falls_back_to_fuzzy_for_misspellings():
    graph = FakeGraph({
        ("/me/people", None): [people_entry("Kai Low", "klow@tagsolutions.com"),
                               people_entry("Joe Baronet", "joe@cpa.com")],
    })
    people = find_people(graph, "Khai")
    assert [p.email for p in people] == ["klow@tagsolutions.com"]


# ── mail ──────────────────────────────────────────────────────────────────────

def message(mid, received, subject="Project X", sender="klow@tagsolutions.com"):
    return {"id": mid, "subject": subject, "receivedDateTime": received, "bodyPreview": "hi",
            "from": {"emailAddress": {"name": "Kai Low", "address": sender}}, "isRead": False}


def test_build_search_quotes_phrases():
    assert build_search("Khai", "Project X") == '"from:Khai Project X"'
    assert build_search("Kai Low", None) == '"from:Kai Low"'
    assert build_search("klow@tagsolutions.com", 'say "hi"') == '"from:klow@tagsolutions.com say hi"'
    assert build_search(None, None) == ""
    assert build_search("Kai", "Chelsi SDR", any_word=True) == '"from:Kai (Chelsi OR SDR)"'


def test_search_mail_widens_to_some_words_and_lists_real_attachments():
    path = "/users/dvener@tagsolutions.com/messages"
    resume_mail = {**message("r", "2026-09-24T12:00:00Z", subject="New candidates"),
                   "attachments": [{"name": "logo.png", "isInline": True}, {"name": "Chelsi Resume.pdf", "isInline": False}]}
    graph = FakeGraph({
        (path, '"Chelsi interview"'): [message("i", "2026-09-28T12:00:00Z", subject="Interview Chelsi")],
        (path, '"(Chelsi OR interview)"'): [message("i", "2026-09-28T12:00:00Z"), resume_mail],
    })
    found = search_mail(graph, about="Chelsi interview", now=datetime(2026, 10, 5, tzinfo=timezone.utc))
    assert [(m["id"], m["matched"]) for m in found] == [("i", "all words"), ("r", "some words")]
    assert found[1]["attachments"] == ["Chelsi Resume.pdf"]


def test_search_mail_drops_old_results_and_sorts_newest_first():
    path = "/users/dvener@tagsolutions.com/messages"
    graph = FakeGraph({(path, '"from:Kai Project X"'): [
        message("old", "2026-07-01T12:00:00Z"),
        message("a", "2026-10-01T12:00:00Z"),
        message("b", "2026-10-04T12:00:00Z"),
    ]})
    found = search_mail(graph, "Kai", "Project X", since_days=30, now=datetime(2026, 10, 5, tzinfo=timezone.utc))
    assert [m["id"] for m in found] == ["b", "a"]
    assert found[0]["received"] == "Sun Oct 4 8:00 AM"
    assert found[0]["from"]["email"] == "klow@tagsolutions.com"


def test_read_email_prefers_unique_body_and_lists_attachments():
    path = "/users/dvener@tagsolutions.com/messages/m1"
    graph = FakeGraph(
        routes={(path + "/attachments", None): [{"name": "plan.pdf", "contentType": "application/pdf", "size": 10}]},
        single={path: {**message("m1", "2026-10-04T12:00:00Z"), "hasAttachments": True,
                       "uniqueBody": {"content": "Can we meet Thursday?"},
                       "body": {"content": "Can we meet Thursday?\n> old quoted thread"}}},
    )
    content, is_error = ToolRegistry(mail_tools(graph)).run(ToolCall("t", "read_email", {"message_id": "m1"}))
    email = json.loads(content)
    assert not is_error
    assert email["body"] == "Can we meet Thursday?"
    assert email["attachments"][0]["name"] == "plan.pdf"
    assert graph.calls[0][2] == {"Prefer": 'outlook.body-content-type="text"'}


# ── scheduling ────────────────────────────────────────────────────────────────

def busy(start_utc, end_utc, status="busy"):
    return {"status": status, "start": {"dateTime": start_utc, "timeZone": "UTC"},
            "end": {"dateTime": end_utc, "timeZone": "UTC"}}


SCHEDULE_PATH = "/users/dvener@tagsolutions.com/calendar/getSchedule"


def run_mutual(graph, **args):
    content, is_error = ToolRegistry(scheduling_tools(graph)).run(ToolCall("t", "find_mutual_time", {
        "start_date": "2026-10-13", "end_date": "2026-10-13", "duration_minutes": 60, **args}))
    return json.loads(content) if not is_error else content, is_error


def test_mutual_time_intersects_everyone_and_includes_dave():
    graph = FakeGraph(posts={SCHEDULE_PATH: {"value": [
        {"scheduleId": "dvener@tagsolutions.com", "scheduleItems": [busy("2026-10-13T12:00:00", "2026-10-13T16:00:00")]},  # 8-12
        {"scheduleId": "klow@tagsolutions.com", "scheduleItems": [busy("2026-10-13T17:00:00", "2026-10-13T19:00:00", "tentative")]},  # 1-3
    ]}})
    result, is_error = run_mutual(graph, attendees=["KLow@tagsolutions.com"])

    assert not is_error
    assert result["free_windows"] == [{"from": "Tue Oct 13 12:00 PM", "to": "Tue Oct 13 1:00 PM"},
                                      {"from": "Tue Oct 13 3:00 PM", "to": "Tue Oct 13 5:00 PM"}]
    body = graph.calls[0][1]
    assert body["schedules"] == ["dvener@tagsolutions.com", "klow@tagsolutions.com"]
    assert body["startTime"] == {"dateTime": "2026-10-13T04:00:00", "timeZone": "UTC"}


def test_mutual_time_flags_calendars_it_cannot_see():
    graph = FakeGraph(posts={SCHEDULE_PATH: {"value": [
        {"scheduleId": "dvener@tagsolutions.com", "scheduleItems": []},
        {"scheduleId": "client@acme.com", "error": {"message": "Not found", "responseCode": "ErrorMailRecipientNotFound"}},
    ]}})
    result, _ = run_mutual(graph, attendees=["client@acme.com"])
    assert result["checked"] == ["dvener@tagsolutions.com"]
    assert result["could_not_check"][0]["email"] == "client@acme.com"
    assert "note" in result


def test_mutual_time_rejects_names_instead_of_addresses():
    content, is_error = run_mutual(FakeGraph(), attendees=["Kai"])
    assert is_error and "find_person" in content
