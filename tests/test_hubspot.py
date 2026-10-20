from datetime import date

import pytest

from agent.hubspot import HubSpot, hubspot_tools, object_type
from agent.periods import period

TODAY = date(2026, 10, 20)   # a Tuesday


@pytest.mark.parametrize("phrase, lean, expected", [
    ("last month", "past", ("2026-09-01", "2026-09-30")),
    ("this quarter", "past", ("2026-10-01", "2026-12-31")),
    ("last quarter", "past", ("2026-07-01", "2026-09-30")),
    ("last week", "past", ("2026-10-12", "2026-10-18")),
    ("Q3", "past", ("2026-07-01", "2026-09-30")),
    ("q4 2025", "past", ("2025-10-01", "2025-12-31")),
    ("since September", "past", ("2026-09-01", None)),
    ("more than 6 months ago", "past", (None, "2026-04-19")),
    ("over a year ago", "past", (None, "2025-10-19")),
    ("the past two weeks", "past", ("2026-10-06", "2026-10-20")),
    ("next 30 days", "future", ("2026-10-20", "2026-11-19")),
    ("December", "past", ("2025-12-01", "2025-12-31")),       # looking back: the one gone
    ("December", "future", ("2026-12-01", "2026-12-31")),     # when something closes: the coming one
    ("by end of year", "future", (None, "2026-12-31")),
    ("between Sep 1 and Oct 15", "past", ("2026-09-01", "2026-10-15")),
    ("ytd", "past", ("2026-01-01", "2026-10-20")),
])
def test_reporting_periods_are_worked_out_in_code(phrase, lean, expected):
    p = period(phrase, TODAY, lean)
    assert (p["from"] and p["from"].isoformat(), p["to"] and p["to"].isoformat()) == expected


def test_a_phrase_it_cannot_read_is_none():
    assert period("whenever works", TODAY) is None


COMPANY_PROPS = [
    {"name": "name", "label": "Company name", "type": "string", "hubspotDefined": True},
    {"name": "domain", "label": "Company Domain Name", "type": "string", "hubspotDefined": True},
    {"name": "hubspot_owner_id", "label": "Company owner", "type": "enumeration", "hubspotDefined": True},
    {"name": "lifecyclestage", "label": "Lifecycle Stage", "type": "enumeration", "hubspotDefined": True,
     "options": [{"value": "lead", "label": "Lead"}, {"value": "customer", "label": "Customer"},
                 {"value": "opportunity", "label": "Opportunity"}]},
    {"name": "notes_last_contacted", "label": "Last Contacted", "type": "datetime", "hubspotDefined": True},
    {"name": "numberofemployees", "label": "Number of Employees", "type": "number", "hubspotDefined": True},
    {"name": "state", "label": "State/Region", "type": "string", "hubspotDefined": True},
    {"name": "city", "label": "City", "type": "string", "hubspotDefined": True},
    {"name": "industry", "label": "Industry", "type": "enumeration", "hubspotDefined": True, "options": []},
    {"name": "hs_lastmodifieddate", "label": "Last Modified Date", "type": "datetime", "hubspotDefined": True},
    {"name": "createdate", "label": "Create Date", "type": "datetime", "hubspotDefined": True},
    {"name": "distance_from_albany_ny", "label": "Distance from Albany, NY", "type": "number"},
    {"name": "num_associated_contacts", "label": "Number of Associated Contacts", "type": "number", "hubspotDefined": True},
]
CALL_PROPS = [
    {"name": "hs_call_title", "label": "Call Title", "type": "string"},
    {"name": "hubspot_owner_id", "label": "Activity assigned to", "type": "enumeration"},
    {"name": "hs_timestamp", "label": "Activity date", "type": "datetime"},
    {"name": "hs_call_direction", "label": "Call direction", "type": "enumeration", "options": []},
    {"name": "hs_call_disposition", "label": "Call outcome", "type": "string"},
    {"name": "hs_call_duration", "label": "Call duration", "type": "number"},
    {"name": "hs_lastmodifieddate", "label": "Last Modified Date", "type": "datetime"},
]
OWNERS = [
    {"id": "1", "email": "dave@tag.example", "firstName": "David", "lastName": "Vener"},
    {"id": "2", "email": "rgray@tag.example", "firstName": "Richard", "lastName": "Gray"},
    {"id": "3", "email": "gsmith@tag.example", "firstName": "Garrett", "lastName": "Smith"},
    {"id": "9", "email": "phoebe@x.example", "firstName": "Phoebe", "lastName": "Lawson", "archived": True},
]


