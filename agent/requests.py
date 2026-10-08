"""Meeting requests: what Maria does when someone emails "can we meet?".

  spot      code pre-filters the inbox (meeting words, not invites, not from
            Dave); the fast model classifies only those candidates and extracts
            who, what, how long, when, and in what format.
  suggest   code picks up to three times from Dave's free time (and the
            requester's, for staff), spread across days.
  reply     the model drafts a short reply in Dave's voice proposing the times;
            code checks every time made it in. Sending is an approval slip with
            the full, editable text.
  book      or book straight away when they proposed a time (or for staff).

  follow up after Dave sends times, the thread is watched: when they pick one of
            the offered times and it is still free, it is booked (invite and
            Teams link) without asking again, since Dave chose those times; a
            different time or anything else brings the card back.

A request is closed when anyone at TAG (Dave or Maria) replies in the thread, a
meeting with that person appears on Dave's calendar, or Dave books or dismisses it.

Statuses: new -> waiting (times sent) -> booked; also handled, dismissed,
superseded, ignored.
"""

from __future__ import annotations

import html
import re
import time as clock
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from agent.actions import ActionKind, Actions, public_action
from agent.calendar import find_free_slots, local_zone, next_working_day
from agent.events import create_event_kind, find_conflicts, summarize_event, validate_event
from agent.people import internal_domain
from agent.junk import known_correspondents
from agent.mail import read_email
from agent.relationship import automated, clean_preview, describe, thread_deleted_before
from agent.scheduling import get_busy
from llm.provider import ToolSpec

REPLY_KIND = "reply_email"
BOOK_KIND = "book_meeting"
SCAN_DAYS = 3
MEETING_WORDS = re.compile(
    r"\b(meet|meeting|call|chat|catch up|catch-up|connect|sync|availability|available|free (?:time|for)|"
    r"time to|find (?:a )?time|schedule|calendar|zoom|teams|coffee|lunch|this week|next week|grab \d+)\b", re.I)
LIST_SELECT = "id,subject,from,receivedDateTime,bodyPreview,conversationId,webLink,parentFolderId"
SKIP_FOLDERS = ("sentitems", "drafts", "junkemail", "deleteditems", "outbox")
_skip_folder_ids: dict[str, set[str]] = {}


def skip_folder_ids(graph: Any) -> set[str]:
    """Ids of Sent, Drafts, Junk, Deleted and Outbox (looked up once)."""
    if graph.mailbox not in _skip_folder_ids:
        ids = set()
        for name in SKIP_FOLDERS:
            try:
                ids.add(graph.get(f"/users/{graph.mailbox}/mailFolders/{name}", {"$select": "id"})["id"])
            except Exception:  # noqa: BLE001 - a missing folder just isn't skipped
                pass
        _skip_folder_ids[graph.mailbox] = ids
    return _skip_folder_ids[graph.mailbox]


# ── spotting ─────────────────────────────────────────────────────────────────

