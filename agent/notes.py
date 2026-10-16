"""Meeting notes: a transcript in, a digest Dave can use out.

Where transcripts come from:
- Teams meetings (Graph transcripts; needs the tenant's "Transcript API access" and an app
  permission limited to Dave, see fetch_teams_transcripts);
- recordings from Dave's phone, uploaded through the app and transcribed by Azure Speech;
- a transcript or notes pasted in.
And quick notes: a line Dave asks the assistant to keep ("Bob wants the proposal Friday").

Code numbers the transcript's lines and does the arithmetic (due dates from "by Friday",
who owns what); the model writes the digest and must cite a line for everything it says,
the way briefs cite their emails, so every point can be checked against what was said.
"""

from __future__ import annotations

import html
import logging
import re
from datetime import date, datetime
from typing import Any

from agent.briefs import plain_style
from agent.calendar import local_zone
from agent.tools import Tool
from agent.when import resolve as resolve_when
from llm.provider import ToolSpec

log = logging.getLogger("assistant.notes")

MAX_TRANSCRIPT_CHARS = 150_000      # about two and a half hours of talk


# ── transcripts: whatever the source, numbered lines "[L12] 14:03 Speaker: text" ──

def number_lines(entries: list[tuple[str, str, float | None]]) -> str:
    """(speaker, text, seconds from the start) -> the numbered transcript the digest cites."""
    out = []
    for n, (speaker, text, seconds) in enumerate(entries, 1):
        stamp = f"{int(seconds // 60):02d}:{int(seconds % 60):02d} " if seconds is not None else ""
        who = f"{speaker}: " if speaker else ""
        out.append(f"[L{n}] {stamp}{who}{' '.join(text.split())}")
    return "\n".join(out)


def _seconds(stamp: str) -> float:
    h, m, s = (["0"] + stamp.split(":"))[-3:]
    return int(h) * 3600 + int(m) * 60 + float(s)


def from_vtt(vtt: str) -> list[tuple[str, str, float | None]]:
    """Teams' WebVTT: cues of '<v Name>words</v>', merged while the same person keeps talking."""
    entries: list[tuple[str, str, float | None]] = []
    for block in re.split(r"\n\s*\n", vtt.replace("\r", "")):
        timing = re.search(r"(\d{1,2}:\d{2}:\d{2}(?:\.\d+)?|\d{2}:\d{2}(?:\.\d+)?)\s*-->", block)
        if not timing:
            continue
        body = block[timing.end():].split("\n", 1)[-1].strip()
        voice = re.match(r"<v\s+([^>]+)>(.*?)(?:</v>)?$", body, re.S)
        speaker, text = (voice.group(1).strip(), voice.group(2)) if voice else ("", body)
        text = html.unescape(re.sub(r"<[^>]+>", "", text)).strip()
        if not text:
            continue
        if entries and entries[-1][0] == speaker:
            entries[-1] = (speaker, entries[-1][1] + " " + text, entries[-1][2])
        else:
            entries.append((speaker, text, _seconds(timing.group(1))))
    return entries


def from_azure(result: dict[str, Any]) -> list[tuple[str, str, float | None]]:
    """Azure Speech fast transcription: phrases with a speaker number and an offset."""
    entries: list[tuple[str, str, float | None]] = []
    for p in result.get("phrases") or []:
        speaker = f"Speaker {p['speaker']}" if p.get("speaker") is not None else ""
        text, at = p.get("text", ""), (p.get("offsetMilliseconds") or 0) / 1000
        if entries and entries[-1][0] == speaker and speaker:
            entries[-1] = (speaker, entries[-1][1] + " " + text, entries[-1][2])
        elif text.strip():
            entries.append((speaker, text, at))
    if not entries:
        text = " ".join(p.get("text", "") for p in result.get("combinedPhrases") or [])
        entries = [("", text, None)] if text.strip() else []
    return entries


def from_plain(text: str) -> list[tuple[str, str, float | None]]:
    """Pasted notes or a transcript: one entry per paragraph or 'Name: words' line."""
    entries = []
    for chunk in re.split(r"\n\s*\n|\n(?=[A-Z][\w .'-]{1,30}:\s)", text.strip()):
        chunk = chunk.strip()
        if not chunk:
            continue
        m = re.match(r"([A-Z][\w .'-]{1,30}):\s+(.*)", chunk, re.S)
        entries.append((m.group(1).strip(), m.group(2), None) if m else ("", chunk, None))
    return entries