def company(cid, name, domain, contacts=0, lifecycle=None, last=None, owner=None):
    return {"id": cid, "properties": {"name": name, "domain": domain, "num_associated_contacts": str(contacts),
                                      "lifecyclestage": lifecycle, "notes_last_contacted": last, "hubspot_owner_id": owner}}


class FakeHubSpot:
    def __init__(self, companies=(), totals=None):
        self.companies = list(companies)
        self.totals = totals or {}
        self.searches = []

    def properties(self, obj):
        return {"companies": COMPANY_PROPS, "calls": CALL_PROPS}.get(obj, [
            {"name": "hs_lastmodifieddate", "label": "Last Modified Date", "type": "datetime"},
            {"name": "hubspot_owner_id", "label": "Owner", "type": "enumeration"}])

    def owners(self):
        return OWNERS

    def pipelines(self, obj="deals"):
        return [{"id": "default", "label": "Sales Pipeline", "stages": [{"id": "appointmentscheduled", "label": "Appointment Scheduled"}]}]

    def call_outcomes(self):
        return {"f240bbac": "Connected", "b2cf5968": "Left voicemail"}

    def record_url(self, obj, rid):
        return f"https://app.hubspot.example/{obj}/{rid}"

    def search(self, obj, **kw):
        self.searches.append((obj, kw))
        if obj == "companies" and kw.get("query"):
            return {"total": len(self.companies), "results": self.companies}
        groups = kw.get("filter_groups") or [{"filters": kw.get("filters") or []}]
        key = tuple(sorted(f.get("value", f.get("operator")) for g in groups for f in g["filters"] if f["propertyName"] == "lifecyclestage"
                           or f["operator"] == "NOT_HAS_PROPERTY"))
        return {"total": self.totals.get(key, 3), "results": self.companies[:kw.get("limit", 100)]}

    def search_all(self, obj, max_rows=2000, **kw):
        self.searches.append((obj, kw))
        return self.companies[:max_rows], self.totals.get("all", len(self.companies))


@pytest.fixture
def hub(monkeypatch):
    monkeypatch.setenv("GRAPH_MAILBOX", "dave@tag.example")
    monkeypatch.setenv("ASSISTANT_TIMEZONE", "America/New_York")
    return HubSpot(FakeHubSpot(), today=TODAY)


def test_daves_words_become_hubspot_fields_owners_and_options(hub):
    assert object_type("accounts") == "companies" and object_type("phone calls") == "calls"
    assert hub.field("companies", "lifecycle")["name"] == "lifecyclestage"
    assert hub.field("companies", "Distance from Albany, NY")["name"] == "distance_from_albany_ny"   # by its label
    with pytest.raises(ValueError, match="No companies field called 'favourite colour'"):
        hub.field("companies", "favourite colour")
    assert hub.owner_id("me") == "1" and hub.owner_id("Garrett") == "3" and hub.owner_id("Richard Gray") == "2"
    assert hub.owner_name("9") == "Phoebe Lawson (former)"
    [f], said = hub.build_filter("companies", {"field": "lifecycle", "op": "is", "value": "Customer"})
    assert f == {"propertyName": "lifecyclestage", "operator": "EQ", "value": "customer"} and said == "Lifecycle Stage is Customer"
    [f], _ = hub.build_filter("calls", {"field": "outcome", "op": "is", "value": "connected"})
    assert f["value"] == "f240bbac"


