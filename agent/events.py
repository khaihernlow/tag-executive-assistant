"""create_event: book a meeting on Dave's calendar, behind approval.

The tool the model calls only validates and proposes. Code checks the
time, duration, attendee addresses and conflicts (live free/busy), and
writes the human-readable summary shown on the approval card. Booking
happens in `execute_create_event` once Dave approves.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from agent.actions import ActionKind, Actions, public_action
from agent.calendar import fmt_local, local_zone
from agent.scheduling import get_busy
from agent.tools import Tool
from llm.provider import ToolSpec

KIND = "create_event"


class GraphSource(Protocol):
    mailbox: str

    def post(self, path: str, body: dict[str, Any], headers: dict[str, str] | None = None) -> dict[str, Any]: ...


def validate_event(args: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    tz = local_zone()
    subject = (args.get("subject") or "").strip()
    if not subject:
        raise ValueError("subject is required")
    try:
        start = datetime.fromisoformat(args["start"]).replace(tzinfo=None).replace(tzinfo=tz)
    except (KeyError, ValueError) as e:
        raise ValueError("start must be local time as YYYY-MM-DDTHH:MM") from e
    duration = int(args.get("duration_minutes", 30))
    if not 5 <= duration <= 480:
        raise ValueError("duration_minutes must be between 5 and 480")
    if start < (now or datetime.now(tz)):
        raise ValueError("start is in the past")

    attendees = []
    for raw in args.get("attendees") or []:
        email = (raw.get("email") if isinstance(raw, dict) else raw or "").strip().lower()
        name = raw.get("name", "") if isinstance(raw, dict) else ""
        if "@" not in email:
            raise ValueError(f"Attendee {raw!r} is not an email address. Use find_person first.")
        attendees.append({"email": email, "name": name})

    return {
        "subject": subject,
        "start": start.isoformat(),
        "end": (start + timedelta(minutes=duration)).isoformat(),
        "attendees": attendees,
        "teams": bool(args.get("teams", True)),
        "location": (args.get("location") or "").strip(),
        "description": (args.get("description") or "").strip(),
    }


def summarize_event(payload: dict[str, Any], conflicts: list[str], unchecked: list[str]) -> str:
    start = datetime.fromisoformat(payload["start"])
    end = datetime.fromisoformat(payload["end"])
    who = ", ".join(a["name"] or a["email"] for a in payload["attendees"]) or "just you"
    kind = "Teams meeting" if payload["teams"] else "Meeting"
    where = f" at {payload['location']}" if payload["location"] else ""
    text = f"{kind} “{payload['subject']}” · {fmt_local(start)}–{end.strftime('%I:%M %p').lstrip('0')}{where} · with {who}"
    if conflicts:
        text += f" · ⚠ conflicts: {', '.join(conflicts)}"
    if unchecked:
        text += f" · couldn't check: {', '.join(unchecked)}"
    return text


def find_conflicts(graph: GraphSource, payload: dict[str, Any]) -> tuple[list[str], list[str]]:
    start = datetime.fromisoformat(payload["start"])
    end = datetime.fromisoformat(payload["end"])
    people = [graph.mailbox.lower()] + [a["email"] for a in payload["attendees"] if a["email"] != graph.mailbox.lower()]
    busy, unavailable = get_busy(graph, people, start, end)
    clashes = sorted({fmt_local(b.start) for b in busy if b.blocks_time and b.start < end and b.end > start})
    return clashes, [u["email"] for u in unavailable]


def event_body(payload: dict[str, Any], action_id: str) -> dict[str, Any]:
    def utc(value: str) -> str:
        return datetime.fromisoformat(value).astimezone(timezone.utc).replace(tzinfo=None).isoformat()

    body: dict[str, Any] = {
        "subject": payload["subject"],
        "body": {"contentType": "text", "content": payload["description"]},
        "start": {"dateTime": utc(payload["start"]), "timeZone": "UTC"},
        "end": {"dateTime": utc(payload["end"]), "timeZone": "UTC"},
        "attendees": [
            {"emailAddress": {"address": a["email"], "name": a["name"] or a["email"]}, "type": "required"}
            for a in payload["attendees"]
        ],
        # Graph de-duplicates creates with the same transactionId, so a retry can't double-book.
        "transactionId": action_id,
    }
    if payload["teams"]:
        body["isOnlineMeeting"] = True
        body["onlineMeetingProvider"] = "teamsForBusiness"
    if payload["location"]:
        body["location"] = {"displayName": payload["location"]}
    return body


def create_event_kind(graph: GraphSource) -> ActionKind:
    def execute(payload: dict[str, Any], action_id: str) -> dict[str, Any]:
        # Creating an event with attendees sends the invitations.
        event = graph.post(f"/users/{graph.mailbox}/events", event_body(payload, action_id))
        return {
            "event_id": event.get("id"),
            "web_link": event.get("webLink"),
            "join_url": (event.get("onlineMeeting") or {}).get("joinUrl"),
        }

    return ActionKind(KIND, execute)


def create_event_tool(graph: GraphSource, actions: Actions) -> Tool:
    def propose(args: dict[str, Any]) -> dict[str, Any]:
        payload = validate_event(args)
        conflicts, unchecked = find_conflicts(graph, payload)
        payload["conflicts"] = conflicts
        action = actions.propose(KIND, summarize_event(payload, conflicts, unchecked), payload)
        result = public_action(action)
        if action["status"] == "pending":
            result["note"] = ("Not booked yet. Dave sees an approval card; tell him it's ready for his "
                              "approval. Never say it's booked.")
        return result

    return Tool(
        spec=ToolSpec(
            name="create_event",
            description=(
                "Propose a meeting on Dave's calendar (sends invites once Dave approves; Teams link by default). "
                "Returns pending approval: it is NOT booked until Dave approves the card. "
                "Attendees must be email addresses from find_person."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "start": {"type": "string", "description": "Dave's local time, YYYY-MM-DDTHH:MM"},
                    "duration_minutes": {"type": "integer", "description": "Default 30"},
                    "attendees": {
                        "type": "array",
                        "items": {"type": "object", "properties": {"email": {"type": "string"}, "name": {"type": "string"}},
                                  "required": ["email"]},
                    },
                    "teams": {"type": "boolean", "description": "Add a Teams link (default true)"},
                    "location": {"type": "string"},
                    "description": {"type": "string", "description": "Short agenda for the invite body"},
                },
                "required": ["subject", "start"],
            },
        ),
        handler=propose,
    )
