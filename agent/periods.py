"""Reporting periods in code: "last quarter" -> Jul 1 to Sep 30.

agent.when reads scheduling phrases (forward-looking: "next week", "Thursday at 2"). Questions
about records look back as often as forward: "deals created last month", "companies we haven't
talked to in 6 months", "closing this quarter". The model quotes the phrase; this works out the
dates, so the same words always mean the same days.
"""

from __future__ import annotations

import re
from calendar import monthrange
from datetime import date, timedelta
from typing import Any

MONTHS = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
UNITS = {"day": 1, "week": 7, "month": 30, "quarter": 91, "year": 365}
COUNTS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
          "nine": 9, "ten": 10, "twelve": 12, "eighteen": 18, "couple of": 2, "few": 3}


def _month_end(year: int, month: int) -> date:
    return date(year, month, monthrange(year, month)[1])


def _add_months(day: date, months: int) -> date:
    index = day.year * 12 + day.month - 1 + months
    year, month = divmod(index, 12)
    return date(year, month + 1, min(day.day, monthrange(year, month + 1)[1]))


def _quarter(year: int, q: int) -> tuple[date, date]:
    start = date(year, 3 * (q - 1) + 1, 1)
    return start, _month_end(year, 3 * q)


def _unit_span(unit: str, today: date, offset: int) -> tuple[date, date]:
    """The calendar week/month/quarter/year `offset` away from the current one."""
    if unit == "day":
        d = today + timedelta(days=offset)
        return d, d
    if unit == "week":
        start = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)
        return start, start + timedelta(days=6)
    if unit == "month":
        first = _add_months(today.replace(day=1), offset)
        return first, _month_end(first.year, first.month)
    if unit == "quarter":
        q = (today.month - 1) // 3 + offset
        year, q = today.year + q // 4, q % 4 + 1
        return _quarter(year, q)
    return date(today.year + offset, 1, 1), date(today.year + offset, 12, 31)


def _count(text: str) -> int | None:
    text = text.strip()
    if text.isdigit():
        return int(text)
    return COUNTS.get(text)


def _a_date(text: str, today: date, lean: str = "past") -> tuple[date, date] | None:
    """One date or month: '2026-10-01', 'Oct 1', 'October 1 2026', 'September', 'June 2025'."""
    text = text.strip().strip(",.").lower()
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return d, d
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?", text)
    if m:
        year = int(m.group(3)) if m.group(3) else today.year
        year += 2000 if year < 100 else 0
        d = date(year, int(m.group(1)), int(m.group(2)))
        return d, d
    m = re.fullmatch(r"([a-z]{3,9})\.?(?:\s+(\d{1,2})(?:st|nd|rd|th)?)?(?:,?\s+(\d{4}))?", text)
    if m and m.group(1)[:3] in MONTHS and (len(m.group(1)) == 3 or m.group(1).startswith(
            ("january", "february", "march", "april", "may", "june", "july", "august", "september", "sept",
             "october", "november", "december"))):
        month = MONTHS[m.group(1)[:3]]
        year = int(m.group(3)) if m.group(3) else today.year
        if not m.group(3):
            # "September" said in October means the one just gone when looking back (when deals were
            # created); "February" means the coming one when looking ahead (when deals close)
            if lean == "past" and month > today.month:
                year -= 1
            elif lean == "future" and month < today.month:
                year += 1
        if m.group(2):
            d = date(year, month, int(m.group(2)))
            return d, d
        return date(year, month, 1), _month_end(year, month)
    m = re.fullmatch(r"(\d{4})", text)
    if m:
        return date(int(text), 1, 1), date(int(text), 12, 31)
    return None


