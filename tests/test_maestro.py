import base64
import json

import pytest

from agent import maestro
from agent.notes import new_meeting_note
from store.db import Store

# The shape of a real TeamsMaestro email (Oct 2026), trimmed
LINK = base64.urlsafe_b64encode(json.dumps({"p": json.dumps({"url": "https://app.teamsmaestro.com/summary/abc-123"})}).encode()).decode().rstrip("=")
EMAIL = f"""<html><head><style>.x{{color:red}}</style></head><body>
<table><tr><td><h3>Meeting notes</h3><a href="https://mandrillapp.com/track/click/1/app.teamsmaestro.com?p={LINK}">✦ Ask AI about this meeting</a></td></tr></table>
<h3>Trinity Alliance Meeting Prep</h3>&nbsp;<table><tr><td>Oct 20, 2026&nbsp;•&nbsp;36 minutes&nbsp;•&nbsp;<a href="#">View full transcript</a><br><span>Notes were taken by AI.</span></td></tr></table>
<div><div>Purpose </div><div><div><span>Strategize on securing a key account against competitors. </span><br>&nbsp; </div></div></div>
<div><div>Takeaways </div><div><ul><li>Freddy is a strong advocate but lacks decision-making power. </li><li>Jennifer, the CFO, is the key decision-maker. </li></ul></div></div>
<div><div>Detailed summary </div><div><div>&nbsp;</div></div><div>Pricing strategy </div><div><ul><li>Thrive's pricing is $24,000 a month, while TAG Solutions is at $19,000. </li></ul></div>
<div>Video proposal </div><div><ul><li>Use iPhones to record quick 15-second clips. </li></ul></div></div>
<div>Action items</div><div><table><tr><td><a href="#"><strong>Create a video with employee testimonials.</strong><br><span>RJ Gray</span></a></td></tr>
<tr><td><a href="#"><strong>Contact Walter Thorne to advocate with the CEO.</strong><br><span>Dave Vener</span></a></td></tr></table></div>
<div><div>Attendees</div><div>Dave Vener<span>*</span>, RJ Gray<span>*</span>, Garrett Smith </div><br></div>TeamsMaestro users <span>*</span> <br>
AI generated content may be incorrect. ©2026 MaestroLabs. All rights reserved.</body></html>"""


def test_the_email_is_read_by_its_structure():
    n = maestro.parse(EMAIL)
    assert (n["title"], str(n["day"]), n["minutes"]) == ("Trinity Alliance Meeting Prep", "2026-10-20", 36)
    assert n["purpose"] == "Strategize on securing a key account against competitors."
    assert n["takeaways"] == ["Freddy is a strong advocate but lacks decision-making power.",
                              "Jennifer, the CFO, is the key decision-maker."]
    assert [s["heading"] for s in n["sections"]] == ["Pricing strategy", "Video proposal"]
    assert n["action_items"] == [{"what": "Create a video with employee testimonials.", "owner": "RJ Gray"},
                                 {"what": "Contact Walter Thorne to advocate with the CEO.", "owner": "Dave Vener"}]
    assert n["attendees"] == ["Dave Vener", "RJ Gray", "Garrett Smith"]                      # not the footer
    assert maestro.summary_link(EMAIL) == "https://app.teamsmaestro.com/summary/abc-123"
    d = maestro.as_digest(n)
    assert d["headline"] == n["purpose"] and [a["dave"] for a in d["action_items"]] == [False, True]
    assert maestro.as_transcript(n).splitlines()[0] == "[L1] Purpose: Strategize on securing a key account against competitors."


class Graph:
    mailbox = "dave@tag.example"

    def __init__(self, events, emails):
        self.events, self.emails = events, emails

    def calendar_view(self, start, end):
        return self.events

    def get_all(self, path, params=None, limit=500, headers=None):
        return [{"id": mid, "subject": "Summary: Trinity Alliance Meeting Prep", "receivedDateTime": "2026-10-20T18:38:00Z"}
                for mid in self.emails]

    def get(self, path, params=None, headers=None):
        return {"body": {"content": EMAIL}}


PREP = {"id": "ev-prep", "subject": "Trinity Alliance Meeting Prep", "showAs": "busy",
        "start": {"dateTime": "2026-10-20T17:30:00", "timeZone": "UTC"}, "end": {"dateTime": "2026-10-20T18:00:00", "timeZone": "UTC"},
        "attendees": [{"emailAddress": {"name": "RJ Gray", "address": "rgray@tag.example"}}],
        "organizer": {"emailAddress": {"name": "Dave", "address": "dave@tag.example"}}}
OTHER = {**PREP, "id": "ev-other", "subject": "Lunch"}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("ASSISTANT_TIMEZONE", "America/New_York")
    monkeypatch.setenv("GRAPH_MAILBOX", "dave@tag.example")


def test_each_write_up_becomes_notes_on_its_meeting_once():
    store = Store(":memory:")
    graph = Graph([OTHER, PREP], ["m1"])
    assert maestro.ingest(graph, store) == 1
    note = store.note_for_event("ev-prep")
    assert note["source"] == "maestro" and note["status"] == "ready" and note["title"] == "Trinity Alliance Meeting Prep"
    assert note["digest"]["link"] == "https://app.teamsmaestro.com/summary/abc-123"
    assert note["people"][0]["email"] == "rgray@tag.example"         # from the calendar invite
    assert maestro.ingest(graph, store) == 0                         # the same email again: nothing new
    assert len(store.list_notes()) == 1


def test_a_meeting_that_already_has_notes_gets_this_as_another_part():
    store = Store(":memory:")
    nid = new_meeting_note(store, title="Trinity Alliance Meeting Prep", source="recording", audio_path="a.webm",
                           event={"id": "ev-prep", "subject": "Trinity Alliance Meeting Prep"})
    assert maestro.ingest(Graph([PREP], ["m1"]), store) == 1
    parts = store.get_note(nid)["parts"]
    assert [(p["n"], p["source"]) for p in parts] == [(1, "recording"), (2, "maestro")]


def test_no_matching_meeting_still_keeps_the_notes():
    store = Store(":memory:")
    assert maestro.ingest(Graph([OTHER], ["m1"]), store) == 1
    [note] = store.list_notes()
    assert note["event_id"] is None and [p["name"] for p in note["people"]] == ["Dave Vener", "RJ Gray", "Garrett Smith"]
