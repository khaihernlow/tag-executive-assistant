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

        # moving notes: a meeting the calendar can't find is refused; "not a calendar meeting" is fine
        assert http.patch(f"/api/notes/{pasted['id']}", json={"event_id": "ev9"}).status_code == 400
        moved = http.patch(f"/api/notes/{pasted['id']}", json={"event_id": None}).json()
        assert moved["event_id"] is None and moved["parts"] == [{"n": 1, "source": "pasted", "status": "done", "error": None}]
    finally:
        main.app.state.services = None


def test_a_conversation_about_a_note_starts_with_it_in_view():
    from agent.assistant import Assistant
    store = Store(":memory:")
    nid = N.new_meeting_note(store, title="Bob/Dave", source="pasted", transcript=TRANSCRIPT)
    section = Assistant(None, None, store, None)._topic_section({"note": nid})
    assert "one meeting's notes" in section and "[L3] 00:20 Dave Vener: I'll send the revised proposal by Friday." in section


class Speech:
    def __init__(self, **texts):
        self.texts = texts

    def transcribe(self, path):
        return {"phrases": [{"speaker": 1, "offsetMilliseconds": 0, "text": self.texts[path]}]}


def notes_for(*lines):
    return tool("write_meeting_notes", headline="Proposal [L2].", summary=[f"Point [L{n}]." for n in lines],
                decisions=[], open_questions=[], action_items=[])


def test_a_second_recording_of_the_same_meeting_becomes_part_two_of_one_set_of_notes():
    store = Store(":memory:")
    event = {"id": "ev1", "subject": "Bob/Dave", "attendees": PEOPLE}
    first = N.new_meeting_note(store, title="Bob/Dave", source="recording", audio_path="a.webm", event=event,
                               started_at="2026-10-14T10:00")
    N.process(store, LLM(notes_for(1)), speech=Speech(**{"a.webm": "We'd like pricing for 42 seats."}))
    assert store.get_note(first)["status"] == "ready"

    second = N.new_meeting_note(store, title="Bob/Dave", source="recording", audio_path="b.webm", event=event)
    assert second == first                                   # the same notes, not a second set
    note = store.get_note(first)
    assert note["status"] == "waiting_transcript" and note["digest"]["headline"]   # part 1's notes still there
    llm = LLM(notes_for(2, 4))
    N.process(store, llm, speech=Speech(**{"b.webm": "I'll send the proposal by Friday."}))
    note = store.get_note(first)
    assert note["status"] == "ready" and [p["n"] for p in note["parts"]] == [1, 2]
    lines = note["transcript"].split("\n")
    assert lines[0].startswith("[L1] Part 1: recording") and lines[1] == "[L2] 00:00 Speaker 1: We'd like pricing for 42 seats."
    assert lines[2].startswith("[L3] Part 2: recording") and lines[3] == "[L4] 00:00 Speaker 1: I'll send the proposal by Friday."
    assert "Part 2: recording" in llm.seen[0]                # written over the whole meeting
    assert store.list_notes() and len(store.list_notes()) == 1


def test_pasted_notes_join_the_meeting_and_a_failed_part_does_not_sink_the_rest():
    store = Store(":memory:")
    event = {"id": "ev1", "subject": "Bob/Dave", "attendees": PEOPLE}
    nid = N.new_meeting_note(store, title="Bob/Dave", source="recording", audio_path="a.webm", event=event)
    N.new_meeting_note(store, title="Bob/Dave", source="pasted", transcript=N.number_lines(N.from_plain("Bob: Phones next.")),
                       event=event)

    class Broken:
        def transcribe(self, path):
            raise RuntimeError("speech service down")

    N.process(store, LLM(notes_for(1)), speech=Broken())
    note = store.get_note(nid)
    assert note["status"] == "ready" and note["transcript"] == "[L1] Bob: Phones next."
    assert [(p["n"], p["status"]) for p in note["parts"]] == [(1, "failed"), (2, "done")]
    N.retry(store, nid)                                      # the failed recording is tried again
    assert store.get_note(nid)["status"] == "waiting_transcript"


def test_a_part_that_arrives_while_the_notes_are_being_written_is_not_lost():
    store = Store(":memory:")
    event = {"id": "ev1", "subject": "Bob/Dave", "attendees": PEOPLE}
    nid = N.new_meeting_note(store, title="Bob/Dave", source="pasted", transcript=TRANSCRIPT, event=event)

    class Slow(LLM):
        def complete(self, messages, **kwargs):
            N.new_meeting_note(store, title="Bob/Dave", source="recording", audio_path="b.webm", event=event)
            return super().complete(messages, **kwargs)

    N.process(store, Slow(notes_for(2)))
    note = store.get_note(nid)
    assert note["digest"]["summary"] and note["status"] == "waiting_transcript"   # notes kept, part 2 still to do


