from datetime import date, datetime, timedelta, timezone

import pytest

from agent.actions import Actions
from agent.opportunities import Directory, opportunity_kinds, propose_create
from agent.quote_tickets import QuoteHistory, suggest
from llm.provider import LLMResponse
from store.db import Store
from tests.test_opportunities import DAVE_ID, FakeAutotask

TODAY = date(2026, 10, 20)
TICKET_FIELDS = [
    {"name": "priority", "isPickList": True, "picklistValues": [{"value": "1", "label": "High"}, {"value": "2", "label": "Medium"},
                                                                 {"value": "3", "label": "Low"}]},
    {"name": "ticketCategory", "isPickList": True, "picklistValues": [{"value": "119", "label": "Pre-Sales - New Quote"},
                                                                       {"value": "120", "label": "Pre-Sales - Renewal"},
                                                                       {"value": "3", "label": "Standard"}]},
    {"name": "queueID", "isPickList": True, "picklistValues": [{"value": "50", "label": "Pre-Sales / Design Desk"},
                                                               {"value": "60", "label": "MST: Pro Services"}]},
    {"name": "status", "isPickList": True, "picklistValues": [{"value": "1", "label": "New"}, {"value": "5", "label": "Complete"}]},
    {"name": "issueType", "isPickList": True, "picklistValues": [{"value": "9", "label": "Voice"}, {"value": "13", "label": "Pre-Sales"}]},
    {"name": "subIssueType", "isPickList": True, "picklistValues": [{"value": "61", "label": "Quote", "parentValue": "13"},
                                                                     {"value": "77", "label": "Sales Quote", "parentValue": "9"}]},
    {"name": "ticketType", "isPickList": True, "picklistValues": [{"value": "4", "label": "Change Request"}]},
    {"name": "source", "isPickList": True, "picklistValues": [{"value": "10", "label": "Sales Representative"}]},
]


def ticket(i, title, category="119", work=200, days_ago=10, due_days=4, priority=3):
    made = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return {"id": i, "title": title, "description": "", "queueID": 50, "ticketCategory": int(category), "billingCodeID": work,
            "priority": priority, "ticketType": 4, "source": 10, "issueType": None, "opportunityID": 900 + i,
            "createDate": made.strftime("%Y-%m-%dT%H:%M:%S"),
            "dueDateTime": (made + timedelta(days=due_days)).strftime("%Y-%m-%dT%H:%M:%S")}


class QuotingAutotask(FakeAutotask):
    def __init__(self):
        super().__init__()
        self.tables["Tickets"] = [ticket(1, "Quote - Mitel phone system", work=201), ticket(2, "Quote - KnowBe4 Renewal", "120"),
                                  ticket(3, "Quote - Firewall replacement"), ticket(4, "Quote - Meraki switches")]
        self.tables["BillingCodes"] = [{"id": 200, "name": "Pre-Sales", "isActive": True},
                                       {"id": 201, "name": "Voice - Pre-Sales", "isActive": True}]

    def _request_json(self, method, url, **kw):
        if url == "Tickets/entityInformation/fields":
            return {"fields": TICKET_FIELDS}
        return super()._request_json(method, url, **kw)

    def query(self, entity, filters, include_fields=None, max_records=None):
        if entity == "Tickets":
            return self.tables["Tickets"]
        if entity == "BillingCodes":
            rows = self.tables["BillingCodes"]
            if filters[0]["op"] == "contains":
                return [r for r in rows if "Pre-Sales" in r["name"]]
            return [r for r in rows if r["id"] in filters[0]["value"]]
        return super().query(entity, filters, include_fields, max_records)


class PickLLM:
    def __init__(self, **picks):
        self.picks, self.seen = picks, []

    def complete(self, messages, **kw):
        self.seen.append(messages[0]["content"])
        return LLMResponse(content=[{"type": "tool_use", "id": "t", "name": "pick_quote_ticket", "input": self.picks}],
                           stop_reason="tool_use")


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("AUTOTASK_DAVE_RESOURCE_ID", str(DAVE_ID))
    monkeypatch.setenv("ASSISTANT_TIMEZONE", "America/New_York")


PHONES = dict(title="Quote - Phone System for Northwind", category="119", work_type="201", issue="none", priority="3",
              description="PROJECT OBJECTIVE:\n- Phone system for 200 users", why="A phone system, so voice pre-sales.")


