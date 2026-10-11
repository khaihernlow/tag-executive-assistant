"""Scheduling phrases to dates, in code.

Meeting requests say "next week", "Thursday at 2", "October 22 at 11:00 AM".
The model quotes the phrase as written; this turns it into dates against the
day the email arrived. When this understands the phrase, it wins over the
model's own arithmetic; when it doesn't, the model's dates stand.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from typing import Any

WEEKDAYS = {"monday": 0, "mon": 0, "tuesday": 1, "tues": 1, "tue": 1, "wednesday": 2, "wed": 2,
            "thursday": 3, "thurs": 3, "thur": 3, "thu": 3, "friday": 4, "fri": 4, "saturday": 5, "sat": 5,
            "sunday": 6, "sun": 6}
MONTHS = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "a": 1, "a couple of": 2, "couple of": 2}


def _week_of(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _time(text: str) -> time | None:
    m = re.search(r"\b(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)\b", text) or \
        re.search(r"\b(?:at|@)\s*(\d{1,2})(?::(\d{2}))?\b(?!\s*/)", text) or \
        re.search(r"\b(\d{1,2}):(\d{2})\b()", text)
    if not m:
        if "noon" in text:
            return time(12, 0)
        return None
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    suffix = ((m.group(3) if m.re.groups >= 3 else "") or "").replace(".", "")
    if hour > 23 or minute > 59:
        return None
    if suffix == "pm" and hour < 12:
        hour += 12
    elif suffix == "am" and hour == 12:
        hour = 0
    elif not suffix and 1 <= hour <= 6:
        hour += 12  # "at 2" in a business email means the afternoon
    return time(hour, minute)


def _date(text: str, received: date) -> date | None:
    """An explicit date: 'October 22', 'Oct 22nd', '10/22'. The year is the next one it falls in."""
    m = re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b", text)
    month = day = None
    if m:
        month, day = MONTHS[m.group(1)], int(m.group(2))
    else:
        m = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", text)
        if m:
            month, day = int(m.group(1)), int(m.group(2))
    if not month:
        return None
    for year in (received.year, received.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            return None
        if candidate >= received - timedelta(days=1):
            return candidate
    return None


def resolve(phrase: str, received: date) -> dict[str, Any]:
    """{'earliest_date', 'latest_date', and 'proposed_start' when a day and time are both given}, or {}."""
    text = " ".join((phrase or "").lower().split())
    if not text:
        return {}
    day: date | None = _date(text, received)
    span: tuple[date, date] | None = None
    if day is None:
        weekday = next((WEEKDAYS[w] for w in re.findall(r"[a-z]+", text) if w in WEEKDAYS), None)
        if "tomorrow" in text:
            day = received + timedelta(days=1)
        elif weekday is not None:
            this_week = _week_of(received)
            if "next" in text.split() and re.search(r"\bnext\s+\w+day\b", text):
                day = this_week + timedelta(weeks=1, days=weekday)   # "next Thursday": next week's
            else:
                day = this_week + timedelta(days=weekday)
                if day <= received:
                    day += timedelta(weeks=1)                        # "Thursday": the coming one
        elif "next week" in text:
            start = _week_of(received) + timedelta(weeks=1)
            if "early" in text:
                span = (start, start + timedelta(days=1))
            elif "end of" in text or "late" in text:
                span = (start + timedelta(days=3), start + timedelta(days=4))
            else:
                span = (start, start + timedelta(days=4))
        elif "this week" in text or "end of the week" in text or "end of this week" in text:
            friday = _week_of(received) + timedelta(days=4)
            span = (max(received, friday - timedelta(days=1)) if "end of" in text else received, friday)
        else:
            m = re.search(r"\bin\s+(\d+|one|two|three|four|a couple of|couple of|a)\s+weeks?\b", text)
            if m:
                weeks = int(m.group(1)) if m.group(1).isdigit() else NUMBERS[m.group(1)]
                start = _week_of(received) + timedelta(weeks=weeks)
                span = (start, start + timedelta(days=4))
    if day is not None:
        out: dict[str, Any] = {"earliest_date": day.isoformat(), "latest_date": day.isoformat()}
        at = _time(text)
        if at:
            out["proposed_start"] = datetime.combine(day, at).strftime("%Y-%m-%dT%H:%M")
        return out
    if span:
        return {"earliest_date": span[0].isoformat(), "latest_date": span[1].isoformat()}
    return {}