# ── the digest ──

WRITE_DIGEST = ToolSpec(
    name="write_meeting_notes",
    description="Record the notes for this meeting. Leave any list empty rather than pad it.",
    input_schema={"type": "object", "properties": {
        "title": {"type": "string", "description": "A short title for the meeting, if the one given is missing or generic"},
        "headline": {"type": "string", "description": "One plain sentence: what the meeting was about and where it landed"},
        "summary": {"type": "array", "items": {"type": "string"},
                    "description": "3-7 points: what was discussed, each ending with its line citations like [L12] or [L12, L40]"},
        "decisions": {"type": "array", "items": {"type": "string"}, "description": "Things agreed, each cited"},
        "action_items": {"type": "array", "items": {"type": "object", "properties": {
            "what": {"type": "string", "description": "The task, starting with a verb, ending with its citation"},
            "owner": {"type": "string", "description": "Who will do it, as named in the transcript ('Dave', 'Bob'); empty if unclear"},
            "due": {"type": "string", "description": "When, quoted as said ('by Friday', 'next week', 'Oct 30'); empty if none"},
        }, "required": ["what"]}},
        "open_questions": {"type": "array", "items": {"type": "string"}, "description": "Left unresolved, each cited"},
    }, "required": ["headline", "summary", "decisions", "action_items", "open_questions"]},
)

DIGEST_PROMPT = """You write meeting notes for Dave, CEO of TAG Solutions (an IT managed services provider in
Albany, NY), from a meeting transcript. He reads them later to remember what happened and what he owes.

- Use ONLY the transcript. Never add facts, names, numbers or dates that weren't said.
- Every point ends with the line(s) it comes from, e.g. "[L12]" or "[L12, L40]".
- Action items: only things someone actually said they or someone else would do. Quote the due phrase as
  said ("by Friday"); leave due empty when no time was given. Owner as named; "Dave" when it's him.
- Speakers may be labelled "Speaker 1", "Speaker 2": keep those labels; don't guess who they are.
- Plain sentences, at most 25 words each, the fact first. No em or en dashes. No filler."""

_CITE = re.compile(r"\[L\d+(?:\s*[-,]\s*L?\d+)*\]")


def _cited_lines(text: str) -> list[int]:
    lines = []
    for group in _CITE.findall(text):
        nums = [int(n) for n in re.findall(r"\d+", group)]
        if "-" in group and len(nums) == 2:
            lines += list(range(nums[0], nums[1] + 1))
        else:
            lines += nums
    return lines


def write_digest(llm: Any, title: str, transcript: str, people: list[dict[str, str]], when: str) -> dict[str, Any]:
    attendees = ", ".join(p.get("name") or p.get("email", "") for p in people) or "not known"
    response = llm.complete([{"role": "user", "content": f"Meeting: {title}\nWhen: {when}\nAttendees: {attendees}\n\n"
                                                         f"Transcript:\n{transcript[:MAX_TRANSCRIPT_CHARS]}"}],
                            system=DIGEST_PROMPT, tools=[WRITE_DIGEST], tool_choice=WRITE_DIGEST.name, max_tokens=3000)
    if not response.tool_calls:
        raise RuntimeError("The model did not return notes.")
    return response.tool_calls[0].input


def checked(digest: dict[str, Any], transcript: str, started: date, people: list[dict[str, str]]) -> dict[str, Any]:
    """Code's pass over the model's digest: citations must point at real lines (a point citing
    nothing, or a line that doesn't exist, is dropped); due dates are worked out from the phrase
    and the meeting's day; owners are matched to the people who were there."""
    last_line = transcript.count("\n") + 1 if transcript else 0
    names = {(p.get("name") or "").split(" ")[0].lower(): p.get("name") for p in people if p.get("name")}

    def grounded(text: str) -> str | None:
        lines = _cited_lines(text)
        if not lines or any(n < 1 or n > last_line for n in lines):
            return None
        return plain_style(text)

    out: dict[str, Any] = {"headline": plain_style(digest.get("headline") or ""), "dropped": 0}
    for key in ("summary", "decisions", "open_questions"):
        kept = [grounded(t) for t in digest.get(key) or []]
        out["dropped"] += kept.count(None)
        out[key] = [t for t in kept if t]
    items = []
    for item in digest.get("action_items") or []:
        what = grounded(item.get("what") or "")
        if not what:
            out["dropped"] += 1
            continue
        owner = (item.get("owner") or "").strip()
        owner = names.get(owner.split(" ")[0].lower(), owner)
        due_text = (item.get("due") or "").strip()
        due = resolve_when(due_text, started).get("latest_date") if due_text else None
        items.append({"what": what, "owner": owner, "due_text": due_text, "due": due,
                      "dave": owner.lower().startswith("dave")})
    out["action_items"] = items
    if digest.get("title"):
        out["title"] = plain_style(digest["title"])
    return out