def test_the_quote_ticket_rides_on_the_opportunity_slip_with_best_guesses_dave_can_change():
    at = QuotingAutotask()
    directory, history, llm = Directory(at), QuoteHistory(at), PickLLM(**PHONES)
    actions = Actions(Store(":memory:"), opportunity_kinds(at), auto=set())
    slip = propose_create(directory, actions, {"company_id": 1, "title": "Phone System", "contact_email": "cmorgan@northwind.example",
                                               "quote_notes": "Needs a phone system, 200 users"}, today=TODAY, llm=llm, history=history)
    assert "Quote - Mitel phone system" in llm.seen[0]                      # similar past tickets shown to the model
    t = slip["display"]["ticket"]
    rows = {r[0]: r for r in t["rows"]}
    assert rows["Work type"][1] == "Voice - Pre-Sales" and rows["Work type"][2] == "Less common: 1 of the last 4 quote requests"
    assert rows["Category"][2] == "Usual choice: 3 of the last 4 quote requests"
    # Every option's count is on the slip, so the note can follow Dave's pick
    assert t["edit"]["ticket_work_type"]["usage"] == {"200": 3, "201": 1} and t["edit"]["ticket_work_type"]["usage_total"] == 4
    assert t["edit"]["ticket_issue"]["usage"]["none"] == 4
    assert rows["Category"][1] == "Pre-Sales - New Quote" and t["why"] == "A phone system, so voice pre-sales."
    assert [o[1] for o in t["edit"]["ticket_work_type"]["options"]] == ["Pre-Sales", "Voice - Pre-Sales"]
    assert slip["display"]["values"]["ticket"] == "yes"

    # The queue is a choice too: where tickets on opportunities have gone, then every other queue
    assert [o[1] for o in t["edit"]["ticket_queue"]["options"]] == ["Pre-Sales / Design Desk", "MST: Pro Services"]
    assert rows["Queue"][2] == "Usual choice: 4 of the last 4 tickets on opportunities"

    # Dave keeps the ticket but switches the work type, priority and queue
    actions.approve(slip["action_id"], decided_by="test",
                    edits={"ticket_work_type": "200", "ticket_priority": "1", "ticket_queue": "60"})
    (_, opp), (entity, body) = at.created
    assert entity == "Tickets" and body["opportunityID"] == 777 and body["companyID"] == 1 and body["contactID"] == 10
    assert (body["billingCodeID"], body["priority"], body["ticketCategory"]) == (200, 1, 119)
    assert (body["queueID"], body["ticketType"], body["source"], body["status"]) == (60, 4, 10, 1)
    assert body["title"] == "Quote - Phone System for Northwind" and "issueType" not in body


def test_no_ticket_when_dave_says_so_and_a_failed_ticket_keeps_the_opportunity():
    at = QuotingAutotask()
    actions = Actions(Store(":memory:"), opportunity_kinds(at), auto=set())
    slip = propose_create(Directory(at), actions, {"company_id": 2, "title": "Phones"}, today=TODAY,
                          llm=PickLLM(**PHONES), history=QuoteHistory(at))
    actions.approve(slip["action_id"], decided_by="test", edits={"ticket": "no"})
    assert [e for e, _ in at.created] == ["Opportunities"]

    class Refuses(QuotingAutotask):
        def create(self, entity, body):
            if entity == "Tickets":
                raise RuntimeError("no permission")
            return super().create(entity, body)

    at = Refuses()
    actions = Actions(Store(":memory:"), opportunity_kinds(at), auto=set())
    slip = propose_create(Directory(at), actions, {"company_id": 2, "title": "Phones"}, today=TODAY,
                          llm=PickLLM(**PHONES), history=QuoteHistory(at))
    done = actions.approve(slip["action_id"], decided_by="test")
    assert done["status"] == "executed" and done["result"]["opportunity_id"] == 777
    assert "no permission" in done["result"]["ticket_error"]


def test_made_up_codes_fall_back_to_what_similar_tickets_used():
    at = QuotingAutotask()
    out = suggest(PickLLM(**{**PHONES, "category": "Voice stuff", "work_type": "99999", "priority": "Low", "title": "Phones"}),
                  QuoteHistory(at), {"title": "Mitel phone system", "notes": "phone system"})
    assert out["category"] == "119" and out["work_type"] == "201"   # the Mitel ticket was the closest
    assert out["priority"] == "3" and out["title"] == "Quote - Phones"
