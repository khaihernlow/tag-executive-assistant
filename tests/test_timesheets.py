from datetime import date, timedelta

import pytest

from agent.timesheets import resolve_week, roster, timesheet_tools, week_report
from store.db import Store

TODAY = date(2026, 10, 8)            # a Thursday
LAST_MONDAY = date(2026, 9, 28)

USER_TYPES = [{"value": "1", "label": "Service Desk User"}, {"value": "2", "label": "Sales"},
              {"value": "3", "label": "API User (system)"}]
PEOPLE = [
    {"id": 1, "firstName": "Riley", "lastName": "Jones", "userType": 1},     # logs 8h every workday
    {"id": 2, "firstName": "Sam", "lastName": "Ortiz", "userType": 1},       # 40h in four days
    {"id": 3, "firstName": "Alex", "lastName": "Kim", "userType": 1},        # stopped logging last week
    {"id": 4, "firstName": "Morgan", "lastName": "Lee", "userType": 2},      # the odd hour
    {"id": 5, "firstName": "Pat", "lastName": "Quinn", "userType": 2},       # sales: never logs
    {"id": 6, "firstName": "Ninja", "lastName": "RMM", "userType": 3},       # system account
    {"id": 7, "firstName": "Jamie", "lastName": "Fox", "userType": 1},       # service desk, never logs
]


def entries():
    out = []
    start = LAST_MONDAY - timedelta(weeks=8)
    for week in range(9):
        monday = start + timedelta(weeks=week)
        for day in range(5):
            d = (monday + timedelta(days=day)).isoformat() + "T00:00:00Z"
            out.append({"resourceID": 1, "dateWorked": d, "hoursWorked": 8})
            if day < 4:
                out.append({"resourceID": 2, "dateWorked": d, "hoursWorked": 10})
            if monday < LAST_MONDAY:
                out.append({"resourceID": 3, "dateWorked": d, "hoursWorked": 8})
        out.append({"resourceID": 4, "dateWorked": monday.isoformat() + "T00:00:00Z", "hoursWorked": 1})
    out.append({"resourceID": 3, "dateWorked": LAST_MONDAY.isoformat() + "T00:00:00Z", "hoursWorked": 2})
    return out


class FakeAutotask:
    def __init__(self):
        self.entries, self.queries = entries(), []

    def _api_url(self, path):
        return path

    def _request_json(self, method, url, **kw):
        return {"fields": [{"name": "userType", "picklistValues": USER_TYPES}]}

    def query(self, entity, filters, include_fields=None, max_records=None):
        self.queries.append(entity)
        if entity == "Resources":
            return PEOPLE
        lo = next(f["value"] for f in filters if f["op"] == "gte")
        hi = next(f["value"] for f in filters if f["op"] == "lte")
        return [e for e in self.entries if lo <= e["dateWorked"][:10] <= hi[:10]]


def test_roster_is_who_logs_time_not_titles():
    team = roster(FakeAutotask(), None, TODAY)
    names = {p["name"]: p["usual"] for p in team["people"].values()}
    assert set(names) == {"Riley Jones", "Sam Ortiz", "Alex Kim", "Morgan Lee"}  # no sales, no system accounts
    assert names["Riley Jones"] == 40 and names["Morgan Lee"] == 1
    assert team["silent"] == ["Jamie Fox"]


def test_last_week_flags_only_full_time_loggers_who_fell_short():
    report = week_report(FakeAutotask(), None, "last", today=TODAY)
    assert report["week"] == "Week of Sep 28" and report["target_hours"] == 35
    flagged = {p["name"]: p["why"] for p in report["people"] if p["flagged"]}
    # Sam's 40 hours in four days is fine; Morgan logs the odd hour and is never held to 35.
    assert flagged == {"Alex Kim": "2h of 35h, nothing on Tue, Wed, Thu, Fri; under half their usual (35h)"}
    assert report["people"][0]["name"] == "Alex Kim"
    morgan = next(p for p in report["people"] if p["name"] == "Morgan Lee")
    assert morgan["occasional"] and not morgan["flagged"]
    assert "Jamie Fox" in report["note"]


def test_a_week_in_progress_is_pro_rated_and_says_so():
    at = FakeAutotask()
    at.entries += [{"resourceID": 1, "dateWorked": f"2026-10-0{d}T00:00:00Z", "hoursWorked": 8} for d in (5, 6, 7)]
    report = week_report(at, None, "this", today=TODAY)
    assert report["week"] == "Week of Oct 5 (so far)"
    assert report["target_hours"] == 21  # Mon-Wed; today isn't over
    riley = next(p for p in report["people"] if p["name"] == "Riley Jones")
    assert not riley["flagged"] and riley["by_day"] == {"Mon": 8, "Tue": 8, "Wed": 8, "Thu": 0}
    assert "enter the whole week at the end" in report["note"]


def test_one_persons_week_and_unknown_names():
    report = week_report(FakeAutotask(), None, "2026-09-30", person="riley", today=TODAY)
    assert [p["name"] for p in report["people"]] == ["Riley Jones"]
    with pytest.raises(ValueError, match="No one matching"):
        week_report(FakeAutotask(), None, "last", person="nobody", today=TODAY)


def test_roster_is_cached_for_a_day():
    at, store = FakeAutotask(), Store(":memory:")
    roster(at, store, TODAY)
    roster(at, store, TODAY)
    assert at.queries.count("Resources") == 1


def test_weeks_and_the_tool():
    assert resolve_week("last", TODAY) == LAST_MONDAY
    assert resolve_week("2026-10-10", TODAY) == date(2026, 10, 5)
    [tool] = timesheet_tools(FakeAutotask(), None)
    assert tool.spec.name == "check_time_entries"
