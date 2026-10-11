"""Relative dates worked out in code, so the model never counts days.

The model once said two quotes expired "tomorrow or Tuesday" when both expired
the next day. Every tool result now carries a small guide mapping each date
it mentions to its weekday and distance from today ("Oct 12, 2026" ->
"Monday, tomorrow"). The result's own values are untouched, so cards and code
that parse them are unaffected.
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

_MONTHS = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec"
# "Oct 12, 2026" | "2026-10-12" (alone or inside a timestamp) | "Mon Oct 12" (fmt_local, no year)
_DATES = re.compile(rf"\b(?:({_MONTHS}) (\d{{1,2}}), (\d{{4}})|(\d{{4}})-(\d{{2}})-(\d{{2}})|"
                    rf"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) ({_MONTHS}) (\d{{1,2}})\b)")
MAX_DATES = 60


def today_local() -> date:
    return datetime.now(ZoneInfo(os.environ.get("ASSISTANT_TIMEZONE", "America/New_York"))).date()


def relative(day: date, today: date) -> str:
    days = (day - today).days
    if days == 0:
        return "today"
    if days == 1:
        return "tomorrow"
    if days == -1:
        return "yesterday"
    if abs(days) < 14:
        return f"in {days} days" if days > 0 else f"{-days} days ago"
    if abs(days) < 60:
        weeks = round(abs(days) / 7)
        return f"in {weeks} weeks" if days > 0 else f"{weeks} weeks ago"
    months = round(abs(days) / 30)
    return f"in {months} months" if days > 0 else f"{months} months ago"


def _parse(match: re.Match, today: date) -> date | None:
    try:
        if match.group(1):
            return datetime.strptime(f"{match.group(1)} {match.group(2)} {match.group(3)}", "%b %d %Y").date()
        if match.group(4):
            return date(int(match.group(4)), int(match.group(5)), int(match.group(6)))
        # No year: the nearest such date to today.
        candidates = [datetime.strptime(f"{match.group(7)} {match.group(8)} {y}", "%b %d %Y").date()
                      for y in (today.year - 1, today.year, today.year + 1)]
        return min(candidates, key=lambda d: abs((d - today).days))
    except ValueError:
        return None


def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def date_guide(result: Any, today: date | None = None) -> dict[str, str]:
    """{date as written in the result: "Weekday, relative"} for the dates it mentions."""
    today = today or today_local()
    guide: dict[str, str] = {}
    for text in _strings(result):
        for match in _DATES.finditer(text):
            key = match.group(0)
            if key in guide:
                continue
            day = _parse(match, today)
            if day:
                guide[key] = f"{day.strftime('%A')}, {relative(day, today)}"
            if len(guide) >= MAX_DATES:
                return guide
    return guide
