"""Mutual free time: when are Dave and the other attendees all free?

Graph `getSchedule` returns free/busy for anyone in TAG's tenant. Outside
people usually come back with an error (no visibility); their times are then
unknown and the result says so, rather than pretending they're free.
"""

from __future__ import annotations

from datetime import time, timedelta, timezone
from typing import Any, Protocol

from agent.calendar import _default, day_range, fmt_local, find_free_slots, local_zone, parse_event
from agent.tools import Tool
from llm.provider import ToolSpec


class GraphSource(Protocol):
    mailbox: str

    def post(self, path: str, body: dict[str, Any], headers: dict[str, str] | None = None) -> dict[str, Any]: ...


def get_busy(graph: GraphSource, people: list[str], start, end) -> tuple[list, list[dict[str, str]]]:
    """Busy blocks for everyone combined, plus who couldn't be checked."""
    tz = local_zone()
    # Everything in UTC both ways; parse_event converts back to Dave's zone.
    resp = graph.post(f"/users/{graph.mailbox}/calendar/getSchedule", {
        "schedules": people,
        "startTime": {"dateTime": _utc_naive(start), "timeZone": "UTC"},
        "endTime": {"dateTime": _utc_naive(end), "timeZone": "UTC"},
        "availabilityViewInterval": 15,
    }, headers={"Prefer": 'outlook.timezone="UTC"'})

    busy, unavailable = [], []
    for schedule in resp.get("value", []):
        if schedule.get("error"):
            unavailable.append({
                "email": schedule.get("scheduleId", ""),
                "reason": schedule["error"].get("message") or schedule["error"].get("responseCode", "unavailable"),
            })
            continue
        for item in schedule.get("scheduleItems") or []:
            busy.append(parse_event({"start": item["start"], "end": item["end"], "showAs": item.get("status")}, tz))
    return busy, unavailable


def _utc_naive(value) -> str:
    return value.astimezone(timezone.utc).replace(tzinfo=None).isoformat()


def scheduling_tools(graph: GraphSource, memory: Any = None) -> list[Tool]:
    def mutual_time(args: dict[str, Any]) -> dict[str, Any]:
        attendees = [a.strip().lower() for a in args.get("attendees") or []]
        bad = [a for a in attendees if "@" not in a]
        if bad:
            raise ValueError(f"Not email addresses: {bad}. Use find_person first.")
        people = [graph.mailbox.lower()] + [a for a in attendees if a != graph.mailbox.lower()]

        tz = local_zone()
        first, last, start, end = day_range(args["start_date"], args["end_date"], tz)
        busy, unavailable = get_busy(graph, people, start, end)
        slots = find_free_slots(
            busy, first, last,
            timedelta(minutes=int(args.get("duration_minutes") or _default(memory, "meeting_minutes", "30"))), tz,
            day_start=time.fromisoformat(args.get("earliest") or _default(memory, "day_start", "08:00")),
            day_end=time.fromisoformat(args.get("latest") or _default(memory, "day_end", "17:00")),
        )
        result: dict[str, Any] = {
            "timezone": str(tz),
            "checked": [p for p in people if p not in {u["email"].lower() for u in unavailable}],
            "free_windows": [{"from": fmt_local(a), "to": fmt_local(b)} for a, b in slots],
        }
        if unavailable:
            result["could_not_check"] = unavailable
            result["note"] = ("These windows ignore the calendars listed in could_not_check "
                              "(usually people outside TAG). Their availability must be confirmed by asking them.")
        return result

    return [
        Tool(
            spec=ToolSpec(
                name="find_mutual_time",
                description=(
                    "Find windows when Dave AND the given attendees are all free (weekdays, working hours). "
                    "Attendees must be email addresses from find_person. Works for TAG staff; for outside "
                    "people their calendar usually can't be seen and the result says so."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "attendees": {"type": "array", "items": {"type": "string"},
                                      "description": "Email addresses (Dave is included automatically)"},
                        "start_date": {"type": "string", "description": "First day, YYYY-MM-DD"},
                        "end_date": {"type": "string", "description": "Last day (inclusive), YYYY-MM-DD"},
                        "duration_minutes": {"type": "integer", "description": "Meeting length; omit to use Dave's default"},
                        "earliest": {"type": "string", "description": "Day start HH:MM; omit to use Dave's preference"},
                        "latest": {"type": "string", "description": "Day end HH:MM; omit to use Dave's preference"},
                    },
                    "required": ["attendees", "start_date", "end_date"],
                },
            ),
            handler=mutual_time,
        )
    ]
