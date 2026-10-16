from datetime import date

from agent import notes as N
from llm.provider import LLMResponse
from store.db import Store

VTT = """WEBVTT

00:00:01.000 --> 00:00:04.000
<v Dave Vener>Thanks for coming in, Bob.</v>

00:00:04.500 --> 00:00:07.000
<v Dave Vener>Let's go over the proposal.</v>

00:00:08.000 --> 00:00:12.000
<v Bob Smith>We'd like pricing for 42 seats &amp; onboarding.</v>
"""

TRANSCRIPT = N.number_lines([
    ("Dave Vener", "Thanks for coming in, Bob. Let's go over the proposal.", 1),
    ("Bob Smith", "We'd like pricing for 42 seats and onboarding by Friday.", 8),
    ("Dave Vener", "I'll send the revised proposal by Friday.", 20),
    ("Bob Smith", "And we still need to decide on the phone system.", 31),
])
PEOPLE = [{"name": "Dave Vener", "email": "dvener@tag.example"}, {"name": "Bob Smith", "email": "bob@client.example"}]


def tool(name, **args):
    return LLMResponse(content=[{"type": "tool_use", "id": "t", "name": name, "input": args}], stop_reason="tool_use")


class LLM:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.seen = []

    def complete(self, messages, **kwargs):
        self.seen.append(messages[0]["content"])
        return self.responses.pop(0)


def test_transcripts_from_teams_azure_and_pasted_text_become_numbered_lines():
    assert N.number_lines(N.from_vtt(VTT)) == (
        "[L1] 00:01 Dave Vener: Thanks for coming in, Bob. Let's go over the proposal.\n"
        "[L2] 00:08 Bob Smith: We'd like pricing for 42 seats & onboarding.")
    azure = {"phrases": [{"speaker": 1, "offsetMilliseconds": 0, "text": "Hello."},
                         {"speaker": 1, "offsetMilliseconds": 900, "text": "Shall we start?"},
                         {"speaker": 2, "offsetMilliseconds": 3000, "text": "Yes."}]}
    assert N.number_lines(N.from_azure(azure)) == "[L1] 00:00 Speaker 1: Hello. Shall we start?\n[L2] 00:03 Speaker 2: Yes."
    assert N.number_lines(N.from_plain("Dave: Hi Bob.\nBob: Hi.\n\nWe agreed on 42 seats.")) == \
        "[L1] Dave: Hi Bob.\n[L2] Bob: Hi.\n[L3] We agreed on 42 seats."


def test_the_digest_keeps_only_points_that_cite_real_lines_and_dates_are_worked_out_in_code():
    digest = {
        "headline": "Bob wants a revised proposal — pricing for 42 seats.",
        "summary": ["Bob asked for pricing for 42 seats and onboarding [L2].",
                    "They talked about Bob's budget.",                    # cites nothing: dropped
                    "Bob mentioned a merger [L9]."],                       # no line 9: dropped
        "decisions": [],
        "action_items": [{"what": "Send the revised proposal [L3]", "owner": "Dave", "due": "by Friday"},
                         {"what": "Decide on the phone system [L4]", "owner": "Bob", "due": ""}],
        "open_questions": ["Which phone system? [L4]"],
    }
    out = N.checked(digest, TRANSCRIPT, date(2026, 10, 14), PEOPLE)   # a Wednesday
    assert out["headline"] == "Bob wants a revised proposal, pricing for 42 seats."
    assert out["summary"] == ["Bob asked for pricing for 42 seats and onboarding [L2]."]
    assert out["dropped"] == 2
    send, decide = out["action_items"]
    assert (send["owner"], send["due"], send["dave"]) == ("Dave Vener", "2026-10-16", True)
    assert (decide["owner"], decide["due"], decide["dave"]) == ("Bob Smith", None, False)


def test_a_recording_waits_for_the_speech_key_then_is_transcribed_and_digested():
    store = Store(":memory:")
    nid = N.new_meeting_note(store, title="Recording", source="recording", audio_path="x.webm",
                             event={"id": "ev1", "subject": "Bob/Dave - proposal", "attendees": PEOPLE},
                             started_at="2026-10-14T10:00")
    assert N.process(store, LLM(), speech=None) == 0
    note = store.get_note(nid)
    assert note["status"] == "waiting_transcript" and note["error"] == "waiting for the speech-to-text key"
    assert note["title"] == "Recording" and note["event_id"] == "ev1"

    class Speech:
        def transcribe(self, path):
            return {"phrases": [{"speaker": 1, "offsetMilliseconds": 0, "text": "I'll send the proposal by Friday."}]}

    llm = LLM(tool("write_meeting_notes", title="Proposal follow-up with Bob", headline="Dave owes Bob a proposal [L1].",
                   summary=["Dave will send the proposal by Friday [L1]."], decisions=[], open_questions=[],
                   action_items=[{"what": "Send the proposal [L1]", "owner": "Speaker 1", "due": "by Friday"}]))
    assert N.process(store, llm, speech=Speech()) == 1
    note = store.get_note(nid)
    assert note["status"] == "ready" and note["transcript"].startswith("[L1] 00:00 Speaker 1:")
    assert note["title"] == "Proposal follow-up with Bob"        # "Recording" replaced by the model's title
    assert note["digest"]["action_items"][0]["due"] == "2026-10-16"
    assert "Attendees: Dave Vener, Bob Smith" in llm.seen[0]


