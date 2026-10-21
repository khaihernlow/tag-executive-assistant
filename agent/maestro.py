"""Meeting notes from TeamsMaestro: the note-taker Dave already uses.

TeamsMaestro joins Dave's meetings and emails him a write-up afterwards (noreply@maestrolabs.com,
"Summary: <meeting>"): purpose, takeaways, a detailed summary in sections, action items with an
owner each, and who attended. The transcript itself sits behind a TeamsMaestro login, so the email
is what we use: no Teams transcription and no recording needed for these meetings.

Code reads the email's structure (headings, list items, the bold task with its owner after it), so
nothing is re-written by a model; it finds the calendar meeting by title and time, and saves the
notes against it. A meeting that already has notes (a recording) gets this as another part.
"""

from __future__ import annotations

import base64
import html as htmllib
import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any

from agent.calendar import local_zone, parse_event
from agent.notes import add_part, new_part, number_lines

log = logging.getLogger("assistant.maestro")

SENDER = "noreply@maestrolabs.com"
STATE = "maestro_seen"
BACKFILL_DAYS = 120
SECTIONS = ("Purpose", "Takeaways", "Detailed summary", "Action items", "Attendees")


class _Tokens(HTMLParser):
    """The email as a stream of (kind, text): 'title' (h3), 'li' (a point), 'task' (bold action item),
    'span' and 'text' (everything else), in reading order."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.out: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag not in ("br", "img", "meta", "hr", "input"):
            self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in self.stack:
            while self.stack and self.stack.pop() != tag:
                pass

    def handle_data(self, data: str) -> None:
        text = " ".join(data.replace("\xa0", " ").split())
        if not text or "style" in self.stack or "head" in self.stack:
            return
        kind = ("title" if "h3" in self.stack else "task" if "strong" in self.stack else "li" if "li" in self.stack
                else "span" if self.stack and self.stack[-1] == "span" else "text")
        if self.out and self.out[-1][0] == kind and kind in ("li", "text", "task") and not self._new_block:
            self.out[-1] = (kind, f"{self.out[-1][1]} {text}")      # one point split across tags
        else:
            self.out.append((kind, text))
        self._new_block = False

    _new_block = True

    def handle_startendtag(self, tag: str, attrs: list) -> None:
        self._new_block = True

    def feed_all(self, body: str) -> list[tuple[str, str]]:
        # Every element boundary starts a new block, so two list items never merge
        self.feed(re.sub(r"(<(?:li|div|td|p|tr|h3|strong|span)\b)", r"<br/>\1", body, flags=re.I))
        return self.out


def summary_link(body: str) -> str | None:
    """The 'open in TeamsMaestro' address, unwrapped from Mandrill's click tracking."""
    for href in re.findall(r'href="([^"]+)"', body):
        m = re.search(r"[?&]p=([A-Za-z0-9_-]+)", htmllib.unescape(href))
        if not m:
            continue
        try:
            outer = json.loads(base64.urlsafe_b64decode(m.group(1) + "=" * (-len(m.group(1)) % 4)))
            url = json.loads(outer["p"])["url"]
        except (ValueError, KeyError, TypeError):
            continue
        if "/summary/" in url:
            return url
    return None


def parse(body: str) -> dict[str, Any]:
    """A TeamsMaestro email -> title, day, minutes, purpose, takeaways, sections, action items, attendees."""
    tokens = _Tokens().feed_all(body)
    out: dict[str, Any] = {"title": "", "day": None, "minutes": None, "purpose": "", "takeaways": [],
                           "sections": [], "action_items": [], "attendees": []}
    section = None
    for i, (kind, text) in enumerate(tokens):
        clean = text.strip(" •*")
        if kind == "title":
            if clean.lower() != "meeting notes":      # the email's own heading, not the meeting's name
                out["title"] = clean
            continue
        m = re.match(r"^([A-Z][a-z]{2} \d{1,2}, \d{4})\s*•\s*(\d+)\s*minutes?", text)
        if m and out["day"] is None:
            out["day"] = datetime.strptime(m.group(1), "%b %d, %Y").date()
            out["minutes"] = int(m.group(2))
            continue
        if kind in ("text", "span") and clean in SECTIONS:
            section = clean
            continue
        if section == "Purpose" and kind in ("text", "span") and clean:
            out["purpose"] = f"{out['purpose']} {clean}".strip()
        elif section == "Takeaways" and kind == "li":
            out["takeaways"].append(clean)
        elif section == "Detailed summary":
            if kind in ("text", "span") and clean:
                out["sections"].append({"heading": clean, "points": []})
            elif kind == "li":
                if not out["sections"]:
                    out["sections"].append({"heading": "", "points": []})
                out["sections"][-1]["points"].append(clean)
        elif section == "Action items":
            if kind == "task":
                out["action_items"].append({"what": clean, "owner": ""})
            elif kind == "span" and out["action_items"] and not out["action_items"][-1]["owner"]:
                out["action_items"][-1]["owner"] = clean
        elif section == "Attendees" and kind in ("text", "span"):
            names, footer, _ = clean.partition("TeamsMaestro users")   # the footer runs straight on
            out["attendees"] += [a.strip(" *") for a in names.split(",") if a.strip(" *")]
            if footer:
                section = None
    out["attendees"] = list(dict.fromkeys(out["attendees"]))
    out["sections"] = [s for s in out["sections"] if s["points"]]
    return out


