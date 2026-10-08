"""Time-entry oversight: who isn't logging their time in Autotask.

Who gets checked is learned, not guessed from titles: anyone (other than
system/API accounts) who logged time in the last 8 weeks. At TAG that's ~19
people: service desk, project engineers, professional services, and the vCIOs
who log. Sales, finance and admin never log time, so they're never flagged.

Only people who usually log 20+ hours a week are held to a standard; the
occasional loggers (a manager who logs the odd hour) are listed, never flagged.
A full-time logger is flagged when they're under 35 hours (pro-rated for a week
still in progress: 7 per workday so far), naming any workday with nothing
logged, or under half their usual. A missed day alone isn't a flag: several
people log 40 hours in four days. Read-only; Dave asks in chat.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any

from agent.calendar import local_zone
from agent.tools import Tool
from llm.provider import ToolSpec

WEEKLY_TARGET = 35.0
FULL_TIME_MIN = 20.0  # usual hours a week to be held to the target
DAILY_TARGET = WEEKLY_TARGET / 5
ROSTER_WEEKS = 8
ROSTER_STATE = "time_roster"
ROSTER_TTL_HOURS = 24
_SYSTEM_WORDS = ("api", "(system)", "integration", "helpdesk", "automation", "analysis")


def week_of(day: date) -> date:
    return day - timedelta(days=day.weekday())


def resolve_week(value: str, today: date) -> date:
    if value in ("", "this"):
        return week_of(today)
    if value == "last":
        return week_of(today) - timedelta(days=7)
    try:
        return week_of(date.fromisoformat(value[:10]))
    except ValueError as e:
        raise ValueError("week must be 'this', 'last' or a date (YYYY-MM-DD)") from e


def _entries(at: Any, start: date, end: date) -> list[dict[str, Any]]:
    """Time entries with dateWorked in [start, end]."""
    return at.query("TimeEntries", [{"op": "gte", "field": "dateWorked", "value": start.isoformat()},
                                    {"op": "lte", "field": "dateWorked", "value": f"{end.isoformat()}T23:59:59"}],
                    ["resourceID", "dateWorked", "hoursWorked"])


def _people(at: Any) -> dict[int, dict[str, Any]]:
    """Active, human Autotask users: id -> {name, user_type}."""
    fields = at._request_json("GET", at._api_url("Resources/entityInformation/fields"))["fields"]
    types = {str(v["value"]): v["label"] for f in fields if f["name"] == "userType" for v in f.get("picklistValues") or []}
    out = {}
    for r in at.query("Resources", [{"op": "eq", "field": "isActive", "value": True}],
                      ["firstName", "lastName", "userType", "licenseType"]):
        name = f"{r.get('firstName') or ''} {r.get('lastName') or ''}".strip()
        user_type = types.get(str(r.get("userType")), "")
        if any(w in f"{name} {user_type}".lower() for w in _SYSTEM_WORDS):
            continue
        out[r["id"]] = {"name": " ".join(name.split()), "user_type": user_type}
    return out


def roster(at: Any, store: Any, today: date) -> dict[str, Any]:
    """Who logs time, and how much they usually log a week. Cached for a day."""
    raw = store.get_state(ROSTER_STATE) if store is not None else None
    if raw:
        cached = json.loads(raw)
        if datetime.now(timezone.utc) - datetime.fromisoformat(cached["built_at"]) < timedelta(hours=ROSTER_TTL_HOURS):
            return cached
    people = _people(at)
    this_week = week_of(today)
    start = this_week - timedelta(weeks=ROSTER_WEEKS)
    totals: dict[int, float] = defaultdict(float)
    for e in _entries(at, start, this_week - timedelta(days=1)):
        totals[e["resourceID"]] += e.get("hoursWorked") or 0
    loggers = {str(rid): {**info, "usual": round(totals[rid] / ROSTER_WEEKS, 1)}
               for rid, info in people.items() if totals.get(rid, 0) > 0}
    # Service desk accounts that logged nothing at all: likely new or gone.
    silent = sorted(info["name"] for rid, info in people.items()
                    if totals.get(rid, 0) == 0 and "service desk" in info["user_type"].lower())
    built = {"built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "people": loggers, "silent": silent}
    if store is not None:
        store.set_state(ROSTER_STATE, json.dumps(built))
    return built


def week_report(at: Any, store: Any, week: str = "last", person: str = "", today: date | None = None) -> dict[str, Any]:
    today = today or datetime.now(local_zone()).date()
    start = resolve_week(week, today)
    workdays = [start + timedelta(days=i) for i in range(5) if start + timedelta(days=i) <= today]
    if not workdays:
        raise ValueError("That week hasn't started yet.")
    # Today isn't over: it doesn't count as a missed day or towards the target.
    counted = [d for d in workdays if d < today] or workdays
    end = start + timedelta(days=6)
    team = roster(at, store, today)
    by_person: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for e in _entries(at, start, min(end, today)):
        by_person[str(e["resourceID"])][e["dateWorked"][:10]] += e.get("hoursWorked") or 0

    target = round(DAILY_TARGET * len(counted), 1)
    rows = []
    for rid, info in team["people"].items():
        if person and person.lower() not in info["name"].lower():
            continue
        days = by_person.get(rid, {})
        hours = round(sum(days.values()), 1)
        missing = [d.strftime("%a") for d in counted if days.get(d.isoformat(), 0) == 0]
        usual = info["usual"] * len(counted) / 5
        full_time = info["usual"] >= FULL_TIME_MIN
        reasons = []
        if full_time and hours < target:
            reasons.append(f"{hours:g}h of {target:g}h" + (f", nothing on {', '.join(missing)}" if missing else ""))
        if full_time and hours < usual / 2:
            reasons.append(f"under half their usual ({usual:.0f}h)")
        rows.append({
            "name": info["name"], "hours": hours, "missing_days": missing, "usual_week": info["usual"],
            "occasional": not full_time,
            "flagged": bool(reasons), "why": "; ".join(reasons),
            "by_day": {d.strftime("%a"): round(days.get(d.isoformat(), 0), 1) for d in workdays},
        })
    rows.sort(key=lambda r: (not r["flagged"], r["occasional"], r["hours"]))
    if person and not rows:
        raise ValueError(f"No one matching '{person}' logs time in Autotask.")
    label = f"Week of {start.strftime('%b %d').replace(' 0', ' ')}" + (" (so far)" if today <= end else "")
    return {
        "week": label, "target_hours": target, "workdays_counted": len(counted),
        "flagged": sum(r["flagged"] for r in rows), "people": rows,
        "note": (f"Checked everyone who logged Autotask time in the past {ROSTER_WEEKS} weeks; only those who "
                 f"usually log {FULL_TIME_MIN:g}+ hours a week can be flagged. "
                 "Holidays and PTO aren't known, so a missing day may be time off.")
                + (" This week isn't over: several people enter the whole week at the end of it, so they "
                   "look behind until then." if today <= end else "")
                + (f" Service desk accounts with no time at all (new, or gone?): {', '.join(team['silent'])}."
                   if team["silent"] and not person else ""),
    }


def timesheet_tools(at: Any, store: Any) -> list[Tool]:
    return [Tool(spec=ToolSpec(
        name="check_time_entries",
        description=("Who isn't logging their time in Autotask: hours per person for a week, days with nothing "
                     "logged, and who's flagged (under 35h, a missed workday, or under half their usual). "
                     "Only people who normally log time are checked. Give person for one person's week by day."),
        input_schema={"type": "object", "properties": {
            "week": {"type": "string", "description": "'last' (default), 'this', or any date in the week"},
            "person": {"type": "string", "description": "Name, to see just them"}}}),
        handler=lambda a: week_report(at, store, a.get("week") or "last", a.get("person") or ""))]
