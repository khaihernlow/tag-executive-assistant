"""Meeting briefs: prepared ahead of time, opened from the itinerary.

Code decides which meetings get a brief and gathers the material: the
invite, emails with the outside attendees and about the topic, relevant
attachments (resumes, proposals) and web research on the outside people and
companies. One model call writes the brief in a fixed structure from that
material only. Code then cleans the style and fact-checks addresses, and keeps
a record of every source so citations in the app open the real thing.
"""

from __future__ import annotations

import hashlib
import logging
import json
import re
from datetime import datetime, timedelta
from typing import Any

from agent.emailtext import excerpt as email_excerpt
from agent.calendar import Event, event_detail, fmt_local, local_zone, parse_event
from agent.factcheck import known_addresses, verify_addresses
from agent.mail import LIST_FIELDS, read_attachment, read_email, search_mail
from agent.people import internal_domain
from agent.research import research_targets, web_research
from llm.provider import ToolSpec

log = logging.getLogger("assistant.briefs")

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
EXCERPT_CHARS = 600
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


BRIEF_VERSION = "2"   # bump when briefs gain a source, so upcoming ones are written again with it


def fingerprint(event: Event) -> str:
    parts = [BRIEF_VERSION, event.subject, event.start.isoformat(), event.end.isoformat(), event.description,
             ",".join(sorted(event.attendee_emails))]
    return hashlib.sha1("|".join(parts).encode()).hexdigest()


def topic_words(subject: str) -> list[str]:
    words = re.findall(r"[A-Za-z][A-Za-z'&.-]{2,}", subject)
    return [w for w in words if w.lower().strip(".-'") not in STOPWORDS][:4]


# ── gathering ────────────────────────────────────────────────────────────────

def _participant_mail(graph: Any, address: str) -> list[dict[str, Any]]:
    return graph.get_all(f"/users/{graph.mailbox}/messages",
                         {"$search": f'"participants:{address}"', "$select": LIST_FIELDS, "$top": 10}, limit=10)


_LINK_NOISE = re.compile(r"<https?://[^>\s]*>?|https?://\S+|\[(?:signature|cid|image)[^\]]*\]", re.I)


def _excerpt(text: str, limit: int = EXCERPT_CHARS) -> str:
    """A readable preview: no tracking links or signature-image tags, whitespace collapsed."""
    text = " ".join(_LINK_NOISE.sub(" ", text or "").split())
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


MAX_LINKEDIN = 3  # people per brief (about a cent each, cached for a month)


def linkedin_findings(linkedin: Any, directory: Any, outside: list[dict[str, str]], first_label: int) -> list[dict[str, str]]:
    """Each outside attendee's LinkedIn background, as a '[Web n]' source with their profile link.
    The company comes from their email domain in Autotask; with no company and several
    profiles for the name, the person is skipped rather than guessed."""
    found = []
    for a in outside:
        name = (a.get("name") or "").strip()
        if len(found) >= MAX_LINKEDIN or "@" in name or len(name.split()) < 2:
            continue
        company = ""
        if directory is not None:
            try:
                companies = directory.find_companies(email=a.get("email") or "")
                company = companies[0]["name"] if len(companies) == 1 else ""
            except Exception:  # noqa: BLE001
                company = ""
        try:
            result = linkedin.find(name, company)
        except Exception:  # noqa: BLE001 - budget reached or Apify down: the brief goes ahead without it
            continue
        if result["person"]:
            found.append({"label": f"Web {first_label + len(found)}", "about": f"{name} (LinkedIn)",
                          "fact": result["summary"], "url": result["person"]["linkedin"]})
    return found


def gather(graph: Any, event: Event, searcher: Any = None, linkedin: Any = None, directory: Any = None) -> dict[str, Any]:
    domain = internal_domain()
    externals = external_attendees(event, graph.mailbox, domain)
    found: dict[str, str] = {}  # message id -> received
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
            "label": label, "message_id": mid, "web_link": email.get("web_link"),
            "from": email["from"], "subject": email["subject"], "received": email["received"],
            "body": email["body"][:EMAIL_CHARS],
        })
        for att in email.get("attachments", []):
            if len(attachments) < MAX_ATTACHMENTS and WORTH_READING.search(att.get("name") or "") \
                    and (att.get("size") or 0) < 5_000_000:
                try:
                    doc = read_attachment(graph, mid, att["id"])
                    attachments.append({"label": f"Attachment {len(attachments) + 1}", "name": doc["name"],
                                        "from_email": label, "message_id": mid, "web_link": email.get("web_link"),
                                        "text": doc["text"][:6000]})
                except Exception:  # noqa: BLE001 - unreadable files are skipped, not guessed
                    pass

    detail = event_detail(event)
    outside = [a for a in detail["attendees"] if a["email"] in externals]
    web = web_research(searcher, event.subject, research_targets(outside, domain)) if searcher else []
    if linkedin is not None:
        web += linkedin_findings(linkedin, directory, outside, len(web) + 1)
    return {"event": detail, "external": externals, "emails": emails, "attachments": attachments, "web": web}


