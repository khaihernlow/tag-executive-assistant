"""Changing what's already on Dave's calendar: answering invites, moving and
cancelling meetings.

Each change is an action. From chat, the model proposes it and Dave signs off
the slip; on Today, tapping Accept/Maybe/Decline on an invite is itself his
decision. Graph sends the updates: attendees get the new time or the
cancellation, organizers get Dave's response.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from agent.actions import ActionKind, Actions, public_action
from agent.calendar import Event, fmt_local, local_zone, parse_event
from agent.scheduling import get_busy
from agent.tools import Tool
from connectors.graph import EVENT_FIELDS
from llm.provider import ToolSpec

MOVE_KIND = "move_event"
CANCEL_KIND = "cancel_event"
RESPOND_KIND = "respond_invite"

RESPONSES = {"accept": "accept", "tentative": "tentativelyAccept", "decline": "decline"}
RESPONSE_WORDS = {"accept": "Accept", "tentative": "Tentatively accept", "decline": "Decline"}


def get_event(graph: Any, event_id: str) -> tuple[Event, dict[str, Any]]:
    try:
        raw = graph.get(f"/users/{graph.mailbox}/events/{event_id}", {"$select": EVENT_FIELDS},
                        headers={"Prefer": 'outlook.timezone="UTC"'})
    except Exception as e:  # noqa: BLE001
        raise ValueError("No such event. Look it up with find_events and pass its id.") from e
    return parse_event(raw, local_zone()), raw


def is_organizer(graph: Any, event: Event) -> bool:
    return event.response == "organizer" or event.organizer_email == graph.mailbox.lower()


def _clock(moment: datetime) -> str:
    return moment.strftime("%I:%M %p").lstrip("0")


def clashes_at(graph: Any, event: Event, start: datetime, end: datetime) -> list[str]:
    """What's in the way at a new time, for Dave (by name) and the attendees (free/busy).
    The meeting itself doesn't count, wherever it sits now."""
    tz = local_zone()
    out = []
    for raw in graph.calendar_view(start, end):
        other = parse_event(raw, tz)
        if (other.id != event.id and not raw.get("isCancelled") and other.blocks_time
                and other.response != "declined" and other.start < end and other.end > start):
            out.append(f"you have {other.subject} at {_clock(other.start)}")
    others = [a for a in event.attendee_emails if a and a != graph.mailbox.lower()]
    for address, name in zip(event.attendee_emails, event.attendees):
        if address not in others:
            continue
        try:
            busy, _ = get_busy(graph, [address], start, end)
        except Exception:  # noqa: BLE001 - outside people's calendars often can't be read
            continue
        if any(b.blocks_time and b.start < end and b.end > start
               and not (b.start == event.start and b.end == event.end) for b in busy):
            out.append(f"{name or address} is busy")
    return out


# ── actions ──────────────────────────────────────────────────────────────────

def change_kinds(graph: Any) -> list[ActionKind]:
    def utc(value: str) -> dict[str, str]:
        return {"dateTime": datetime.fromisoformat(value).astimezone(timezone.utc).replace(tzinfo=None).isoformat(),
                "timeZone": "UTC"}

    def move(payload: dict[str, Any], action_id: str) -> dict[str, Any]:
        # Updating an organizer's meeting sends the new time to every attendee.
        graph.patch(f"/users/{graph.mailbox}/events/{payload['event_id']}",
                    {"start": utc(payload["start"]), "end": utc(payload["end"])})
        return {"moved": True}

    def cancel(payload: dict[str, Any], action_id: str) -> dict[str, Any]:
        base = f"/users/{graph.mailbox}/events/{payload['event_id']}"
        if not payload["organizer"]:
            # Someone else's meeting: Dave can only decline it.
            graph.post(f"{base}/decline", {"comment": payload.get("comment") or "", "sendResponse": True})
        elif payload["has_attendees"]:
            graph.post(f"{base}/cancel", {"comment": payload.get("comment") or ""})  # attendees are told
        else:
            graph.delete(base)  # just Dave's own block
        return {"cancelled": True}

    def respond(payload: dict[str, Any], action_id: str) -> dict[str, Any]:
        graph.post(f"/users/{graph.mailbox}/events/{payload['event_id']}/{RESPONSES[payload['response']]}",
                   {"comment": payload.get("comment") or "", "sendResponse": True})
        return {"responded": payload["response"]}

    return [ActionKind(MOVE_KIND, move), ActionKind(CANCEL_KIND, cancel, editable=("comment",)),
            ActionKind(RESPOND_KIND, respond, editable=("comment",))]


