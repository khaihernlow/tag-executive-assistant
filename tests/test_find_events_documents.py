import io
import json
import zipfile
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from agent.calendar import calendar_tools, find_events, parse_event
from agent.documents import UnreadableDocument, extract_text
from agent.mail import read_attachment
from agent.tools import ToolRegistry
from llm.provider import ToolCall

NY = ZoneInfo("America/New_York")


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("ASSISTANT_TIMEZONE", "America/New_York")


def raw_event(subject, start_utc, end_utc, attendees=(), preview="", location=""):
    return {
        "subject": subject,
        "start": {"dateTime": start_utc, "timeZone": "UTC"},
        "end": {"dateTime": end_utc, "timeZone": "UTC"},
        "showAs": "busy",
        "location": {"displayName": location},
        "bodyPreview": preview,
        "attendees": [{"emailAddress": {"name": n, "address": a}} for n, a in attendees],
    }


INTERVIEW = raw_event(
    "Interview SDR Role", "2026-10-06T19:00:00", "2026-10-06T19:30:00",
    attendees=[("Jordan Rivera", "candidate@mail.example"), ("Sam", "gabe@tag.example")],
    preview="Interview discussion regarding the sales opportunity. ________________ Microsoft Teams Need help? Join...",
)
SALES = raw_event("Sales meeting", "2026-10-06T12:30:00", "2026-10-06T13:15:00")


def test_find_events_matches_attendee_email_and_all_words():
    events = [parse_event(r, NY) for r in (INTERVIEW, SALES)]
    assert [e.subject for e in find_events(events, "jordan")] == ["Interview SDR Role"]
    assert [e.subject for e in find_events(events, "interview gabe")] == ["Interview SDR Role"]
    assert find_events(events, "interview bob") == []
    assert find_events(events, "") == []


def test_find_events_tool_returns_detail_without_teams_boilerplate():
    class Source:
        def calendar_view(self, start, end):
            return [INTERVIEW, SALES]

    content, is_error = ToolRegistry(calendar_tools(Source())).run(
        ToolCall("t", "find_events", {"about": "Jordan", "start_date": "2026-10-01", "end_date": "2026-10-10"}))
    [event] = json.loads(content)["events"]
    assert not is_error
    assert event["description"] == "Interview discussion regarding the sales opportunity."
    assert {"name": "Sam", "email": "gabe@tag.example"} in event["attendees"]
    assert event["start"] == "Tue Oct 6 3:00 PM"


# ── documents ────────────────────────────────────────────────────────────────

def docx_bytes(paragraphs):
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", f'<w:document xmlns:w="x"><w:body>{body}</w:body></w:document>')
    return buf.getvalue()


def test_extracts_word_html_and_text():
    assert extract_text(docx_bytes(["Jordan Rivera", "SDR, 1 year &amp; counting"]), "resume.docx") == \
        "Jordan Rivera\nSDR, 1 year & counting"
    assert extract_text(b"<p>Hello<br>there</p><script>x()</script>", "a.html") == "Hello\nthere"
    assert extract_text(b"plain notes", "", "text/plain") == "plain notes"


def test_unreadable_files_say_so_instead_of_guessing():
    with pytest.raises(UnreadableDocument, match="Can't read"):
        extract_text(b"\x89PNG", "photo.png", "image/png")
    with pytest.raises(UnreadableDocument):
        extract_text(b"not a pdf", "resume.pdf")


def test_read_attachment_accepts_id_or_file_name():
    class Graph:
        mailbox = "dave@tag.example"

        def __init__(self):
            self.paths = []

        def get_all(self, path, params=None, limit=500, headers=None):
            return [{"id": "a1", "name": "JORDAN Resume.docx", "contentType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}]

        def get_bytes(self, path):
            self.paths.append(path)
            return docx_bytes(["Jordan Rivera"])

    graph = Graph()
    assert read_attachment(graph, "m1", "a1") == {"name": "JORDAN Resume.docx", "text": "Jordan Rivera"}
    assert read_attachment(graph, "m1", "jordan resume.docx")["text"] == "Jordan Rivera"
    assert graph.paths[-1] == "/users/dave@tag.example/messages/m1/attachments/a1/$value"
    with pytest.raises(ValueError, match="Attachments: JORDAN Resume.docx"):
        read_attachment(graph, "m1", "other.pdf")
