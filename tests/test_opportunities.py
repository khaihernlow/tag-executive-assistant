from datetime import date

import pytest

from agent.actions import Actions
from agent.opportunities import (
    Directory, find_opportunities, opportunity_kinds, opportunity_tools, propose_create, propose_update,
)
from store.db import Store

DAVE_ID = 500
TODAY = date(2026, 10, 8)

STAGE_PICKLIST = [
    {"value": "11", "label": "1.  Identification & Discovery", "isActive": True},
    {"value": "12", "label": "2. Business Case Alignment", "isActive": True},
    {"value": "13", "label": "3. Solution Scoping", "isActive": True},
    {"value": "14", "label": "4. Proposal / SOW presented", "isActive": True},
    {"value": "15", "label": "5. Final Approval / Awaiting Signature", "isActive": True},
    {"value": "16", "label": "6. Closed Won", "isActive": True},
    {"value": "17", "label": "7. Deferred / On Hold", "isActive": True},
    {"value": "99", "label": "**DO NOT USE**7. Closing (by the End of Week)", "isActive": True},
    {"value": "18", "label": "8. Lost", "isActive": True},
]
COMPANIES = [{"id": 1, "companyName": "Northwind Savings Bank", "isActive": True},
             {"id": 2, "companyName": "Northwind Dental", "isActive": True}]
CONTACTS = [{"id": 10, "companyID": 1, "firstName": "Casey", "lastName": "Morgan", "emailAddress": "cmorgan@northwind.example"}]
RESOURCES = [{"id": DAVE_ID, "firstName": "Dave", "lastName": "Vener"}, {"id": 501, "firstName": "Riley", "lastName": "Jones"}]
OPPS = [
    {"id": 100, "title": "Managed Services", "companyID": 1, "ownerResourceID": DAVE_ID, "stage": 12, "status": 1,
     "probability": 30, "projectedCloseDate": "2026-06-09T00:00:00.000Z", "amount": 0},
    {"id": 101, "title": "Firewall refresh", "companyID": 2, "ownerResourceID": DAVE_ID, "stage": 14, "status": 1,
     "probability": 70, "projectedCloseDate": "2026-12-01T00:00:00.000Z", "amount": 9000},
]


def matches(row, f):
    value = row.get(f["field"])
    return {"eq": lambda: value == f["value"], "in": lambda: value in f["value"], "lt": lambda: (value or "") < f["value"],
            "contains": lambda: f["value"].lower() in (value or "").lower(), "exist": lambda: value is not None}[f["op"]]()


class FakeAutotask:
    def __init__(self):
        self.created, self.updated = [], []
        self.tables = {"Companies": COMPANIES, "Contacts": CONTACTS, "Resources": RESOURCES, "Opportunities": OPPS}

    def _api_url(self, path):
        return path

    def _request_json(self, method, url, **kw):
        assert url == "Opportunities/entityInformation/fields"
        return {"fields": [{"name": "stage", "picklistValues": STAGE_PICKLIST}]}

    def query(self, entity, filters, include_fields=None, max_records=None):
        return [r for r in self.tables[entity] if all(matches(r, f) for f in filters)]

    def create(self, entity, body):
        self.created.append((entity, body))
        return 777

    def update(self, entity, body):
        self.updated.append((entity, body))
        return body["id"]


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("AUTOTASK_DAVE_RESOURCE_ID", str(DAVE_ID))
    monkeypatch.setenv("ASSISTANT_TIMEZONE", "America/New_York")


def setup():
    at = FakeAutotask()
    directory = Directory(at)
    return at, directory, Actions(Store(":memory:"), opportunity_kinds(at), auto=set())


def test_stages_come_from_tags_picklist_skipping_do_not_use():
    _, directory, _ = setup()
    assert directory.stage_ids()["proposal"] == 14 and directory.stage_ids()["lost"] == 18
    assert directory.stage_label(14) == "Proposal / SOW presented"


def test_daves_overdue_opportunities_are_flagged():
    _, directory, _ = setup()
    rows = find_opportunities(directory, today=TODAY)
    assert [(o["title"], o["overdue"]) for o in rows] == [("Managed Services", True), ("Firewall refresh", False)]
    assert rows[0]["company"] == "Northwind Savings Bank" and rows[0]["stage"] == "Business Case Alignment"
    assert rows[0]["link"].endswith("Code=OpenOpportunity&OpportunityID=100")
    assert [o["title"] for o in find_opportunities(directory, overdue_only=True, today=TODAY)] == ["Managed Services"]