def propose_move(graph: Any, actions: Actions, event_id: str, new_start: str,
                 duration_minutes: int | None = None, now: datetime | None = None) -> dict[str, Any]:
    event, _ = get_event(graph, event_id)
    if not is_organizer(graph, event):
        raise ValueError(f"{event.organizer or 'Someone else'} organizes \"{event.subject}\", so Dave can't move it. "
                         "Offer to decline it with a note suggesting another time, or to email the organizer.")
    tz = local_zone()
    try:
        start = datetime.fromisoformat(new_start).replace(tzinfo=None).replace(tzinfo=tz)
    except ValueError as e:
        raise ValueError("new_start must be Dave's local time as YYYY-MM-DDTHH:MM") from e
    if start < (now or datetime.now(tz)):
        raise ValueError("That time has already passed.")
    length = timedelta(minutes=duration_minutes) if duration_minutes else event.end - event.start
    end = start + length
    clashes = clashes_at(graph, event, start, end)
    others = [n for n, a in zip(event.attendees, event.attendee_emails) if a != graph.mailbox.lower()]
    summary = (f"Move “{event.subject}” from {fmt_local(event.start)} to {fmt_local(start)}"
               f"–{_clock(end)}" + (f" · {len(others)} {'attendee gets' if len(others) == 1 else 'attendees get'} the update"
                                         if others else ""))
    summary += "".join(f" · ⚠ {c}" for c in clashes)
    action = actions.propose(MOVE_KIND, summary, {"event_id": event.id, "subject": event.subject,
                                                  "start": start.isoformat(), "end": end.isoformat(),
                                                  "conflicts": clashes})
    return public_action(action)


def propose_cancel(graph: Any, actions: Actions, event_id: str, message: str = "") -> dict[str, Any]:
    event, _ = get_event(graph, event_id)
    organizer = is_organizer(graph, event)
    others = [a for a in event.attendee_emails if a != graph.mailbox.lower()]
    if organizer:
        summary = (f"Cancel “{event.subject}” on {fmt_local(event.start)}"
                   + (f" · {len(others)} {'attendee gets' if len(others) == 1 else 'attendees get'} a cancellation" if others else ""))
    else:
        summary = f"Decline “{event.subject}” on {fmt_local(event.start)} · {event.organizer} is told"
    payload = {"event_id": event.id, "subject": event.subject, "organizer": organizer, "has_attendees": bool(others)}
    if not organizer or others:
        # Someone hears about it, so the slip offers an optional note to them.
        payload["comment"] = message
        payload["to"] = event.organizer if not organizer else f"{len(others)} attendee{'s' if len(others) != 1 else ''}"
    return public_action(actions.propose(CANCEL_KIND, summary, payload))


def propose_response(graph: Any, actions: Actions, event_id: str, response: str, message: str = "") -> dict[str, Any]:
    if response not in RESPONSES:
        raise ValueError("response must be accept, tentative or decline")
    event, _ = get_event(graph, event_id)
    if is_organizer(graph, event):
        raise ValueError("Dave organizes this meeting; there's no invite to answer.")
    summary = f"{RESPONSE_WORDS[response]} “{event.subject}” on {fmt_local(event.start)} from {event.organizer}"
    return public_action(actions.propose(RESPOND_KIND, summary, {
        "event_id": event.id, "subject": event.subject, "response": response, "comment": message,
        "to": event.organizer or event.organizer_email}))


# ── unanswered invites (Today) ───────────────────────────────────────────────