def test_dates_are_day_bounds_in_local_time_and_states_match_both_spellings(hub):
    start, end = hub.build_filter("calls", {"field": "when", "op": "during", "value": "last week"})[0]
    assert (start["operator"], end["operator"]) == ("GTE", "LTE")
    # Oct 12 00:00 Eastern is 04:00 UTC; Oct 18 23:59:59 Eastern is Oct 19 03:59:59 UTC
    assert start["value"] == "1791777600000" and end["value"] == "1792382399999"
    [before], said = hub.build_filter("companies", {"field": "last contacted", "op": "before", "value": "6 months ago"})
    assert before["operator"] == "LT" and said == "Last Contacted before Apr 20, 2026"
    [state], said = hub.build_filter("companies", {"field": "state", "op": "is", "value": "NY"})
    assert state == {"propertyName": "state", "operator": "IN", "values": ["NY", "New York"]}


def test_either_becomes_or_groups_sharing_the_other_conditions(hub):
    hub.query({"object": "companies", "filters": [{"field": "lifecycle", "op": "is", "value": "Customer"}],
               "either": [{"field": "last contacted", "op": "before", "value": "6 months ago"},
                          {"field": "last contacted", "op": "is_empty"}]})
    _, kw = hub.hs.searches[-1]
    groups = kw["filter_groups"]
    assert len(groups) == 2 and all(g["filters"][0]["propertyName"] == "lifecyclestage" for g in groups)
    assert groups[1]["filters"][1] == {"propertyName": "notes_last_contacted", "operator": "NOT_HAS_PROPERTY"}


def test_counting_a_big_set_by_category_asks_hubspot_for_each_exact_count(monkeypatch):
    monkeypatch.setenv("ASSISTANT_TIMEZONE", "America/New_York")
    sample = [company(str(i), f"Co {i}", f"co{i}.example", lifecycle=lc) for i, lc in enumerate(["lead", "lead", "customer", None])]
    fake = FakeHubSpot(sample, totals={"all": 22089, ("lead",): 10038, ("customer",): 76, ("NOT_HAS_PROPERTY",): 11782})
    out = HubSpot(fake, today=TODAY).query({"object": "companies", "group_by": "lifecycle"})
    assert out["matching"] == 22089
    assert out["groups"] == [{"group": "(none)", "count": 11782}, {"group": "Lead", "count": 10038},
                             {"group": "Customer", "count": 76}, {"group": "(other values)", "count": 193}]
    assert "note" not in out          # exact, so nothing to caveat


def test_a_duplicate_company_record_is_resolved_by_its_history_and_mentioned(hub):
    hub.hs.companies = [company("a", "Sawyer Savings Bank", "sawyersavings.com", contacts=1, lifecycle="opportunity",
                                last="2026-10-08T12:00:00Z", owner="2"),
                        company("b", "Sawyer Savings Bank", "sawyersavings.bank", contacts=0, lifecycle="lead", last="2026-10-05T12:00:00Z")]
    cid, name, others = hub.company_id("Sawyer Savings")
    assert (cid, name, others) == ("a", "Sawyer Savings Bank", ["Sawyer Savings Bank (sawyersavings.bank)"])
    # Two different companies that both have history: Dave picks
    hub.hs.companies = [company("x", "Equinox, Inc.", "equinoxinc.org", contacts=5, lifecycle="opportunity", last="2026-10-20T12:00:00Z"),
                        company("y", "Equinox Fitness", "equinox.com", contacts=4, lifecycle="opportunity", last="2026-10-19T12:00:00Z")]
    with pytest.raises(ValueError, match="Several companies match"):
        hub.company_id("Equinox")


def test_the_tools_are_named_for_the_model(hub):
    assert [t.spec.name for t in hubspot_tools(hub)] == ["hubspot_query", "hubspot_record", "hubspot_fields", "hubspot_meeting_link"]