def candidates(graph: Any, seen: set[str], now: datetime | None = None) -> list[dict[str, Any]]:
    """Recent mail that might be a meeting request, newest per thread, not yet looked at.

    Covers every folder, not just the Inbox: Dave's mail is filed into folders as
    it arrives (his Inbox holds a handful of messages), so a request is usually
    somewhere else by the time the worker looks."""
    since = ((now or datetime.now(timezone.utc)) - timedelta(days=SCAN_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent = graph.get_all(f"/users/{graph.mailbox}/messages", {
        "$select": LIST_SELECT, "$filter": f"receivedDateTime ge {since}",
        "$orderby": "receivedDateTime desc", "$top": 100}, limit=300)
    skip = skip_folder_ids(graph)
    out, threads = [], set()
    for m in recent:
        sender = (((m.get("from") or {}).get("emailAddress") or {}).get("address") or "").lower()
        if m.get("@odata.type", "").endswith("eventMessage") or sender == graph.mailbox.lower():
            continue  # calendar invites/responses, and Dave's own mail
        if m.get("parentFolderId") in skip:
            continue  # sent, drafts, junk, deleted
        thread = m.get("conversationId") or m["id"]
        if thread in threads:
            continue  # only the newest message of a thread speaks for it
        threads.add(thread)
        if m["id"] in seen or not MEETING_WORDS.search(f"{m.get('subject', '')} {m.get('bodyPreview', '')}"):
            continue
        out.append(m)
    return out


CLASSIFY = ToolSpec(
    name="record_requests",
    description="Record, for every email, whether it is a genuine request to meet Dave.",
    input_schema={"type": "object", "properties": {"emails": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "n": {"type": "integer"},
            "kind": {"type": "string",
                     "enum": ["asks_for_times", "proposes_time", "cold_outreach", "not_a_request"]},
            "purpose": {"type": "string", "description": "What the meeting is about, under 8 words"},
            "why": {"type": "string", "description": "For a genuine request: who they are and why they want to meet, under 15 words"},
            "duration_minutes": {"type": "integer", "description": "Only if stated"},
            "earliest_date": {"type": "string", "description": "YYYY-MM-DD, from phrases like 'next week'"},
            "latest_date": {"type": "string", "description": "YYYY-MM-DD"},
            "time_of_day": {"type": "string", "enum": ["morning", "afternoon", "any"]},
            "format": {"type": "string", "enum": ["teams", "in_person", "phone", "unspecified"]},
            "proposed_start": {"type": "string", "description": "For proposes_time: YYYY-MM-DDTHH:MM, Dave's local time"},
        },
        "required": ["n", "kind"],
    }}}, "required": ["emails"]},
)

CLASSIFY_PROMPT = """You triage Dave's inbox (CEO, TAG Solutions, an IT managed services provider). For each email
decide whether it is a GENUINE request to meet Dave that still needs scheduling.
- asks_for_times: someone with a real reason wants to meet and needs times.
- proposes_time: they suggest a specific day and time.
- cold_outreach: unsolicited sales, staffing, recruiting, lead-generation, marketing or vendor pitches
  asking for "a quick call", including automated follow-ups ("following up on this", "reply STOP").
  Be skeptical when the sender is a first contact, earlier messages in the thread were deleted, or it
  has automated-sequence text.
- not_a_request: newsletters, webinars, automated mail, meetings already booked or confirmed, someone
  just mentioning a meeting, or anything else.
A first contact CAN be genuine: a prospect or client asking TAG for IT help is a real request.
Resolve relative dates ("next week", "Thursday") against the date received, in US Eastern time.
Leave fields empty when the email doesn't say."""