# ── writing ──────────────────────────────────────────────────────────────────

WRITE_BRIEF = ToolSpec(
    name="write_brief",
    description="Record the meeting brief. Leave any list empty rather than pad it.",
    input_schema={
        "type": "object",
        "properties": {
            "headline": {"type": "string", "description": "One plain sentence: what this meeting is and why it matters"},
            "who": {"type": "array", "items": {"type": "object", "properties": {
                "name": {"type": "string"},
                "role": {"type": "string", "description": "Their role or title, if known"},
                "organization": {"type": "string", "description": "Their company, if known"},
                "note": {"type": "string", "description": "Optional: one short relevant fact about them"},
            }, "required": ["name"]}},
            "context": {"type": "array", "items": {"type": "string"},
                        "description": "Why this meeting is happening and the history so far"},
            "background": {"type": "array", "items": {"type": "string"},
                           "description": "Facts about the OUTSIDE people and companies only"},
            "prep": {"type": "array", "items": {"type": "string"},
                     "description": "2-4 specific talking points or questions for Dave"},
            "gaps": {"type": "array", "items": {"type": "string"},
                     "description": "Important things the material didn't answer (max 3)"},
        },
        "required": ["headline", "who", "context", "background", "prep", "gaps"],
    },
)

BRIEF_PROMPT = """You prepare short meeting briefs for Dave, CEO of TAG Solutions (an IT managed services provider
in Albany, NY). He reads them on his phone minutes before the meeting.

Content
- Use ONLY the material: the invite, emails, attachments, web findings, earlier meeting notes and HubSpot.
  Never invent facts.
- End each bullet with its source label(s) in brackets, e.g. "[Email 2]", "[Web 1]", "[Invite]", "[Notes 1]", "[HubSpot 2]".
- HubSpot is TAG's CRM. "HubSpot n" sources say who owns the account at TAG, its stage, when TAG last talked to
  them, and what TAG staff (not Dave) wrote, called or emailed. Use them for context and history: e.g. RJ's note
  that they chose TAG, a colleague's call last week. Say who at TAG did it. Its deal records are quote copies,
  not a pipeline: don't report deal stages.
- Earlier meeting notes ("Notes n") are what was said the last time Dave met these people. Use them for context,
  and put anything still open from them (a promise, a question, a deadline) in prep.
- Never tell Dave what he already knows: what TAG Solutions is or does, his own title, his own
  staff's roles, that he accepted the invite, which vendors TAG already uses. Background is ONLY about
  the outside people and companies, and only what matters for THIS meeting: skip generic facts about
  well-known companies (revenue, headcount, stock ticker, headquarters).
- Gaps are things worth finding out before or during the meeting, never questions about TAG itself.
- An empty section is better than a weak one. Do not pad.
- Prep must be specific to this meeting and these people, never generic interview or sales advice.
- Who: one entry per person attending (Dave's own staff only if they are part of the topic).
  Put title and company in their fields; no email addresses or phone numbers.

Style
- Plain sentences, at most 20 words per bullet; prep items are one question or point each. Lead with the fact.
- Dates: use the dates in the material exactly; never infer a year that isn't shown. A weekday in a source
  ("moved it to 11 AM Friday") stays a weekday: never attach a date to it unless the source gives that date.
- Credit each email to its sender as written in the material ("From x@y to ..."), not to whoever it mentions.
- Do not use em dashes or en dashes. Use commas, colons or a new sentence.
- No hedging filler ("it appears that", "it is worth noting"), no marketing adjectives."""

_DASH = re.compile(r"\s*[—–]\s*|\s+-\s+")
_RANGE = re.compile(r"(\d)\s*[–—]\s*(\d)")


_GROUPED_CITE = re.compile(r"\[(Emails?|Attachments?|Web|Notes|HubSpot)\s+([\d,\s&and]+)\]")
_CITE_KIND = {"emails": "Email", "email": "Email", "attachments": "Attachment", "attachment": "Attachment", "web": "Web",
              "notes": "Notes", "hubspot": "HubSpot"}


def _expand_citation(match: re.Match) -> str:
    kind = _CITE_KIND[match.group(1).lower()]
    return "[" + ", ".join(f"{kind} {n}" for n in re.findall(r"\d+", match.group(2))) + "]"


