import json
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from agent.calendar import Event, calendar_tools, find_free_slots, parse_event
from agent.tools import ToolRegistry
from llm.provider import ToolCall

NY = ZoneInfo("America/New_York")


def raw_event(subject, start_utc, end_utc, show_as="busy", **extra):
    return {
        "subject": subject,
        "start": {"dateTime": f"{start_utc}.0000000", "timeZone": "UTC"},
        "end": {"dateTime": f"{end_utc}.0000000", "timeZone": "UTC"},
        "showAs": show_as,
        **extra,
    }


def event(start_hour, end_hour, day=date(2026, 10, 13), show_as="busy"):
    return Event("x", datetime.combine(day, time(start_hour), NY), datetime.combine(day, time(end_hour), NY),
                 False, show_as, "", [], "", False)


def test_parse_event_converts_utc_to_local():
    e = parse_event(raw_event("Standup", "2026-10-13T13:00:00", "2026-10-13T13:30:00",
                              attendees=[{"emailAddress": {"name": "Kai", "address": "k@x"}}]), NY)
    assert e.start == datetime(2026, 10, 13, 9, 0, tzinfo=NY)
    assert e.attendees == ["Kai"]


def test_free_slots_skip_busy_and_ignore_free_events():
    slots = find_free_slots(
        [event(9, 10), event(12, 13), event(14, 15, show_as="free")],
        date(2026, 10, 13), date(2026, 10, 13), timedelta(minutes=30), NY,
    )
    hours = [(a.hour, b.hour) for a, b in slots]
    assert hours == [(8, 9), (10, 12), (13, 17)]


def test_free_slots_merge_overlaps_and_drop_short_gaps():
    slots = find_free_slots(
        [event(8, 11), event(10, 12), event(12, 17)],
        date(2026, 10, 13), date(2026, 10, 13), timedelta(minutes=30), NY,
    )
    assert slots == []


def test_free_slots_skip_weekends():
    # 2026-10-17 is a Saturday, 2026-10-19 a Monday
    slots = find_free_slots([], date(2026, 10, 17), date(2026, 10, 19), timedelta(hours=1), NY)
    assert {a.date() for a, _ in slots} == {date(2026, 10, 19)}


class FakeCalendar:
    def __init__(self, events):
        self.events = events
        self.calls = []

    def calendar_view(self, start, end):
        self.calls.append((start, end))
        return self.events


def test_find_free_time_tool_queries_whole_local_days():
    source = FakeCalendar([raw_event("Busy", "2026-10-13T13:00:00", "2026-10-13T20:00:00")])  # 9am-4pm local
    registry = ToolRegistry(calendar_tools(source))
    content, is_error = registry.run(ToolCall("t1", "find_free_time",
                                              {"start_date": "2026-10-13", "end_date": "2026-10-13", "duration_minutes": 60}))

    assert not is_error
    assert json.loads(content)["free_windows"] == [{"from": "Tue Oct 13 8:00 AM", "to": "Tue Oct 13 9:00 AM"},
                                                   {"from": "Tue Oct 13 4:00 PM", "to": "Tue Oct 13 5:00 PM"}]
    start, end = source.calls[0]
    assert start == datetime(2026, 10, 13, 0, 0, tzinfo=NY)
    assert end == datetime(2026, 10, 14, 0, 0, tzinfo=NY)


def test_list_events_skips_cancelled():
    source = FakeCalendar([raw_event("Kept", "2026-10-13T13:00:00", "2026-10-13T14:00:00"),
                           raw_event("Gone", "2026-10-13T15:00:00", "2026-10-13T16:00:00", isCancelled=True)])
    content, _ = ToolRegistry(calendar_tools(source)).run(
        ToolCall("t1", "list_calendar_events", {"start_date": "2026-10-13", "end_date": "2026-10-13"}))
    assert [e["subject"] for e in json.loads(content)["events"]] == ["Kept"]


def test_bad_range_is_reported_as_tool_error():
    content, is_error = ToolRegistry(calendar_tools(FakeCalendar([]))).run(
        ToolCall("t1", "list_calendar_events", {"start_date": "2026-10-13", "end_date": "2026-10-01"}))
    assert is_error and "before" in content


# ── home screen: which day to lead with ──────────────────────────────────────

from agent.calendar import agenda_day, next_working_day


def at(day, hour):
    return datetime(2026, 10, day, hour, 0, tzinfo=NY)


def test_agenda_day_switches_to_tomorrow_only_when_today_is_done_and_evening():
    assert agenda_day(at(5, 20), anything_left_today=False) == date(2026, 10, 6)   # Mon 8 PM, done
    assert agenda_day(at(5, 20), anything_left_today=True) == date(2026, 10, 5)    # late meeting still ahead
    assert agenda_day(at(5, 14), anything_left_today=False) == date(2026, 10, 5)   # 2 PM gap: stay on today


def test_agenda_day_skips_weekends():
    assert agenda_day(at(9, 18), anything_left_today=False) == date(2026, 10, 12)  # Fri evening -> Mon
    assert agenda_day(at(10, 9), anything_left_today=False) == date(2026, 10, 12)  # Sat morning -> Mon
    assert next_working_day(date(2026, 10, 11)) == date(2026, 10, 12)              # Sun -> Mon


from agent.calendar import meeting_place


def test_meeting_place_keeps_real_places_and_turns_links_into_join():
    assert meeting_place("Microsoft Teams Meeting; TAG Conference", "https://teams/x") == ("TAG Conference", "https://teams/x")
    assert meeting_place("Microsoft Teams Meeting", "https://teams/x") == ("", "https://teams/x")
    assert meeting_place("https://us06web.zoom.us/j/123?pwd=abc", "") == ("", "https://us06web.zoom.us/j/123?pwd=abc")
    assert meeting_place("Dave's office", "") == ("Dave's office", "")