def test_company_lookup_by_email_domain_or_name():
    _, directory, _ = setup()
    assert [c["name"] for c in directory.find_companies(email="someone@northwind.example")] == ["Northwind Savings Bank"]
    assert [c["name"] for c in directory.find_companies(name="northwind")] == ["Northwind Dental", "Northwind Savings Bank"]


def test_create_follows_tags_pattern_and_waits_for_approval():
    at, directory, actions = setup()
    slip = propose_create(directory, actions, {
        "company_id": 1, "title": "Onsite IT assessment", "stage": "scoping", "close_date": "2026-11-15",
        "contact_email": "CMorgan@northwind.example", "description": "Customer:\nNorthwind Savings Bank"}, today=TODAY)
    assert slip["status"] == "pending" and not at.created
    assert slip["summary"].startswith("New opportunity “Onsite IT assessment” for Northwind Savings Bank · "
                                      "Solution Scoping (50%) · closes Nov 15, 2026 · owner Dave Vener · contact Casey Morgan")
    assert "⚠ Northwind Savings Bank already has 1 open: “Managed Services”" in slip["summary"]
    actions.approve(slip["action_id"], decided_by="test")
    [(entity, body)] = at.created
    assert entity == "Opportunities"
    assert body == {"title": "Onsite IT assessment", "companyID": 1, "ownerResourceID": DAVE_ID, "stage": 13, "status": 1,
                    "probability": 50, "startDate": "2026-10-08T00:00:00Z", "projectedCloseDate": "2026-11-15T00:00:00Z",
                    "amount": 0.0, "cost": 0.0, "useQuoteTotals": True, "opportunityCategoryID": 1,
                    "description": "Customer:\nNorthwind Savings Bank", "contactID": 10}


def test_create_refuses_unknown_companies_and_warns_about_unknown_contacts():
    _, directory, actions = setup()
    with pytest.raises(ValueError, match="find_company"):
        propose_create(directory, actions, {"company_id": 42, "title": "X"}, today=TODAY)
    slip = propose_create(directory, actions, {"company_id": 2, "title": "X", "contact_email": "nobody@x.example"}, today=TODAY)
    assert "contact nobody@x.example isn't in Autotask under Northwind Dental" in slip["summary"]


def test_update_moves_stage_with_its_probability_and_status():
    at, directory, actions = setup()
    slip = propose_update(directory, actions, {"opportunity_id": 100, "stage": "won", "close_date": "2026-10-08"}, today=TODAY)
    assert slip["summary"] == ("Update “Managed Services” (Northwind Savings Bank): stage Business Case Alignment "
                               "→ Closed Won (30% → 100%); close date Jun 9, 2026 → Oct 8, 2026")
    actions.approve(slip["action_id"], decided_by="test")
    assert at.updated == [("Opportunities", {"id": 100, "stage": 16, "probability": 100, "status": 3,
                                             "projectedCloseDate": "2026-10-08T00:00:00Z"})]


def test_tools_are_registered_with_pending_notes():
    _, directory, actions = setup()
    tools = {t.spec.name: t for t in opportunity_tools(directory, actions)}
    assert set(tools) == {"find_company", "find_opportunities", "create_opportunity", "update_opportunity"}
    result = tools["update_opportunity"].handler({"opportunity_id": 101, "close_date": "2026-12-15"})
    assert result["status"] == "pending" and "approval" in result["note"]


def test_a_contact_missing_from_autotask_is_added_and_linked():
    at, directory, actions = setup()
    slip = propose_create(directory, actions, {
        "company_id": 2, "title": "Phones", "contact_email": "pat@dental.example", "contact_name": "Pat Lee Quinn",
        "contact_title": "Office Manager"}, today=TODAY)
    assert "adds Pat Lee Quinn (pat@dental.example) as a new contact" in slip["summary"]
    assert not at.created  # nothing until Dave approves
    actions.approve(slip["action_id"], decided_by="test")
    assert at.created[0] == ("Contacts", {"companyID": 2, "firstName": "Pat", "lastName": "Lee Quinn",
                                          "emailAddress": "pat@dental.example", "isActive": 1, "title": "Office Manager"})
    assert at.created[1][0] == "Opportunities" and at.created[1][1]["contactID"] == 777