def pending_invites(graph: Any, now: datetime | None = None, days: int = 21) -> list[dict[str, Any]]:
    """Invites from other people that Dave hasn't answered, soonest first, each with
    whatever it would clash with."""
    tz = local_zone()
    now = now or datetime.now(tz)
    raws = [r for r in graph.calendar_view(now, now + timedelta(days=days)) if not r.get("isCancelled")]
    events = [(parse_event(r, tz), r) for r in raws]
    me = graph.mailbox.lower()
    out = []
    for event, raw in events:
        if event.response not in ("none", "notResponded") or event.organizer_email in ("", me) or event.end <= now:
            continue
        clashes = [f"{other.subject}, {_clock(other.start)} to {_clock(other.end)}" for other, _ in events
                   if other.id != event.id and not other.all_day and not event.all_day and other.blocks_time
                   and other.response not in ("declined", "none", "notResponded")
                   and other.start < event.end and other.end > event.start]
        out.append({
            "id": event.id,
            "subject": event.subject,
            "organizer": event.organizer or event.organizer_email,
            "start": event.start.isoformat(),
            "when": ("All day " + event.start.strftime("%a, %b %d").replace(" 0", " ") if event.all_day else
                     event.start.strftime("%a, %b %d · ").replace(" 0", " ") + f"{_clock(event.start)} to {_clock(event.end)}"),
            "where": "Teams" if event.online else event.location,
            "attendees": len(event.attendee_emails),
            "clashes": clashes,
            "web_link": raw.get("webLink"),
        })
    return out


def answer_invite(graph: Any, actions: Actions, event_id: str, response: str, decided_by: str = "dave") -> dict[str, Any]:
    """Dave tapped a response on Today: that tap is the sign-off."""
    slip = propose_response(graph, actions, event_id, response)
    return public_action(actions.approve(slip["action_id"], decided_by=decided_by))


# ── chat tools ───────────────────────────────────────────────────────────────

def change_tools(graph: Any, actions: Actions) -> list[Tool]:
    def pending_note(result: dict[str, Any]) -> dict[str, Any]:
        if result["status"] == "pending":
            result["note"] = "Not done yet. Dave sees an approval card; tell him it's ready for his approval."
        return result

    def move(args: dict[str, Any]) -> dict[str, Any]:
        return pending_note(propose_move(graph, actions, args["event_id"], args["new_start"], args.get("duration_minutes")))

    def cancel(args: dict[str, Any]) -> dict[str, Any]:
        return pending_note(propose_cancel(graph, actions, args["event_id"], args.get("message") or ""))

    def respond(args: dict[str, Any]) -> dict[str, Any]:
        return pending_note(propose_response(graph, actions, args["event_id"], args["response"], args.get("message") or ""))

    event_id = {"type": "string", "description": "The event's id from find_events or list_calendar_events"}
    return [
        Tool(spec=ToolSpec(
            name="move_event",
            description=("Propose moving a meeting Dave organizes to a new time (attendees get the update once Dave "
                         "approves). Checks who's busy at the new time. Find the event first with find_events."),
            input_schema={"type": "object", "properties": {
                "event_id": event_id,
                "new_start": {"type": "string", "description": "Dave's local time, YYYY-MM-DDTHH:MM"},
                "duration_minutes": {"type": "integer", "description": "Only to change the length"},
            }, "required": ["event_id", "new_start"]}), handler=move),
        Tool(spec=ToolSpec(
            name="cancel_event",
            description=("Propose cancelling a meeting (attendees get a cancellation) or, if someone else organizes it, "
                         "declining it. Needs Dave's approval. Find the event first with find_events."),
            input_schema={"type": "object", "properties": {
                "event_id": event_id,
                "message": {"type": "string", "description": "Optional short note to include"},
            }, "required": ["event_id"]}), handler=cancel),
        Tool(spec=ToolSpec(
            name="respond_to_invite",
            description="Propose accepting, tentatively accepting or declining a meeting invite. Needs Dave's approval.",
            input_schema={"type": "object", "properties": {
                "event_id": event_id,
                "response": {"type": "string", "enum": ["accept", "tentative", "decline"]},
                "message": {"type": "string", "description": "Optional short note to the organizer"},
            }, "required": ["event_id", "response"]}), handler=respond),
    ]
