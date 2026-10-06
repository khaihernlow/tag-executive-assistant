"""Calendar logic and tools: reading Dave's events and finding free time.

Graph returns times in UTC; everything shown to the model is converted to
Dave's local zone (ASSISTANT_TIMEZONE) so it never has to do timezone math.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from agent.tools import Tool
from llm.provider import ToolSpec

# Graph "showAs" values that do not block time.
_NON_BLOCKING = {"free", "workingElsewhere"}


def local_zone() -> ZoneInfo:
    return ZoneInfo(os.environ.get("ASSISTANT_TIMEZONE", "America/New_York"))


class CalendarSource(Protocol):
    def calendar_view(self, start: datetime, end: datetime) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class Event:
    subject: str
    start: datetime
    end: datetime
    all_day: bool
    show_as: str
    location: str
    attendees: list[str]
    organizer: str
    online: bool
    join_url: str = ""

    @property
    def blocks_time(self) -> bool:
        return self.show_as not in _NON_BLOCKING


def parse_event(raw: dict[str, Any], tz: ZoneInfo) -> Event:
    return Event(
        subject=raw.get("subject") or "(no subject)",
        start=_graph_time(raw["start"], tz),
        end=_graph_time(raw["end"], tz),
        all_day=bool(raw.get("isAllDay")),
        show_as=raw.get("showAs") or "busy",
        location=(raw.get("location") or {}).get("displayName") or "",
        attendees=[
            (a.get("emailAddress") or {}).get("name") or (a.get("emailAddress") or {}).get("address", "")
            for a in raw.get("attendees") or []
        ],
        organizer=((raw.get("organizer") or {}).get("emailAddress") or {}).get("name", ""),
        online=bool(raw.get("isOnlineMeeting")),
        join_url=(raw.get("onlineMeeting") or {}).get("joinUrl") or "",
    )


def _graph_time(value: dict[str, Any], tz: ZoneInfo) -> datetime:
    # Without a Prefer: outlook.timezone header Graph answers in UTC, e.g.
    # {"dateTime": "2026-10-13T14:00:00.0000000", "timeZone": "UTC"}.
    stamp = value["dateTime"][:19]
    parsed = datetime.fromisoformat(stamp)
    source = timezone.utc if value.get("timeZone", "UTC") == "UTC" else ZoneInfo(value["timeZone"])
    return parsed.replace(tzinfo=source).astimezone(tz)


def find_free_slots(
    events: list[Event],
    start_day: date,
    end_day: date,
    duration: timedelta,
    tz: ZoneInfo,
    day_start: time = time(8, 0),
    day_end: time = time(17, 0),
    include_weekends: bool = False,
) -> list[tuple[datetime, datetime]]:
    """Open windows of at least `duration` inside working hours, inclusive of both days."""
    busy = sorted((e.start, e.end) for e in events if e.blocks_time)
    slots: list[tuple[datetime, datetime]] = []
    day = start_day
    while day <= end_day:
        if include_weekends or day.weekday() < 5:
            cursor = datetime.combine(day, day_start, tz)
            close = datetime.combine(day, day_end, tz)
            for busy_start, busy_end in busy:
                if busy_end <= cursor or busy_start >= close:
                    continue
                if busy_start - cursor >= duration:
                    slots.append((cursor, busy_start))
                cursor = max(cursor, busy_end)
            if close - cursor >= duration:
                slots.append((cursor, close))
        day += timedelta(days=1)
    return slots


def fmt_local(value: datetime) -> str:
    return value.strftime("%a %b %d %I:%M %p").replace(" 0", " ")


def _parse_day(value: str) -> date:
    return date.fromisoformat(value[:10])


def day_range(start_date: str, end_date: str, tz: ZoneInfo) -> tuple[date, date, datetime, datetime]:
    first, last = _parse_day(start_date), _parse_day(end_date)
    if last < first:
        raise ValueError("end_date is before start_date")
    if (last - first).days > 62:
        raise ValueError("Range too large; ask for at most about two months at a time.")
    start = datetime.combine(first, time.min, tz)
    end = datetime.combine(last + timedelta(days=1), time.min, tz)
    return first, last, start, end


def calendar_tools(source: CalendarSource) -> list[Tool]:
    def list_events(args: dict[str, Any]) -> dict[str, Any]:
        tz = local_zone()
        _, _, start, end = day_range(args["start_date"], args["end_date"], tz)
        events = [parse_event(raw, tz) for raw in source.calendar_view(start, end) if not raw.get("isCancelled")]
        return {
            "timezone": str(tz),
            "events": [
                {
                    "subject": e.subject,
                    "start": "all day " + e.start.strftime("%a %b %d") if e.all_day else fmt_local(e.start),
                    "end": fmt_local(e.end),
                    "show_as": e.show_as,
                    "location": e.location,
                    "online": e.online,
                    "organizer": e.organizer,
                    "attendees": e.attendees[:15],
                }
                for e in events
            ],
        }

    def free_time(args: dict[str, Any]) -> dict[str, Any]:
        tz = local_zone()
        first, last, start, end = day_range(args["start_date"], args["end_date"], tz)
        events = [parse_event(raw, tz) for raw in source.calendar_view(start, end) if not raw.get("isCancelled")]
        slots = find_free_slots(
            events,
            first,
            last,
            timedelta(minutes=int(args.get("duration_minutes", 30))),
            tz,
            day_start=time.fromisoformat(args.get("earliest", "08:00")),
            day_end=time.fromisoformat(args.get("latest", "17:00")),
        )
        return {
            "timezone": str(tz),
            "free_windows": [{"from": fmt_local(a), "to": fmt_local(b)} for a, b in slots],
        }

    date_props = {
        "start_date": {"type": "string", "description": "First day, ISO format YYYY-MM-DD"},
        "end_date": {"type": "string", "description": "Last day (inclusive), ISO format YYYY-MM-DD"},
    }
    return [
        Tool(
            spec=ToolSpec(
                name="list_calendar_events",
                description="List Dave's calendar events between two dates (inclusive), in his local time.",
                input_schema={"type": "object", "properties": date_props, "required": ["start_date", "end_date"]},
            ),
            handler=list_events,
        ),
        Tool(
            spec=ToolSpec(
                name="find_free_time",
                description=(
                    "Find open windows on Dave's calendar during working hours on weekdays. "
                    "Each window is at least duration_minutes long; a meeting can start anywhere inside it."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        **date_props,
                        "duration_minutes": {"type": "integer", "description": "Meeting length, default 30"},
                        "earliest": {"type": "string", "description": "Day start HH:MM, default 08:00"},
                        "latest": {"type": "string", "description": "Day end HH:MM, default 17:00"},
                    },
                    "required": ["start_date", "end_date"],
                },
            ),
            handler=free_time,
        ),
    ]


# ── home screen agenda ───────────────────────────────────────────────────────

LOOK_AHEAD_HOUR = 17  # from 5 PM, an empty rest-of-day means "show tomorrow"


def next_working_day(day: date) -> date:
    day += timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def agenda_day(now: datetime, anything_left_today: bool) -> date:
    """Which day the home screen should lead with.

    Today while it still matters; the next working day once today is done
    and it's evening, or on a weekend with nothing left (Fri night -> Mon).
    """
    today = now.date()
    if anything_left_today:
        return today
    if today.weekday() >= 5 or now.hour >= LOOK_AHEAD_HOUR:
        return next_working_day(today)
    return today


_ONLINE_PLACEHOLDERS = {"microsoft teams meeting", "teams meeting", "zoom meeting", "google meet", "online"}


def meeting_place(location: str, join_url: str) -> tuple[str, str]:
    """(physical place to show, join link) from Outlook's location field.

    Outlook stuffs "Microsoft Teams Meeting" and pasted Zoom/Meet links into
    the location. The Join button covers those, so only real places remain.
    """
    place_parts, link = [], join_url
    for part in (p.strip() for p in (location or "").split(";")):
        if not part:
            continue
        if part.lower().startswith(("http://", "https://")):
            link = link or part
        elif part.lower() not in _ONLINE_PLACEHOLDERS:
            place_parts.append(part)
    return "; ".join(place_parts), link
