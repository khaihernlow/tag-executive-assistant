from datetime import date

import pytest

from agent.opportunities import Directory
from agent.quotes import find_quotes, quote_tools, quote_totals
from tests.test_opportunities import COMPANIES, RESOURCES, STAGE_PICKLIST, matches

DAVE = 500
TODAY = date(2026, 10, 11)
OPPS = [{"id": 100, "title": "Core MSP", "companyID": 1, "ownerResourceID": DAVE, "stage": 14, "status": 1},
        {"id": 101, "title": "Laptops", "companyID": 2, "ownerResourceID": 501, "stage": 12, "status": 1}]
QUOTES = [
    {"id": 1, "name": "Core Managed Services", "quoteNumber": 2156, "companyID": 1, "opportunityID": 100, "approvalStatus": 2,
     "createDate": "2026-10-07T20:00:00Z", "expirationDate": "2026-10-09T00:00:00Z", "primaryQuote": True},
    {"id": 2, "name": "2 New Laptops", "quoteNumber": 2158, "companyID": 2, "opportunityID": 101, "approvalStatus": 3,
     "createDate": "2026-10-08T17:00:00Z", "expirationDate": "2026-10-15T00:00:00Z", "primaryQuote": True},
    {"id": 3, "name": "Old firewall", "quoteNumber": 1001, "companyID": 2, "opportunityID": 101, "approvalStatus": 4,
     "createDate": "2025-01-08T17:00:00Z", "expirationDate": "2025-02-15T00:00:00Z", "primaryQuote": False},
]
ITEMS = [
    {"quoteID": 1, "name": "Managed Services Core", "quantity": 85, "unitPrice": 115, "unitDiscount": 0, "lineDiscount": 0, "periodType": 2},
    {"quoteID": 1, "name": "One-Time Onboarding Fee", "quantity": 1, "unitPrice": 9775, "unitDiscount": 0, "lineDiscount": 0, "periodType": 1},
    {"quoteID": 1, "name": "Optional backup", "quantity": 1, "unitPrice": 500, "periodType": 2, "isOptional": True},
    {"quoteID": 2, "name": "Laptop", "quantity": 2, "unitPrice": 1450, "unitDiscount": 50, "lineDiscount": 100, "periodType": 1},
    {"quoteID": 2, "name": "Yearly warranty", "quantity": 2, "unitPrice": 120, "periodType": 5},
]


class FakeAutotask:
    tables = {"Quotes": QUOTES, "QuoteItems": ITEMS, "Opportunities": OPPS, "Companies": COMPANIES, "Resources": RESOURCES}

    def _api_url(self, path):
        return path

    def _request_json(self, method, url, **kw):
        return {"fields": [{"name": "stage", "picklistValues": STAGE_PICKLIST}]}

    def query(self, entity, filters, include_fields=None, max_records=None):
        rows = []
        for r in self.tables[entity]:
            ok = all(r.get(f["field"], "") >= f["value"] if f["op"] == "gte" else matches(r, f) for f in filters)
            if ok:
                rows.append(r)
        return rows


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("AUTOTASK_DAVE_RESOURCE_ID", str(DAVE))
    monkeypatch.setenv("ASSISTANT_TIMEZONE", "America/New_York")


def test_totals_split_one_time_and_monthly_and_skip_optional_lines():
    assert quote_totals([i for i in ITEMS if i["quoteID"] == 1]) == {"one_time": 9775, "monthly": 9775}
    # 2 x (1450 - 50) - 100 one-time; 2 x 120 a year is 20 a month
    assert quote_totals([i for i in ITEMS if i["quoteID"] == 2]) == {"one_time": 2700, "monthly": 20}


def test_recent_quotes_newest_first_with_status_and_expiry():
    at = FakeAutotask()
    result = find_quotes(at, Directory(at), today=TODAY)
    assert [q["quote_number"] for q in result["quotes"]] == [2158, 2156]   # the 2025 one is outside 90 days
    laptops, core = result["quotes"]
    assert laptops["status"] == "Approved" and not laptops["expired"] and laptops["company"] == "Northwind Dental"
    assert core["status"] == "Waiting on client" and core["expired"] and core["expires"] == "Oct 9, 2026"
    assert core["opportunity"] == "Core MSP" and core["stage"] == "Proposal / SOW presented" and core["owner"] == "Dave Vener"
    assert core["lines"][0] == "85 x Managed Services Core"
    assert core["link"].endswith("OpportunityID=100")


def test_filters_company_mine_and_status():
    at = FakeAutotask()
    d = Directory(at)
    assert [q["quote_number"] for q in find_quotes(at, d, company_id=2, today=TODAY)["quotes"]] == [2158, 1001]
    assert [q["quote_number"] for q in find_quotes(at, d, mine=True, today=TODAY)["quotes"]] == [2156]
    assert [q["quote_number"] for q in find_quotes(at, d, company_id=2, status="Declined", today=TODAY)["quotes"]] == [1001]
    with pytest.raises(ValueError, match="status must be"):
        find_quotes(at, d, status="pending", today=TODAY)
    assert quote_tools(at, d)[0].spec.name == "find_quotes"


def test_highlights_separate_live_from_expired():
    at = FakeAutotask()
    h = find_quotes(at, Directory(at), today=TODAY)["highlights"]
    assert (h["open"], h["live"], h["expired"]) == (1, 0, 1)   # the approved laptop quote isn't open
    assert h["biggest_live"] == [] and h["expiring_within_7_days"] == []
    assert h["biggest_expired"] == ["Northwind Savings Bank: Core Managed Services ($9,775 + $9,775/mo), expired Oct 9, 2026"]