def transcript_lines(transcript: str, numbers: list[int]) -> list[str]:
    """The cited lines themselves, for showing a point's source."""
    lines = transcript.split("\n")
    return [lines[n - 1] for n in numbers if 0 < n <= len(lines)]


# ── the work: recordings to transcripts, transcripts to digests ──

def process(store: Any, llm: Any, speech: Any = None, limit: int = 3) -> int:
    """Move waiting notes along: transcribe recordings (when there's a speech key), then digest.
    Returns how many notes changed. Safe to call every worker cycle."""
    done = 0
    for note in store.notes_waiting(("waiting_transcript", "summarizing"))[:limit]:
        try:
            if note["status"] == "waiting_transcript":
                if speech is None:
                    if note.get("error") != "waiting for the speech-to-text key":
                        store.update_note(note["id"], error="waiting for the speech-to-text key")
                    continue
                store.update_note(note["id"], status="transcribing", error=None)
                result = speech.transcribe(note["audio_path"])
                transcript = number_lines(from_azure(result))
                if not transcript:
                    store.update_note(note["id"], status="failed", error="No speech was found in the recording.")
                    continue
                store.update_note(note["id"], transcript=transcript, status="summarizing")
                note = store.get_note(note["id"])
            summarize(store, llm, note)
            done += 1
        except Exception as e:  # noqa: BLE001 - recorded on the note; Dave can retry from the app
            log.exception("note %s failed", note["id"])
            store.update_note(note["id"], status="failed", error=f"{type(e).__name__}: {e}"[:400])
    return done


def summarize(store: Any, llm: Any, note: dict[str, Any]) -> dict[str, Any]:
    started = datetime.fromisoformat(note["started_at"]) if note.get("started_at") else datetime.now(local_zone())
    when = started.astimezone(local_zone()).strftime("%A %B %d %Y, %I:%M %p")
    digest = checked(write_digest(llm, note["title"], note["transcript"] or "", note.get("people") or [], when),
                     note["transcript"] or "", started.date(), note.get("people") or [])
    fields: dict[str, Any] = {"digest": digest, "status": "ready", "error": None}
    if digest.get("title") and note["title"].lower() in ("", "recording", "meeting", "notes"):
        fields["title"] = digest["title"]
    store.update_note(note["id"], **fields)
    return digest


def new_meeting_note(store: Any, *, title: str, source: str, transcript: str = "", audio_path: str | None = None,
                     event: dict[str, Any] | None = None, started_at: str | None = None) -> str:
    """A note for one meeting: from a transcript (digested next) or a recording (transcribed first)."""
    people = (event or {}).get("attendees") or []
    return store.add_note(
        kind="meeting", title=title or (event or {}).get("subject") or "Meeting", source=source,
        event_id=(event or {}).get("id"), started_at=started_at or datetime.now(local_zone()).isoformat(timespec="minutes"),
        people=people, transcript=transcript or None, audio_path=audio_path,
        status="summarizing" if transcript else "waiting_transcript")


# ── for briefs: what was said last time with these people ──

def _uncited(text: str) -> str:
    """'Pricing for 42 seats [L2].' -> 'Pricing for 42 seats.'"""
    return re.sub(r"\s+([.,;:!?])", r"\1", _CITE.sub("", text)).strip()


def earlier_notes(store: Any, emails: list[str], first_label: int = 1, limit: int = 3) -> list[dict[str, Any]]:
    out = []
    for i, note in enumerate(store.notes_with_people(emails, limit=limit)):
        d = note.get("digest") or {}
        out.append({"label": f"Notes {first_label + i}", "id": note["id"], "title": note["title"],
                    "when": (note.get("started_at") or "")[:10], "headline": d.get("headline", ""),
                    "points": [_uncited(t) for t in (d.get("summary") or []) + (d.get("decisions") or [])][:8],
                    "open": [_uncited(a["what"]) + (f" (due {a['due']})" if a.get("due") else "")
                             for a in d.get("action_items") or []][:6]})
    return out