def period(phrase: str, today: date, lean: str = "past") -> dict[str, Any] | None:
    """A phrase -> {"from": date|None, "to": date|None, "label"}, both ends inclusive; None if not understood.
    An open end (None) means 'any time before' or 'any time since'. `lean` settles a bare month or
    date: "past" for when something happened, "future" for when something is due."""
    text = " ".join(phrase.lower().replace("’", "'").split())
    text = re.sub(r"^(?:in|during|for|over|within|from)\s+(?=the\s|this|last|next|past|q\d)", "", text)
    text = re.sub(r"^the\s+", "", text)

    def out(start: date | None, end: date | None) -> dict[str, Any]:
        label = (f"{start:%b %d, %Y} to {end:%b %d, %Y}" if start and end else
                 f"since {start:%b %d, %Y}" if start else f"before {end + timedelta(days=1):%b %d, %Y}" if end else "any time")
        return {"from": start, "to": end, "label": label.replace(" 0", " ")}

    fixed = {"today": (today, today), "yesterday": (today - timedelta(days=1),) * 2,
             "tomorrow": (today + timedelta(days=1),) * 2}
    if text in fixed:
        return out(*fixed[text])
    if text in ("ytd", "year to date", "this year so far", "so far this year"):
        return out(date(today.year, 1, 1), today)
    if text in ("qtd", "quarter to date", "this quarter so far"):
        return out(_unit_span("quarter", today, 0)[0], today)
    if text in ("mtd", "month to date", "this month so far"):
        return out(today.replace(day=1), today)

    m = re.fullmatch(r"(?:the\s+)?end\s+of\s+(?:the\s+)?(?:(this|next|last)\s+)?(week|month|quarter|year)", text)
    if m:
        end = _unit_span(m.group(2), today, {"next": 1, "last": -1}.get(m.group(1) or "", 0))[1]
        return out(end, end)

    m = re.fullmatch(r"(this|last|next|previous|past|coming|current)\s+(day|week|month|quarter|year)", text)
    if m:
        offset = {"this": 0, "current": 0, "last": -1, "previous": -1, "past": -1, "next": 1, "coming": 1}[m.group(1)]
        return out(*_unit_span(m.group(2), today, offset))

    # "last 30 days", "past 6 months", "the last two weeks", "next 90 days": a rolling window to/from today
    m = re.fullmatch(r"(last|past|next|coming|previous)\s+(\d+|[a-z]+(?: of)?)\s+(day|week|month|quarter|year)s?", text)
    if m and _count(m.group(2)):
        n, unit = _count(m.group(2)), m.group(3)
        if m.group(1) in ("next", "coming"):
            end = _add_months(today, n * {"month": 1, "quarter": 3, "year": 12}[unit]) if unit in ("month", "quarter", "year") \
                else today + timedelta(days=n * UNITS[unit])
            return out(today, end)
        start = _add_months(today, -n * {"month": 1, "quarter": 3, "year": 12}[unit]) if unit in ("month", "quarter", "year") \
            else today - timedelta(days=n * UNITS[unit])
        return out(start, today)

    # "more than 6 months ago", "over a year ago", "6 months ago or more": an open start
    m = re.fullmatch(r"(?:more than|over|at least|longer than)?\s*(\d+|[a-z]+(?: of)?)\s+(day|week|month|quarter|year)s?\s+ago(?: or (?:more|longer|earlier))?", text)
    if m and _count(m.group(1)):
        n, unit = _count(m.group(1)), m.group(2)
        cut = _add_months(today, -n * {"month": 1, "quarter": 3, "year": 12}[unit]) if unit in ("month", "quarter", "year") \
            else today - timedelta(days=n * UNITS[unit])
        if text.startswith(("more", "over", "at least", "longer")) or text.endswith(("more", "longer", "earlier")):
            return out(None, cut - timedelta(days=1))
        return out(cut, cut)

    m = re.fullmatch(r"q([1-4])(?:\s*(?:of\s+)?'?(\d{2,4}))?", text)
    if m:
        year = int(m.group(2)) if m.group(2) else today.year
        year += 2000 if year < 100 else 0
        return out(*_quarter(year, int(m.group(1))))

    m = re.fullmatch(r"(since|after|from|starting)\s+(.+)", text)
    if m:
        when = period(m.group(2), today, lean) or (lambda d: d and out(*d))(_a_date(m.group(2), today, lean))
        if when and when["from"]:
            start = when["from"] if m.group(1) != "after" else (when["to"] or when["from"]) + timedelta(days=1)
            return out(start, None)
    m = re.fullmatch(r"(before|until|till|up to|prior to|by)\s+(.+)", text)
    if m:
        when = period(m.group(2), today, lean) or (lambda d: d and out(*d))(_a_date(m.group(2), today, lean))
        if when and (when["from"] or when["to"]):
            if m.group(1) in ("until", "till", "up to", "by"):
                return out(None, when["to"] or when["from"])
            return out(None, (when["from"] or when["to"]) - timedelta(days=1))
    m = re.fullmatch(r"(?:between\s+)?(.+?)\s+(?:and|to|through|-|–)\s+(.+)", text)
    if m:
        a, b = _a_date(m.group(1), today, lean), _a_date(m.group(2), today, lean)
        if a and b and a[0] <= b[1]:
            return out(a[0], b[1])

    one = _a_date(text, today, lean)
    if one:
        return out(*one)
    return None