def as_transcript(notes: dict[str, Any]) -> str:
    """The write-up as numbered lines: what search, chat and a later combined write-up read."""
    lines: list[tuple[str, str, None]] = []
    if notes["purpose"]:
        lines.append(("Purpose", notes["purpose"], None))
    lines += [("Takeaway", t, None) for t in notes["takeaways"]]
    for s in notes["sections"]:
        lines += [(s["heading"], p, None) for p in s["points"]]
    lines += [(f"Action ({a['owner'] or 'unassigned'})", a["what"], None) for a in notes["action_items"]]
    if notes["attendees"]:
        lines.append(("Attendees", ", ".join(notes["attendees"]), None))
    return number_lines(lines)


def as_digest(notes: dict[str, Any], dave: str = "Dave Vener") -> dict[str, Any]:
    first = dave.split()[0].lower()
    return {
        "headline": notes["purpose"] or (notes["takeaways"][0] if notes["takeaways"] else notes["title"]),
        "summary": notes["takeaways"],
        "decisions": [], "open_questions": [],
        "sections": notes["sections"],
        "action_items": [{"what": a["what"], "owner": a["owner"], "due_text": "", "due": None,
                          "dave": (a["owner"] or "").lower().startswith((first, "dave"))} for a in notes["action_items"]],
        "dropped": 0,
    }


# ── finding the meeting ──

def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 1}


def find_event(graph: Any, title: str, day: date | None, received: datetime) -> dict[str, Any] | None:
    """The calendar meeting these notes are for: same day, a matching title, over before the email came."""
    tz = local_zone()
    day = day or received.astimezone(tz).date()
    start = datetime.combine(day, datetime.min.time(), tz)
    best, score = None, 0.0
    for raw in graph.calendar_view(start, start + timedelta(days=1)):
        event = parse_event(raw, tz)
        if event.all_day or event.start > received + timedelta(minutes=5):
            continue
        a, b = _words(title), _words(event.subject)
        overlap = len(a & b) / max(1, len(a | b))
        if title.strip().lower() == event.subject.strip().lower():
            overlap = 1.0
        if overlap > score:
            best, score = event, overlap
    return {"event": best, "score": score} if best is not None and score >= 0.5 else None


# ── reading the inbox ──

def ingest(graph: Any, store: Any, limit: int = 40) -> int:
    """New TeamsMaestro emails become meeting notes. The first run reaches back BACKFILL_DAYS.
    Returns how many notes were added (or added to)."""
    seen = set(json.loads(store.get_state(STATE) or "[]"))
    backfilled = store.get_state(STATE + "_backfilled") == "yes"
    # Until the history is in, keep reaching back; after that only recent mail needs looking at
    since = (datetime.now(timezone.utc) - timedelta(days=7 if backfilled else BACKFILL_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    found = graph.get_all(f"/users/{graph.mailbox}/messages", {
        "$filter": f"receivedDateTime ge {since} and from/emailAddress/address eq '{SENDER}'",
        "$orderby": "receivedDateTime desc", "$select": "id,subject,receivedDateTime", "$top": 50}, limit=600)
    unseen = [m for m in found if m["id"] not in seen]
    fresh = unseen[:limit]
    if not backfilled and len(unseen) <= limit:
        store.set_state(STATE + "_backfilled", "yes")
    added = 0
    for m in reversed(fresh):                      # oldest first, so "part 2" follows "part 1"
        try:
            added += _take(graph, store, m)
        except Exception:  # noqa: BLE001 - one odd email mustn't stop the rest
            log.exception("TeamsMaestro email %s", m.get("subject"))
        seen.add(m["id"])
    if fresh or not store.get_state(STATE):
        store.set_state(STATE, json.dumps(sorted(seen)[-3000:]))
    return added


def _take(graph: Any, store: Any, message: dict[str, Any]) -> int:
    body = graph.get(f"/users/{graph.mailbox}/messages/{message['id']}", {"$select": "body"})["body"]["content"]
    notes = parse(body)
    if not notes["title"]:
        notes["title"] = re.sub(r"^Summary:\s*", "", message.get("subject") or "").strip()
    if not (notes["takeaways"] or notes["sections"] or notes["action_items"] or notes["purpose"]):
        return 0                                  # an empty write-up (a meeting nobody spoke in)
    received = datetime.fromisoformat(message["receivedDateTime"].replace("Z", "+00:00"))
    match = find_event(graph, notes["title"], notes["day"], received)
    event = match["event"] if match else None
    transcript = as_transcript(notes)
    if event is not None:
        existing = store.note_for_event(event.id)
        if existing:
            if existing.get("source") == "maestro":
                return 0                          # already have TeamsMaestro's notes for this one
            add_part(store, existing["id"], "maestro", transcript)
            return 1
    from agent.calendar import event_detail
    detail = event_detail(event) if event is not None else {}
    people = detail.get("attendees") or [{"name": n} for n in notes["attendees"]]
    started = (event.start if event is not None else
               received - timedelta(minutes=notes["minutes"] or 30)).astimezone(local_zone())
    digest = as_digest(notes)
    link = summary_link(body)
    if link:
        digest["link"] = link
    part = new_part(1, "maestro", transcript)
    part["added_at"] = received.astimezone(local_zone()).isoformat(timespec="minutes")
    store.add_note(kind="meeting", title=(event.subject if event is not None else notes["title"]), source="maestro",
                   event_id=event.id if event is not None else None, started_at=started.isoformat(timespec="minutes"),
                   people=people, transcript=transcript, parts=[part], status="ready", digest=digest)
    return 1