# ── chat tools: keep a quick note, find notes ──

def note_view(note: dict[str, Any], full: bool = False) -> dict[str, Any]:
    d = note.get("digest") or {}
    view = {"id": note["id"], "kind": note["kind"], "title": note["title"], "when": note.get("started_at"),
            "status": note["status"], "people": [p.get("name") or p.get("email") for p in note.get("people") or []]}
    if note["kind"] == "quick":
        view["text"] = d.get("text", "")
    else:
        view.update(headline=d.get("headline", ""), decisions=d.get("decisions", []),
                    action_items=d.get("action_items", []))
        if full:
            view.update(summary=d.get("summary", []), open_questions=d.get("open_questions", []))
    return view


def notes_tools(store: Any) -> list[Tool]:
    def save_note(args: dict[str, Any]) -> dict[str, Any]:
        text = " ".join((args.get("text") or "").split())
        if not text:
            return {"error": "Nothing to save."}
        people = [{"name": n} for n in args.get("about") or [] if n]
        nid = store.add_note(kind="quick", title=args.get("title") or text[:60], source="typed", people=people,
                             started_at=datetime.now(local_zone()).isoformat(timespec="minutes"),
                             digest={"text": text}, status="ready")
        return {"saved": True, "id": nid, "text": text, "about": [p["name"] for p in people]}

    def find_notes(args: dict[str, Any]) -> dict[str, Any]:
        found = store.list_notes(query=args.get("query") or "", limit=int(args.get("limit") or 8))
        full = len(found) <= 2
        return {"notes": [note_view(n, full=full) for n in found],
                "note": "Meeting notes cite transcript lines like [L12]; quote the points, not the labels."}

    return [
        Tool(ToolSpec("save_note", "Keep a note Dave asks you to remember: a fact, a promise, something someone said. "
                      "Attach the people it's about. Not for preferences or nicknames (use remember for those).",
                      {"type": "object", "properties": {
                          "text": {"type": "string", "description": "The note, in Dave's words, complete enough to read later"},
                          "about": {"type": "array", "items": {"type": "string"}, "description": "People or companies it's about"},
                          "title": {"type": "string"},
                      }, "required": ["text"]}), handler=save_note),
        Tool(ToolSpec("find_notes", "Search Dave's meeting notes and saved notes: what was discussed, decided or promised. "
                      "Use for 'what did we agree with Bob', 'what did I say I'd do after the Equinox meeting'.",
                      {"type": "object", "properties": {
                          "query": {"type": "string", "description": "A name, company or topic; empty for the latest notes"},
                          "limit": {"type": "integer"},
                      }}), handler=find_notes),
    ]


# ── Teams transcripts (once TAG's tenant allows it) ──

def fetch_teams_transcripts(app_graph: Any, store: Any, mailbox: str, events: list[Any]) -> int:
    """For Dave's Teams meetings that have ended, fetch any transcript not yet made into a note.

    Needs, from TAG's admin: the tenant's Teams "Transcript API access" turned on, and the app's
    application permissions OnlineMeetings.Read.All + OnlineMeetingTranscript.Read.All with an
    application access policy limited to Dave. `app_graph` is that app-only client."""
    added = 0
    user_id = app_graph.get(f"/users/{mailbox}", {"$select": "id"})["id"]
    for event in events:
        if not event.join_url or store.note_for_event(event.id):
            continue
        meetings = app_graph.get(f"/users/{user_id}/onlineMeetings",
                                 {"$filter": f"JoinWebUrl eq '{event.join_url}'"}).get("value", [])
        if not meetings:
            continue
        transcripts = app_graph.get(f"/users/{user_id}/onlineMeetings/{meetings[0]['id']}/transcripts").get("value", [])
        if not transcripts:
            continue
        vtt = app_graph.get_text(f"/users/{user_id}/onlineMeetings/{meetings[0]['id']}/transcripts/"
                                 f"{transcripts[-1]['id']}/content", {"$format": "text/vtt"})
        transcript = number_lines(from_vtt(vtt))
        if transcript:
            from agent.calendar import event_detail
            new_meeting_note(store, title=event.subject, source="teams", transcript=transcript,
                             event=event_detail(event), started_at=event.start.isoformat(timespec="minutes"))
            added += 1
    return added