def classify(llm: Any, messages: list[dict[str, Any]], facts: dict[str, dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    if not messages:
        return {}
    facts = facts or {}

    def line(i: int, m: dict[str, Any]) -> str:
        f = facts.get(m["id"], {})
        context = (f"Sender is: {f.get('label', 'unknown')}. Earlier message in this thread deleted: "
                   f"{'yes' if f.get('deleted_before') else 'no'}. Automated-sequence text: {'yes' if f.get('automated') else 'no'}.")
        return (f"#{i}\nReceived: {m.get('receivedDateTime', '')}\nFrom: "
                f"{((m.get('from') or {}).get('emailAddress') or {}).get('name', '')}\n{context}\n"
                f"Subject: {m.get('subject', '')}\n{clean_preview(m.get('bodyPreview') or '')[:400]}")

    listing = "\n\n".join(line(i, m) for i, m in enumerate(messages))
    response = llm.complete([{"role": "user", "content": listing}], system=CLASSIFY_PROMPT,
                            tools=[CLASSIFY], tool_choice=CLASSIFY.name, max_tokens=3000)
    out: dict[str, dict[str, Any]] = {}
    for call in response.tool_calls:
        for item in call.input.get("emails") or []:
            n = item.get("n")
            if isinstance(n, int) and 0 <= n < len(messages):
                out[messages[n]["id"]] = item
    return out


def sender_facts(graph: Any, store: Any, messages: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Relationship, deleted-earlier and automation facts per message (code, no model)."""
    correspondents = known_correspondents(graph)
    domain = internal_domain()
    facts = {}
    for m in messages:
        address = (((m.get("from") or {}).get("emailAddress") or {}).get("address") or "").lower()
        info = describe(address, correspondents, store, domain)
        if not info["known"]:
            # Only worth the extra lookup for senders Dave doesn't already know.
            info["deleted_before"] = thread_deleted_before(graph, m)
        info["automated"] = automated(m.get("bodyPreview") or "")
        facts[m["id"]] = info
    return facts


def scan(graph: Any, llm: Any, store: Any, now: datetime | None = None, actions: Any = None) -> int:
    """Find new meeting requests, follow up on threads waiting for a reply, and close
    requests the team already handled. Returns how many new requests were added."""
    waiting = {row["thread_id"] for row in store.requests_in(("waiting",))}
    inbox = candidates(graph, set(), now)
    seen = store.seen_request_ids([m["id"] for m in inbox])
    fresh = [m for m in inbox if m["id"] not in seen and (m.get("conversationId") or m["id"]) not in waiting]
    facts = sender_facts(graph, store, fresh)
    # Senders Dave has junked are never meeting requests; no need to ask the model.
    for m in [m for m in fresh if facts[m["id"]]["kind"] == "junk"]:
        store.save_request(m["id"], m.get("conversationId") or m["id"], m["receivedDateTime"], "ignored")
    fresh = [m for m in fresh if facts[m["id"]]["kind"] != "junk"]
    verdicts = classify(llm, fresh, facts)
    added = 0
    for m in fresh:
        verdict = verdicts.get(m["id"], {"kind": "not_a_request"})
        thread = m.get("conversationId") or m["id"]
        if verdict.get("kind") not in ("asks_for_times", "proposes_time"):
            store.save_request(m["id"], thread, m["receivedDateTime"], "ignored")
            continue
        sender = (m.get("from") or {}).get("emailAddress") or {}
        request = {
            **{k: v for k, v in verdict.items() if k != "n" and v not in (None, "")},
            "from_name": sender.get("name") or sender.get("address"),
            "from_email": (sender.get("address") or "").lower(),
            "subject": m.get("subject") or "",
            "preview": clean_preview(m.get("bodyPreview") or "")[:300],
            "web_link": m.get("webLink"),
            "relationship": facts[m["id"]]["label"],
        }
        # A newer message in the same thread replaces any older open request for it.
        for old in store.open_requests():
            if old["thread_id"] == thread:
                store.update_request(old["message_id"], status="superseded")
        store.save_request(m["id"], thread, m["receivedDateTime"], "new", request)
        added += 1
    retire_handled(graph, store)
    if actions is not None:
        follow_up(graph, llm, store, actions)
    return added


def thread_messages(graph: Any, thread: str, after: str) -> list[dict[str, Any]]:
    """Messages in a thread after a time, from every folder (Dave's sent mail included)."""
    return graph.get_all(f"/users/{graph.mailbox}/messages", {
        "$select": "id,from,receivedDateTime,bodyPreview",
        "$filter": f"conversationId eq '{thread}' and receivedDateTime gt {after}",
        "$top": 20}, limit=20)


def _sender(message: dict[str, Any]) -> str:
    return (((message.get("from") or {}).get("emailAddress") or {}).get("address") or "").lower()


def retire_handled(graph: Any, store: Any) -> None:
    """Someone at TAG already dealt with it: Dave or Maria replied in the thread (a
    reply from Maria's own mailbox still reaches Dave's when he's on the thread), or
    a meeting with that person is now on Dave's calendar (e.g. Maria booked it)."""
    domain = internal_domain()
    rows = store.open_requests()
    if not rows:
        return
    tz = local_zone()
    now = datetime.now(tz)
    upcoming = graph.calendar_view(now, now + timedelta(days=45))
    for row in rows:
        requester = (row["request"] or {}).get("from_email", "")
        later = thread_messages(graph, row["thread_id"], row["received_at"])
        if any(_sender(m) != requester and (_sender(m) == graph.mailbox.lower() or _sender(m).endswith("@" + domain))
               for m in later):
            store.update_request(row["message_id"], status="handled")
            continue
        if requester and any(requester in {((a.get("emailAddress") or {}).get("address") or "").lower()
                                           for a in e.get("attendees") or []} for e in upcoming):
            store.update_request(row["message_id"], status="handled")


# ── following up after Dave sent times ───────────────────────────────────────

READ_ANSWER = ToolSpec(
    name="record_answer",
    description="Record what the person said about the proposed meeting times.",
    input_schema={"type": "object", "properties": {
        "outcome": {"type": "string", "enum": ["picked_offered", "proposed_other", "declined", "other"]},
        "slot_number": {"type": "integer", "description": "For picked_offered: which offered time (1-based)"},
        "proposed_start": {"type": "string", "description": "For proposed_other: YYYY-MM-DDTHH:MM, Dave's local time"},
        "summary": {"type": "string", "description": "Their reply in under 15 words"},
    }, "required": ["outcome", "summary"]},
)

READ_ANSWER_PROMPT = """Dave offered meeting times by email. Read the person's reply and record what they said.
- picked_offered: they accepted one of the offered times (give its number).
- proposed_other: they suggested a different time (resolve it to a date and time in US Eastern).
- declined: they don't want or need the meeting.
- other: anything else (questions, "let me check", out of office).
Only choose picked_offered when the reply clearly matches exactly one offered time."""


def read_answer(llm: Any, offered: list[str], reply_text: str, received: str) -> dict[str, Any]:
    listing = "\n".join(f"{i + 1}. {slot_label(datetime.fromisoformat(s))}" for i, s in enumerate(offered))
    response = llm.complete(
        [{"role": "user", "content": f"Times Dave offered:\n{listing}\n\nTheir reply (received {received}):\n{reply_text}"}],
        system=READ_ANSWER_PROMPT, tools=[READ_ANSWER], tool_choice=READ_ANSWER.name, max_tokens=400)
    return response.tool_calls[0].input if response.tool_calls else {"outcome": "other", "summary": ""}


def follow_up(graph: Any, llm: Any, store: Any, actions: Any) -> None:
    """For threads waiting on the other person: did they answer, and with what?"""
    for row in store.requests_in(("waiting",)):
        request = row["request"] or {}
        requester = request.get("from_email", "")
        answers = [m for m in thread_messages(graph, row["thread_id"], request.get("replied_at") or row["received_at"])
                   if _sender(m) == requester]
        if not answers:
            continue
        latest = max(answers, key=lambda m: m.get("receivedDateTime", ""))
        try:
            text = read_email(graph, latest["id"])["body"][:2000]
        except Exception:  # noqa: BLE001 - fall back to the preview
            text = latest.get("bodyPreview") or ""
        answer = read_answer(llm, request.get("offered") or [], text, latest.get("receivedDateTime", ""))
        request = {**request, "answer": answer, "answer_message_id": latest["id"]}
        outcome = answer.get("outcome")
        offered = request.get("offered") or []
        number = answer.get("slot_number")

        if outcome == "picked_offered" and isinstance(number, int) and 1 <= number <= len(offered):
            store.update_request(row["message_id"], request=request)
            action = propose_booking(graph, store, actions, row["message_id"], offered[number - 1],
                                     allow_status=("waiting",))
            if not action.get("conflicts_found"):
                # Dave chose this time when he sent it; it's still free, so book it.
                actions.approve(action["action_id"], decided_by=f"auto: {request.get('from_name')} picked a time you offered")
            else:
                store.update_request(row["message_id"], status="new")
        elif outcome == "proposed_other" and answer.get("proposed_start"):
            store.update_request(row["message_id"], status="new",
                                 request={**request, "kind": "proposes_time", "proposed_start": answer["proposed_start"]})
        else:
            store.update_request(row["message_id"], status="new", request=request)


# ── suggesting times ─────────────────────────────────────────────────────────

def _round_up(moment: datetime, minutes: int = 30) -> datetime:
    extra = (-moment.minute) % minutes
    return (moment + timedelta(minutes=extra)).replace(second=0, microsecond=0)


def suggest_slots(graph: Any, request: dict[str, Any], memory: Any = None, now: datetime | None = None,
                  count: int = 3) -> list[datetime]:
    """Up to `count` start times, one per day where possible, inside Dave's preferred hours."""
    tz = local_zone()
    now = now or datetime.now(tz)
    duration = timedelta(minutes=int(request.get("duration_minutes") or (memory.pref("meeting_minutes") if memory else 30)))
    first = _parse_day(request.get("earliest_date")) or next_working_day(now.date())
    last = _parse_day(request.get("latest_date")) or first + timedelta(days=7)
    first, last = max(first, now.date()), max(last, first)
    day_start = time.fromisoformat(memory.pref("day_start") if memory else "08:00")
    day_end = time.fromisoformat(memory.pref("day_end") if memory else "17:00")
    if request.get("time_of_day") == "morning":
        day_end = min(day_end, time(12, 0))
    elif request.get("time_of_day") == "afternoon":
        day_start = max(day_start, time(12, 0))

    people = [graph.mailbox.lower()]
    requester = request.get("from_email", "")
    if requester.endswith("@" + internal_domain()):
        people.append(requester)  # staff: their free/busy is visible, so find a time that works for both
    start = datetime.combine(first, time.min, tz)
    end = datetime.combine(last + timedelta(days=1), time.min, tz)
    busy, _ = get_busy(graph, people, start, end)
    windows = find_free_slots(busy, first, last, duration, tz, day_start=day_start, day_end=day_end)

    # Every possible start on the half hour, then one per day near a sensible time.
    # The earliest free slot is usually 8:00, a poor offer to someone outside, so
    # each day aims for a different target (10:00, 2:00, 11:00) to give real choice.
    options: list[datetime] = []
    for begin, finish in windows:
        candidate = _round_up(max(begin, now + timedelta(hours=2)))
        while candidate + duration <= finish:
            options.append(candidate)
            candidate += timedelta(minutes=30)
    by_day: dict[date, list[datetime]] = {}
    for option in options:
        by_day.setdefault(option.date(), []).append(option)
    targets = (time(10, 0), time(14, 0), time(11, 0))

    def nearest(day_options: list[datetime], target: time) -> datetime:
        aim = datetime.combine(day_options[0].date(), target, tz)
        return min(day_options, key=lambda o: abs((o - aim).total_seconds()))

    picks = [nearest(day_options, targets[i % len(targets)]) for i, day_options in enumerate(by_day.values())][:count]
    picks += [o for o in options if o not in picks][: count - len(picks)]
    return sorted(picks)


def _parse_day(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def slot_label(start: datetime) -> str:
    return start.strftime("%A, %b %d at %I:%M %p").replace(" 0", " ") + " ET"


# ── replying ─────────────────────────────────────────────────────────────────

WRITE_REPLY = ToolSpec(
    name="write_reply",
    description="Record the reply email body.",
    input_schema={"type": "object", "properties": {"comment": {"type": "string"}}, "required": ["comment"]},
)

REPLY_PROMPT = """Write Dave's reply email (Dave is CEO of TAG Solutions). He writes short, warm, direct emails;
match the style samples. Propose exactly the listed times, each on its own line, and ask them to pick
one (or suggest another). If the meeting is on Teams, say a Teams invite will follow. Under 80 words.
Sign off with just "Dave". No subject line, no em dashes, no placeholders."""

_style_cache: dict[str, tuple[float, list[str]]] = {}


def style_samples(graph: Any, count: int = 6) -> list[str]:
    """A few of Dave's own short sent emails, so drafts sound like him. Cached for 12 hours."""
    cached = _style_cache.get(graph.mailbox)
    if cached and clock.monotonic() - cached[0] < 12 * 3600:
        return cached[1]
    samples = []
    for m in graph.get_all(f"/users/{graph.mailbox}/mailFolders/sentitems/messages",
                           {"$select": "bodyPreview", "$top": 40}, limit=40):
        text = " ".join((m.get("bodyPreview") or "").split())
        text = re.split(r"Dave Vener|From:|Sent from", text)[0].strip()
        if 20 <= len(text) <= 400:
            samples.append(text)
        if len(samples) >= count:
            break
    _style_cache[graph.mailbox] = (clock.monotonic(), samples)
    return samples


def draft_reply(llm: Any, graph: Any, request: dict[str, Any], slots: list[datetime]) -> str:
    labels = [slot_label(s) for s in slots]
    brief = (f"Their email (from {request.get('from_name')}): \"{request.get('subject')}\" {request.get('preview')}\n"
             f"Meeting: {request.get('purpose') or 'a meeting'}, format: {request.get('format') or 'unspecified'}\n"
             "Times to propose:\n" + "\n".join(labels) +
             "\n\nStyle samples of Dave's emails:\n" + "\n---\n".join(style_samples(graph)))
    response = llm.complete([{"role": "user", "content": brief}], system=REPLY_PROMPT,
                            tools=[WRITE_REPLY], tool_choice=WRITE_REPLY.name, max_tokens=600)
    comment = (response.tool_calls[0].input.get("comment") if response.tool_calls else "") or ""
    comment = re.sub(r"\s*[—–]\s*", ", ", comment).strip()
    missing = [label for label in labels if label.split(" at ")[1].replace(" ET", "") not in comment]
    if not comment or missing:
        # The times are the point of the email: if any didn't make it in verbatim, use a plain version.
        comment = ("Hi " + (request.get("from_name") or "").split(" ")[0] + ",\n\nHappy to meet. Would any of these work?\n"
                   + "\n".join(labels) + "\n\nDave")
    return comment


# ── actions ──────────────────────────────────────────────────────────────────

def request_kinds(graph: Any, store: Any) -> list[ActionKind]:
    def send_reply(payload: dict[str, Any], action_id: str) -> dict[str, Any]:
        # Reply in the thread (keeps their email quoted below); sends immediately.
        # The reply API reads the comment as HTML: escape it and keep the line breaks.
        body = html.escape(payload["comment"]).replace("\n", "<br>")
        graph.post(f"/users/{graph.mailbox}/messages/{payload['message_id']}/reply", {"comment": body})
        return {"sent": True}

    def mark(status: str):
        return lambda payload, result: store.update_request(payload["message_id"], status=status)

    def sent(payload: dict[str, Any], result: dict[str, Any]) -> None:
        row = store.get_request(payload["message_id"])
        request = {**(row["request"] or {}), "offered": payload.get("slots") or [],
                   "replied_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        store.update_request(payload["message_id"], status="waiting", request=request)

    def booked(payload: dict[str, Any], result: dict[str, Any]) -> None:
        row = store.get_request(payload["message_id"])
        request = {**(row["request"] or {}), "booked_start": payload.get("start")}
        store.update_request(payload["message_id"], status="booked", request=request)

    book = create_event_kind(graph)
    return [
        ActionKind(REPLY_KIND, send_reply, editable=("comment",), on_done=sent),
        ActionKind(BOOK_KIND, book.execute, on_done=booked),
    ]


def propose_reply(graph: Any, llm: Any, store: Any, actions: Actions, message_id: str,
                  starts: list[str]) -> dict[str, Any]:
    row = store.get_request(message_id)
    if row is None or row["status"] != "new":
        raise ValueError("This request is no longer open.")
    tz = local_zone()
    slots = sorted(datetime.fromisoformat(s).astimezone(tz) for s in starts)
    if not slots:
        raise ValueError("Pick at least one time.")
    request = row["request"]
    comment = draft_reply(llm, graph, request, slots)
    action = actions.propose(REPLY_KIND, f"Reply to {request['from_name']} proposing {len(slots)} time"
                             f"{'s' if len(slots) != 1 else ''}", {
        "message_id": message_id, "to": request["from_email"], "subject": f"Re: {request['subject']}",
        "comment": comment, "slots": [s.isoformat() for s in slots],
    })
    store.update_request(message_id, action_id=action["id"])
    return public_action(action)


def propose_booking(graph: Any, store: Any, actions: Actions, message_id: str, start: str,
                    memory: Any = None, allow_status: tuple[str, ...] = ("new",)) -> dict[str, Any]:
    row = store.get_request(message_id)
    if row is None or row["status"] not in allow_status:
        raise ValueError("This request is no longer open.")
    request = row["request"]
    local = datetime.fromisoformat(start).astimezone(local_zone()).strftime("%Y-%m-%dT%H:%M")
    duration = int(request.get("duration_minutes") or (memory.pref("meeting_minutes") if memory else 30))
    payload = validate_event({
        "subject": request.get("purpose") or f"Meeting with {request['from_name']}",
        "start": local, "duration_minutes": duration,
        "attendees": [{"email": request["from_email"], "name": request.get("from_name", "")}],
        "teams": request.get("format") != "in_person",
    })
    conflicts, unchecked = find_conflicts(graph, payload)
    payload.update({"conflicts": conflicts, "message_id": message_id})
    action = actions.propose(BOOK_KIND, summarize_event(payload, conflicts, unchecked), payload)
    store.update_request(message_id, action_id=action["id"])
    return {**public_action(action), "conflicts_found": bool(conflicts)}


def open_requests_view(graph: Any, store: Any, actions_store: Any, memory: Any = None) -> list[dict[str, Any]]:
    """What the Today screen shows for each open request, with fresh suggested times."""
    out = []
    recent = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    rows = store.requests_in(("new", "waiting")) + store.requests_in(("booked",), since=recent)
    for row in rows:
        request = row["request"] or {}
        if row["status"] == "new":
            try:
                slots = suggest_slots(graph, request, memory)
            except Exception:  # noqa: BLE001 - a calendar hiccup shouldn't hide the request
                slots = []
        else:
            slots = []
        proposed = None
        if request.get("kind") == "proposes_time" and request.get("proposed_start"):
            try:
                start = datetime.fromisoformat(request["proposed_start"]).replace(tzinfo=local_zone())
                proposed = {"start": start.isoformat(), "label": slot_label(start)}
            except ValueError:
                pass
        action = actions_store.get_action(row["action_id"]) if row.get("action_id") else None
        out.append({
            "id": row["message_id"],
            "status": row["status"],
            "offered": [slot_label(datetime.fromisoformat(s).astimezone(local_zone())) for s in request.get("offered") or []],
            "answer": (request.get("answer") or {}).get("summary"),
            "booked": slot_label(datetime.fromisoformat(request["booked_start"]))
                      if request.get("booked_start") else None,
            "received": row["received_at"],
            "from": request.get("from_name"),
            "from_email": request.get("from_email"),
            "subject": request.get("subject"),
            "purpose": request.get("purpose"),
            "why": request.get("why"),
            "relationship": request.get("relationship"),
            "kind": request.get("kind"),
            "duration_minutes": request.get("duration_minutes"),
            "format": request.get("format"),
            "web_link": request.get("web_link"),
            "slots": [{"start": s.isoformat(), "label": slot_label(s)} for s in slots],
            "proposed": proposed,
            "action": public_action(action) if action and action["status"] in ("pending", "executing") else None,
        })
    return out
