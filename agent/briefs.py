"""Meeting briefs: prepared ahead of time, opened from the itinerary.

Code decides which meetings get a brief and gathers the material (invite,
emails with the outside attendees and about the topic, relevant
attachments such as resumes). One model call writes the brief in a fixed
structure from that material only, then addresses are fact-checked.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from typing import Any

from agent.calendar import Event, event_detail, fmt_local, local_zone, parse_event
from agent.factcheck import known_addresses, verify_addresses
from agent.mail import LIST_FIELDS, read_attachment, read_email, search_mail
from agent.people import internal_domain
from llm.provider import ToolSpec

INTERVIEW = re.compile(r"\b(interview|candidate|applicant)\b", re.I)
WORTH_READING = re.compile(r"(resume|résumé|\bcv\b|proposal|rfp|quote|sow|agenda|profile|deck)", re.I)
STOPWORDS = {
    "meeting", "meet", "call", "discussion", "discuss", "sync", "review", "interview", "role", "with", "and",
    "the", "for", "re", "fw", "fwd", "tag", "solutions", "teams", "zoom", "invitation", "updated", "weekly",
    "monthly", "one", "on", "dave", "vener", "check", "in", "intro", "introduction", "follow", "up", "followup",
    "person", "in-person", "virtual", "onsite", "session", "touch", "base", "quick", "round",
}
WORK_HOURS = (7, 18)  # briefs are for business meetings, not evening plans
MAX_EMAILS = 6
EMAIL_CHARS = 2500
MAX_ATTACHMENTS = 2


# ── which meetings get a brief ───────────────────────────────────────────────

def external_attendees(event: Event, mailbox: str, domain: str) -> list[str]:
    people = set(event.attendee_emails) | ({event.organizer_email} if event.organizer_email else set())
    return sorted(a for a in people if a and a != mailbox.lower() and not (domain and a.endswith("@" + domain)))


def needs_brief(event: Event, mailbox: str, domain: str) -> bool:
    if event.all_day or event.response == "declined" or not event.attendee_emails:
        return False
    if not WORK_HOURS[0] <= event.start.hour < WORK_HOURS[1]:
        return False
    return bool(external_attendees(event, mailbox, domain)) or bool(INTERVIEW.search(event.subject))


def fingerprint(event: Event) -> str:
    parts = [event.subject, event.start.isoformat(), event.end.isoformat(), event.description,
             ",".join(sorted(event.attendee_emails))]
    return hashlib.sha1("|".join(parts).encode()).hexdigest()


def topic_words(subject: str) -> list[str]:
    words = re.findall(r"[A-Za-z][A-Za-z'&.-]{2,}", subject)
    return [w for w in words if w.lower().strip(".-'") not in STOPWORDS][:4]


# ── gathering ────────────────────────────────────────────────────────────────

def _participant_mail(graph: Any, address: str) -> list[dict[str, Any]]:
    return graph.get_all(f"/users/{graph.mailbox}/messages",
                         {"$search": f'"participants:{address}"', "$select": LIST_FIELDS, "$top": 10}, limit=10)


def gather(graph: Any, event: Event) -> dict[str, Any]:
    externals = external_attendees(event, graph.mailbox, internal_domain())
    found: dict[str, str] = {}  # message id -> received, newest first after sort
    for address in externals[:4]:
        for m in _participant_mail(graph, address):
            found.setdefault(m["id"], m.get("receivedDateTime", ""))
    words = topic_words(event.subject)
    if words:
        # All words only: generic subject words ("Person", "Engineer") would pull in noise,
        # and the attendee searches above already cover the broad side.
        for m in search_mail(graph, about=" ".join(words), since_days=120, limit=8, broaden=False):
            found.setdefault(m["id"], "")
    ids = sorted(found, key=lambda i: found[i], reverse=True)[: MAX_EMAILS * 2]

    emails, attachments = [], []
    for mid in ids:
        if len(emails) >= MAX_EMAILS:
            break
        try:
            email = read_email(graph, mid)
        except Exception:  # noqa: BLE001 - a vanished message just isn't a source
            continue
        label = f"Email {len(emails) + 1}"
        emails.append({
            "label": label,
            "from": email["from"],
            "subject": email["subject"],
            "received": email["received"],
            "body": email["body"][:EMAIL_CHARS],
        })
        for att in email.get("attachments", []):
            if len(attachments) < MAX_ATTACHMENTS and WORTH_READING.search(att.get("name") or "") \
                    and (att.get("size") or 0) < 5_000_000:
                try:
                    doc = read_attachment(graph, mid, att["id"])
                    attachments.append({"label": f"Attachment {len(attachments) + 1}", "name": doc["name"],
                                        "from_email": label, "text": doc["text"][:6000]})
                except Exception:  # noqa: BLE001 - unreadable files are skipped, not guessed
                    pass
    return {"event": event_detail(event), "external": externals, "emails": emails, "attachments": attachments}


# ── writing ──────────────────────────────────────────────────────────────────

WRITE_BRIEF = ToolSpec(
    name="write_brief",
    description="Record the meeting brief.",
    input_schema={
        "type": "object",
        "properties": {
            "headline": {"type": "string", "description": "One line: what this meeting is"},
            "who": {"type": "array", "items": {"type": "object", "properties": {
                "name": {"type": "string"}, "role": {"type": "string", "description": "role / company / relation"},
            }, "required": ["name", "role"]}},
            "context": {"type": "array", "items": {"type": "string"},
                        "description": "Why the meeting is happening and the history so far"},
            "background": {"type": "array", "items": {"type": "string"},
                           "description": "Facts about the person or company (e.g. from a resume)"},
            "prep": {"type": "array", "items": {"type": "string"},
                     "description": "2-4 suggested talking points or questions for Dave"},
            "gaps": {"type": "array", "items": {"type": "string"},
                     "description": "What the material didn't answer"},
            "sources": {"type": "array", "items": {"type": "string"},
                        "description": "Labels used, e.g. 'Email 2', 'Attachment 1'"},
        },
        "required": ["headline", "who", "context", "background", "prep", "gaps", "sources"],
    },
)

BRIEF_PROMPT = """You prepare short meeting briefs for Dave, CEO of TAG Solutions (an IT managed services provider).
Use ONLY the material provided: the invite, the emails and the attachments. Never invent facts,
titles, companies or history. When a fact comes from an email or attachment, end the bullet with
its label in brackets, e.g. "[Email 2]". If the material is thin, say so in gaps rather than padding.
Keep every bullet short; Dave reads this on his phone minutes before the meeting."""


def write_brief(llm: Any, gathered: dict[str, Any]) -> dict[str, Any]:
    material = json.dumps(gathered, ensure_ascii=False, default=str)
    response = llm.complete([{"role": "user", "content": f"Material for the brief:\n{material}"}],
                            system=BRIEF_PROMPT, tools=[WRITE_BRIEF], tool_choice=WRITE_BRIEF.name, max_tokens=2500)
    calls = response.tool_calls
    if not calls:
        raise RuntimeError("The model did not return a brief.")
    brief = calls[0].input

    # Same rule as chat replies: every address must come from the material.
    known = known_addresses(material)
    def check(value: Any) -> Any:
        if isinstance(value, str):
            return verify_addresses(value, known)[0]
        if isinstance(value, list):
            return [check(v) for v in value]
        if isinstance(value, dict):
            return {k: check(v) for k, v in value.items()}
        return value
    return check(brief)


def prepare_brief(graph: Any, llm: Any, store: Any, event: Event, force: bool = False) -> bool:
    """Generate (or refresh) one brief. False if someone else is on it or it's current."""
    if not store.claim_brief(event.id, event.subject, event.start.isoformat(), fingerprint(event), force=force):
        return False
    try:
        gathered = gather(graph, event)
        brief = write_brief(llm, gathered)
        brief["meeting"] = {"subject": event.subject, "when": fmt_local(event.start),
                            "ends": event.end.strftime("%I:%M %p").lstrip("0"), "location": event.location,
                            "join_url": event.join_url}
        brief["material"] = {
            "emails": [{k: e[k] for k in ("label", "subject", "received")} | {"from": e["from"].get("name") or e["from"].get("email")}
                       for e in gathered["emails"]],
            "attachments": [{k: a[k] for k in ("label", "name")} for a in gathered["attachments"]],
        }
        store.finish_brief(event.id, brief=brief)
    except Exception as e:  # noqa: BLE001 - recorded on the brief, retried next cycle
        store.finish_brief(event.id, error=f"{type(e).__name__}: {e}"[:500])
    return True


def upcoming_meetings(graph: Any, now: datetime | None = None) -> list[Event]:
    """The rest of today and the next working day."""
    from agent.calendar import next_working_day

    tz = local_zone()
    now = now or datetime.now(tz)
    end_day = next_working_day(now.date())
    end = datetime.combine(end_day + timedelta(days=1), datetime.min.time(), tz)
    events = [parse_event(raw, tz) for raw in graph.calendar_view(now, end) if not raw.get("isCancelled")]
    return [e for e in events if e.end > now]
