from datetime import date

import pytest

from agent.when import resolve

RECEIVED = date(2026, 10, 8)  # a Thursday


@pytest.mark.parametrize("phrase, expected", [
    ("next week", {"earliest_date": "2026-10-12", "latest_date": "2026-10-16"}),
    ("early next week", {"earliest_date": "2026-10-12", "latest_date": "2026-10-13"}),
    ("end of next week", {"earliest_date": "2026-10-15", "latest_date": "2026-10-16"}),
    ("sometime this week", {"earliest_date": "2026-10-08", "latest_date": "2026-10-09"}),
    ("in two weeks", {"earliest_date": "2026-10-19", "latest_date": "2026-10-23"}),
    ("tomorrow", {"earliest_date": "2026-10-09", "latest_date": "2026-10-09"}),
    ("Tuesday", {"earliest_date": "2026-10-13", "latest_date": "2026-10-13"}),              # the coming one
    ("Thursday at 2", {"earliest_date": "2026-10-15", "latest_date": "2026-10-15",          # not today
                       "proposed_start": "2026-10-15T14:00"}),
    ("next Tuesday at 10am", {"earliest_date": "2026-10-13", "latest_date": "2026-10-13",
                              "proposed_start": "2026-10-13T10:00"}),
    ("October 22 at 11:00 AM", {"earliest_date": "2026-10-22", "latest_date": "2026-10-22",
                                "proposed_start": "2026-10-22T11:00"}),
    ("10/22 @ 1:30", {"earliest_date": "2026-10-22", "latest_date": "2026-10-22", "proposed_start": "2026-10-22T13:30"}),
    ("Jan 5th", {"earliest_date": "2027-01-05", "latest_date": "2027-01-05"}),              # next year
    ("whenever suits you", {}),
    ("", {}),
])
def test_phrases(phrase, expected):
    assert resolve(phrase, RECEIVED) == expected


def test_scan_prefers_code_dates_over_the_models():
    from agent.requests import with_code_dates

    model = {"kind": "proposes_time", "when_text": "Thursday at 2", "proposed_start": "2026-10-14T14:00",
             "earliest_date": "2026-10-14"}  # the model picked Wednesday
    out = with_code_dates(model, "2026-10-08T14:00:00Z")
    assert out["proposed_start"] == "2026-10-15T14:00" and out["dates_from"] == "code"
    asks = with_code_dates({"kind": "asks_for_times", "when_text": "Thursday at 2"}, "2026-10-08T14:00:00Z")
    assert "proposed_start" not in asks and asks["earliest_date"] == "2026-10-15"
    unknown = {"kind": "asks_for_times", "when_text": "whenever", "earliest_date": "2026-10-12"}
    assert with_code_dates(unknown, "2026-10-08T14:00:00Z") == unknown  # the model's dates stand
