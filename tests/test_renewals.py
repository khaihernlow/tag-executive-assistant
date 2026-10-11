from datetime import date

import pytest

from agent.actions import Actions
from agent.opportunities import Directory, opportunity_kinds
from agent.renewals import propose_renewal, renewal_tools, upcoming_renewals
from store.db import Store
from tests.test_opportunities import COMPANIES, FakeAutotask, STAGE_PICKLIST

TODAY = date(2026, 10, 11)
CATEGORIES = [{"value": "1", "label": "Standard", "isActive": True}, {"value": "102", "label": "Renewals", "isActive": True}]

CONTRACTS = [
    {"contract_id": 900, "company_id": "1", "customer": "Northwind Savings Bank", "contract_name": "Managed Services - Complete",
     "category": "Managed Service", "billing": "Monthly", "start_date": date(2025, 10, 31), "end_date": date(2026, 10, 30),
     "needs_review": "Yes", "period_value": 4200},
    {"contract_id": 901, "company_id": "2", "customer": None, "contract_name": "Voice Maintenance - Core",
     "category": "Maintenance", "billing": "Yearly", "start_date": date(2023, 12, 13), "end_date": date(2026, 12, 13),
     "needs_review": "No", "period_value": 2400},
]


class FakeDB:
    def __init__(self):
        self.calls = []

    def rows(self, sql, params=None):
        self.calls.append(params)
        if "count(*)" in sql:
            return [{"n": 187}]
        if "contract_id = %(contract_id)s" in sql:
            return [c for c in CONTRACTS if c["contract_id"] == params["contract_id"]]
        return [c for c in CONTRACTS if params["today"] <= c["end_date"] <= params["until"]]


class FakeAutotaskWithCategories(FakeAutotask):
    def _request_json(self, method, url, **kw):
        return {"fields": [{"name": "stage", "picklistValues": STAGE_PICKLIST},
                           {"name": "opportunityCategoryID", "picklistValues": CATEGORIES}]}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("AUTOTASK_DAVE_RESOURCE_ID", "500")
    monkeypatch.setenv("ASSISTANT_TIMEZONE", "America/New_York")


def setup():
    at = FakeAutotaskWithCategories()
    return at, Directory(at), Actions(Store(":memory:"), opportunity_kinds(at), auto=set())


def test_upcoming_renewals_soonest_first_with_monthly_value_and_names():
    _, directory, _ = setup()
    result = upcoming_renewals(FakeDB(), directory, days=90, today=TODAY)
    assert [r["contract_id"] for r in result["renewals"]] == [900, 901]
    first, second = result["renewals"]
    assert first["customer"] == "Northwind Savings Bank" and first["needs_review"] and first["days_left"] == 19
    assert first["monthly_value"] == 4200 and first["term_months"] == 12
    assert second["customer"] == "Northwind Dental"   # not in Matt's customer list: looked up in Autotask
    assert second["monthly_value"] == 200             # a yearly line, per month
    assert result["monthly_value"] == 4400
    assert "187 more are still marked Active past their end date" in result["note"]


def test_window_and_customer_filters():
    _, directory, _ = setup()
    assert upcoming_renewals(FakeDB(), directory, days=30, today=TODAY)["count"] == 1
    assert [r["contract_id"] for r in upcoming_renewals(FakeDB(), directory, customer="dental", today=TODAY)["renewals"]] == [901]


def test_starting_a_renewal_is_an_opportunity_in_the_renewals_category():
    at, directory, actions = setup()
    slip = propose_renewal(FakeDB(), directory, actions, 900, today=TODAY)
    assert slip["status"] == "pending" and not at.created
    assert slip["display"]["title"] == "Renewal: Managed Services - Complete"
    assert ["Category", "Renewals"] in slip["display"]["rows"]
    assert slip["display"]["values"]["close_date"] == "2026-10-30"
    assert "$4,200.00/month" in slip["display"]["description"]
    actions.approve(slip["action_id"], decided_by="test")
    body = at.created[0][1]
    assert body["opportunityCategoryID"] == 102 and body["companyID"] == 1
    assert body["projectedCloseDate"] == "2026-10-30T00:00:00Z"


def test_unknown_contract_and_tools():
    _, directory, actions = setup()
    with pytest.raises(ValueError, match="find_renewals"):
        propose_renewal(FakeDB(), directory, actions, 12345, today=TODAY)
    assert {t.spec.name for t in renewal_tools(FakeDB(), directory, actions)} == {"find_renewals", "start_renewal"}


def test_renewal_highlights_are_counted_in_code():
    _, directory, _ = setup()
    h = upcoming_renewals(FakeDB(), directory, days=90, today=TODAY)["highlights"]
    assert h["biggest"][0].startswith("Northwind Savings Bank: Managed Services - Complete ($4,200/mo")
    assert h["needs_review_count"] == 1 and len(h["flagged_needs_review"]) == 1
    assert h["ending_within_30_days"] == {"count": 1, "monthly_value": 4200}