def test_a_failure_is_kept_on_the_note():
    store = Store(":memory:")
    nid = N.new_meeting_note(store, title="Pasted", source="pasted", transcript=TRANSCRIPT)
    N.process(store, LLM(LLMResponse(content=[{"type": "text", "text": "no tool"}], stop_reason="end_turn")))
    note = store.get_note(nid)
    assert note["status"] == "failed" and "did not return notes" in note["error"]


def test_quick_notes_and_search_and_the_brief_section():
    store = Store(":memory:")
    save, find = N.notes_tools(store)
    saved = save.handler({"text": "Bob wants the  proposal by Friday", "about": ["Bob Smith"]})
    assert saved["saved"] and saved["text"] == "Bob wants the proposal by Friday"
    nid = N.new_meeting_note(store, title="Bob/Dave", source="pasted", transcript=TRANSCRIPT,
                             event={"id": "ev2", "subject": "Bob/Dave", "attendees": PEOPLE}, started_at="2026-10-14T10:00")
    store.update_note(nid, status="ready", digest={"headline": "Proposal", "summary": ["Pricing for 42 seats [L2]."],
                                                   "decisions": [], "open_questions": [],
                                                   "action_items": [{"what": "Send the proposal [L3]", "due": "2026-10-16"}]})
    found = find.handler({"query": "bob"})["notes"]
    assert {n["kind"] for n in found} == {"quick", "meeting"}
    assert find.handler({"query": "42 seats"})["notes"][0]["title"] == "Bob/Dave"
    [earlier] = N.earlier_notes(store, ["BOB@client.example"])
    assert earlier["label"] == "Notes 1" and earlier["points"] == ["Pricing for 42 seats."]
    assert earlier["open"] == ["Send the proposal (due 2026-10-16)"]
    assert N.earlier_notes(store, ["someone@else.example"]) == []


def test_the_notes_api(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    monkeypatch.setenv("REQUIRE_AUTH", "False")
    import app.main as main
    monkeypatch.setattr(main, "RECORDINGS", str(tmp_path))
    store = Store(":memory:")
    kicked = []

    class Graph:
        mailbox = "dvener@tag.example"

        def get(self, path, params=None, headers=None):
            raise RuntimeError("no calendar in this test")

    main.app.state.services = SimpleNamespace(store=store, graph=Graph(),
                                              worker=SimpleNamespace(process_notes_now=lambda: kicked.append(1)))
    try:
        http = TestClient(main.app)
        pasted = http.post("/api/notes", json={"text": "Dave: I'll send it Friday.\nBob: Great."}).json()
        assert pasted["status"] == "summarizing" and pasted["title"] == "Meeting notes" and kicked
        full = http.get(f"/api/notes/{pasted['id']}").json()
        assert full["transcript"] == "[L1] Dave: I'll send it Friday.\n[L2] Bob: Great."
        assert http.post("/api/notes", json={"text": "   "}).status_code == 400

        rec = http.post("/api/recordings", files={"file": ("memo.m4a", b"\x00\x01audio", "audio/x-m4a")},
                        data={"event_id": ""}).json()
        assert rec["status"] == "waiting_transcript" and rec["source"] == "recording"
        assert list(tmp_path.iterdir())[0].suffix == ".m4a"
        assert http.post("/api/recordings", files={"file": ("notes.pdf", b"%PDF", "application/pdf")}).status_code == 400
        assert http.post("/api/recordings", files={"file": ("empty.webm", b"", "audio/webm")}).status_code == 400

        store.update_note(rec["id"], status="failed", error="boom")
        assert http.post(f"/api/notes/{rec['id']}/retry").json()["status"] == "waiting_transcript"
        assert {n["id"] for n in http.get("/api/notes").json()["notes"]} == {pasted["id"], rec["id"]}
        assert http.get("/api/notes/nope").status_code == 404
    finally:
        main.app.state.services = None


def test_a_conversation_about_a_note_starts_with_it_in_view():
    from agent.assistant import Assistant
    store = Store(":memory:")
    nid = N.new_meeting_note(store, title="Bob/Dave", source="pasted", transcript=TRANSCRIPT)
    section = Assistant(None, None, store, None)._topic_section({"note": nid})
    assert "one meeting's notes" in section and "[L3] 00:20 Dave Vener: I'll send the revised proposal by Friday." in section