def test_notes_moved_to_the_right_meeting():
    store = Store(":memory:")
    wrong = {"id": "ev1", "subject": "Lawn Sprinklers", "attendees": [PEOPLE[0]]}
    right = {"id": "ev2", "subject": "Bob/Dave", "attendees": PEOPLE}
    nid = N.new_meeting_note(store, title="Lawn Sprinklers", source="pasted", transcript=TRANSCRIPT, event=wrong)
    store.update_note(nid, status="ready", digest={"headline": "x"})
    assert N.move_note(store, nid, right, started_at="2026-10-14T14:00") == nid
    note = store.get_note(nid)
    assert (note["event_id"], note["title"], note["started_at"]) == ("ev2", "Bob/Dave", "2026-10-14T14:00")
    assert note["people"] == PEOPLE and note["status"] == "summarizing"      # rewritten with the right people

    # moved onto a meeting that already has notes: the two become one
    other = N.new_meeting_note(store, title="Lawn Sprinklers", source="recording", audio_path="c.webm", event=wrong)
    assert N.move_note(store, other, right) == nid and store.get_note(other) is None
    assert [(p["n"], p["source"]) for p in store.get_note(nid)["parts"]] == [(1, "pasted"), (2, "recording")]

    N.move_note(store, nid, None)                            # not a calendar meeting after all
    assert store.get_note(nid)["event_id"] is None and store.get_note(nid)["people"] == []


def test_misheard_names_are_fixed_only_toward_known_names():
    transcript = ("[L1] 00:00 Speaker 1: The phone system for Pecanic Landing.\n"
                  "[L2] 00:08 Speaker 2: Check whether their middle licenses can be reused.")
    names = ["Peconic Landing", "Mitel", "Lisa Quinn"]
    fixes = N.corrections([{"heard": "Pecanic Landing", "meant": "Peconic Landing"},
                           {"heard": "middle licenses", "meant": "Mitel licenses"},
                           {"heard": "phone system", "meant": "PBX"},                 # not a name on the list: refused
                           {"heard": "Pinecone", "meant": "Peconic"}],                # not in the transcript: refused
                          transcript, names)
    assert fixes == [("Pecanic Landing", "Peconic Landing"), ("middle licenses", "Mitel licenses")]
    assert N.apply_fixes(transcript, fixes).splitlines()[1] == "[L2] 00:08 Speaker 2: Check whether their Mitel licenses can be reused."


def test_the_write_up_fixes_names_in_the_notes_the_transcript_and_the_parts():
    store = Store(":memory:")
    transcript = "[L1] 00:00 Speaker 1: Check whether their middle licenses can be reused by Friday."
    nid = N.new_meeting_note(store, title="Phones", source="pasted", transcript=transcript, started_at="2026-10-14T10:00")
    llm = LLM(tool("write_meeting_notes", headline="Licensing [L1].", summary=["Reuse the middle licenses [L1]."],
                   decisions=[], open_questions=[], action_items=[{"what": "Check the middle licenses [L1]", "owner": "Speaker 1"}],
                   misheard=[{"heard": "middle licenses", "meant": "Mitel licenses"}]))
    N.process(store, llm, vocabulary=lambda note: ["Mitel", "Peconic Landing"])
    note = store.get_note(nid)
    assert "Expected names (spell them this way): Mitel; Peconic Landing" in llm.seen[0]
    assert "Mitel licenses" in note["transcript"] and "middle" not in note["transcript"]
    assert note["parts"][0]["transcript"] == note["transcript"]
    assert note["digest"]["summary"] == ["Reuse the Mitel licenses [L1]."]
    assert note["digest"]["action_items"][0]["what"] == "Check the Mitel licenses [L1]"
    assert note["digest"]["fixed_names"] == [{"heard": "middle licenses", "meant": "Mitel licenses"}]


def test_speech_gets_the_names_and_retries_without_them_if_azure_refuses():
    from connectors.azure_speech import AzureSpeech

    class Session:
        def __init__(self):
            self.definitions = []

        def post(self, url, headers=None, files=None, timeout=None):
            import json
            from types import SimpleNamespace
            d = json.loads(files["definition"][1])
            self.definitions.append(d)
            ok = "phraseList" not in d
            return SimpleNamespace(ok=ok, status_code=200 if ok else 400, text="Invalid JSON format.",
                                   json=lambda: {"phrases": []})

    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(b"RIFF")
    session = Session()
    AzureSpeech("k", "eastus", session=session).transcribe(f.name, ["Peconic Landing", "Mitel"])
    assert session.definitions[0]["phraseList"] == {"phrases": ["Peconic Landing", "Mitel"]}
    assert "phraseList" not in session.definitions[1]