def plain_style(text: str) -> str:
    """Remove em/en dashes the model still writes (keeping numeric ranges like 9–10 AM)
    and normalize grouped citations ("[Emails 3, 4]" -> "[Email 3, Email 4]") so each
    becomes a tappable source in the app."""
    text = _GROUPED_CITE.sub(_expand_citation, text)
    text = _RANGE.sub(r"\1-\2", text)
    text = _DASH.sub(", ", text)
    return re.sub(r",\s*,", ",", text).strip(" ,")


def write_brief(llm: Any, gathered: dict[str, Any]) -> dict[str, Any]:
    material = json.dumps(gathered, ensure_ascii=False, default=str)
    today = datetime.now(local_zone()).strftime("%A %B %d %Y")
    response = llm.complete([{"role": "user", "content": f"Today is {today}.\nMaterial for the brief:\n{material}"}],
                            system=BRIEF_PROMPT, tools=[WRITE_BRIEF], tool_choice=WRITE_BRIEF.name, max_tokens=2500)
    calls = response.tool_calls
    if not calls:
        raise RuntimeError("The model did not return a brief.")
    brief = calls[0].input

    known = known_addresses(material)

    def clean(value: Any) -> Any:
        if isinstance(value, str):
            # Same rule as chat replies: every address must come from the material.
            return plain_style(verify_addresses(value, known)[0])
        if isinstance(value, list):
            return [c for c in (clean(v) for v in value) if c not in ("", None, {})]
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items()}
        return value
    out = clean(brief)
    for person in out.get("who") or []:
        # Citations belong in the note; in a name, role or company they'd show as raw "[HubSpot 1]"
        for key in ("name", "role", "organization"):
            if isinstance(person.get(key), str):
                person[key] = re.sub(r"\s*\[[^\]]*\d\]", "", person[key]).strip(" ,")
    return out


def material_record(event: Event, gathered: dict[str, Any]) -> dict[str, Any]:
    """What the app needs to open each cited source."""
    return {
        "invite": {"label": "Invite", "excerpt": email_excerpt(event.description), "web_link": None},
        "emails": [{
            "label": e["label"], "subject": e["subject"], "received": e["received"],
            "from": e["from"].get("name") or e["from"].get("email"),
            "message_id": e["message_id"], "web_link": e["web_link"], "excerpt": email_excerpt(e["body"], e["subject"]),
        } for e in gathered["emails"]],
        "attachments": [{
            "label": a["label"], "name": a["name"], "from_email": a["from_email"],
            "web_link": a["web_link"], "excerpt": _excerpt(a["text"], 400),
        } for a in gathered["attachments"]],
        "web": [{"label": w["label"], "about": w["about"], "fact": w["fact"], "url": w["url"]} for w in gathered["web"]],
        "notes": [{"label": n["label"], "id": n["id"], "title": n["title"], "when": n["when"], "headline": n["headline"],
                   "points": n["points"][:4]} for n in gathered.get("notes") or []],
        "hubspot": [{"label": h["label"], "kind": h["kind"], "title": h["title"], "when": h.get("when", ""), "by": h.get("by", ""),
                     "url": h.get("url"), "excerpt": h["text"]} for h in gathered.get("hubspot") or []],
    }


def prepare_brief(graph: Any, llm: Any, store: Any, event: Event, force: bool = False, searcher: Any = None,
                  linkedin: Any = None, directory: Any = None, hubspot: Any = None) -> bool:
    """Generate (or refresh) one brief. False if someone else is on it or it's current."""
    if not store.claim_brief(event.id, event.subject, event.start.isoformat(), fingerprint(event), force=force):
        return False
    try:
        gathered = gather(graph, event, searcher, linkedin, directory)
        # What was said last time with these people (the outside ones, or everyone but Dave).
        people = gathered["external"] or [a["email"] for a in gathered["event"]["attendees"]
                                          if a["email"] and a["email"].lower() != graph.mailbox.lower()]
        from agent.notes import earlier_notes
        gathered["notes"] = earlier_notes(store, people) if hasattr(store, "notes_with_people") else []
        if hubspot is not None and gathered["external"]:
            from agent.hubspot import brief_material
            try:
                gathered["hubspot"] = brief_material(hubspot, gathered["external"], graph.mailbox)
            except Exception:  # noqa: BLE001 - HubSpot being slow or down shouldn't cost Dave his brief
                log.exception("HubSpot material failed for %s", event.subject)
        brief = write_brief(llm, gathered)
        brief["meeting"] = {"subject": event.subject, "when": fmt_local(event.start),
                            "ends": event.end.strftime("%I:%M %p").lstrip("0"), "location": event.location,
                            "join_url": event.join_url}
        brief["material"] = material_record(event, gathered)
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
