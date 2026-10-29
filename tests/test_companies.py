import pytest

from agent.actions import Actions
from agent.companies import company_kinds, propose_company
from agent.opportunities import Directory
from store.db import Store
from tests.test_opportunities import DAVE_ID, FakeAutotask


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("AUTOTASK_DAVE_RESOURCE_ID", str(DAVE_ID))


def setup():
    at = FakeAutotask()
    return at, Directory(at), Actions(Store(":memory:"), company_kinds(at), auto=set())


def test_a_new_account_is_a_lead_owned_by_dave_and_waits_for_approval():
    at, directory, actions = setup()
    card = propose_company(directory, actions, {"name": "Hudson Valley Orthodontics", "phone": "(518) 555-0142",
                                                "city": "Albany", "state": "NY"})
    assert card["status"] == "pending" and not at.created
    assert "Lead" in card["summary"] and "Dave Vener" in card["summary"] and "Albany, NY" in card["summary"]
    values = card["display"]["values"]
    assert values["company_type"] == "lead" and values["owner_id"] == str(DAVE_ID) and values["title"] == "Hudson Valley Orthodontics"

    done = actions.approve(card["action_id"], "dave")
    assert done["status"] == "executed" and done["result"]["company_id"] == 777
    assert "OpenAccount&AccountID=777" in done["result"]["web_link"]
    [(entity, body)] = at.created
    assert entity == "Companies"
    assert body == {"companyName": "Hudson Valley Orthodontics", "companyType": 2, "ownerResourceID": DAVE_ID,
                    "phone": "(518) 555-0142", "isActive": True, "city": "Albany", "state": "NY"}


def test_daves_changes_on_the_card_are_what_gets_saved():
    at, directory, actions = setup()
    card = propose_company(directory, actions, {"name": "Hudson Valley Ortho", "phone": "518-555-0142"})
    actions.approve(card["action_id"], "dave", edits={"title": "Hudson Valley Orthodontics, PLLC", "company_type": "customer",
                                                      "web": "hvortho.example", "phone": "518-555-0199"})
    body = at.created[0][1]
    assert body["companyName"] == "Hudson Valley Orthodontics, PLLC" and body["companyType"] == 1
    assert body["webAddress"] == "hvortho.example" and body["phone"] == "518-555-0199"


def test_an_owner_or_type_the_card_never_offered_is_ignored():
    at, directory, actions = setup()
    card = propose_company(directory, actions, {"name": "Acme Widgets", "phone": "1"})
    actions.approve(card["action_id"], "dave", edits={"owner_id": "999", "company_type": "vendor"})
    body = at.created[0][1]
    assert body["ownerResourceID"] == DAVE_ID and body["companyType"] == 2


def test_no_phone_no_card_because_autotask_requires_one():
    _, directory, actions = setup()
    with pytest.raises(ValueError, match="phone"):
        propose_company(directory, actions, {"name": "Acme Widgets"})


def test_an_existing_company_is_not_added_twice_even_with_a_suffix():
    _, directory, actions = setup()
    with pytest.raises(ValueError, match="already in Autotask .*company_id 2"):
        propose_company(directory, actions, {"name": "Northwind Dental, Inc.", "phone": "1"})


def test_similar_names_are_flagged_but_not_blocked():
    _, directory, actions = setup()
    card = propose_company(directory, actions, {"name": "Northwind Pediatrics", "phone": "1"})
    assert any("Northwind" in w for w in card["display"]["warnings"])
