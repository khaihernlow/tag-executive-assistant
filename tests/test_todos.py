from datetime import date

import pytest

from agent.todos import collect, is_dave, owners, set_done, todo_tools, view
from store.db import Store

TODAY = date(2026, 10, 21)


@pytest.mark.parametrize("owner, mine", [
    ("Dave Vener", True), ("David Vener", True), ("Dave", True),
    ("Dave Vener, Ximena Morales", True), ("Matt and Dave Vener", True),       # shared: still his
    ("Dave Vener's assistant", False), ("Davey Jones", False), ("Garrett Smith", False), ("", False),
])
def test_whose_item_it_is(owner, mine):
    assert is_dave(owner) is mine


def test_names_split_on_commas_and_the_word_and_only():
    assert owners("Matt and Dave Vener") == ["Matt", "Dave Vener"]
    assert owners("Brandon Sanders") == ["Brandon Sanders"]


def note(store, title, held, items):
    return store.add_note(kind="meeting", title=title, source="maestro", started_at=f"{held}T10:00", people=[],
                          status="ready", digest={"headline": title, "action_items": items})


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setenv("ASSISTANT_TIMEZONE", "America/New_York")
    s = Store(":memory:")
    note(s, "FoxPoint/TAG", "2026-10-19", [{"what": "Prepare and send NDA for sharing financials. [L4]", "owner": "Dave Vener"},
                                            {"what": "Send the deck.", "owner": "Garrett Smith"}])
    note(s, "Sales meeting", "2026-09-30", [{"what": "Review resumes.", "owner": "David Vener"},
                                             {"what": "Update the pipeline.", "owner": "All employees"}])
    note(s, "Old meeting", "2026-05-01", [{"what": "Something from May.", "owner": "Dave Vener"}])
    return s


def test_the_list_is_daves_items_then_others_by_person(store):
    items = collect(store, days=60, today=TODAY)
    assert {t["what"] for t in items} == {"Prepare and send NDA for sharing financials.", "Send the deck.",
                                          "Review resumes.", "Update the pipeline."}            # citation gone; May too old
    v = view(store, days=60)
    assert [t["what"] for t in v["mine"]] == ["Prepare and send NDA for sharing financials.", "Review resumes."]
    assert [g["owner"] for g in v["others"]] == ["Garrett Smith", "Everyone / unassigned"]
    resumes = next(t for t in items if t["what"] == "Review resumes.")
    assert resumes["old"] and resumes["age_days"] == 21                                        # open two weeks or more


def test_ticking_one_off_moves_it_to_done_and_back(store):
    nda = next(t for t in collect(store, today=TODAY) if t["what"].startswith("Prepare"))
    set_done(store, nda["id"], True)
    v = view(store)
    assert [t["what"] for t in v["mine"]] == ["Review resumes."] and v["done"][0]["id"] == nda["id"]
    set_done(store, nda["id"], False)
    assert len(view(store)["mine"]) == 2


def test_chat_can_ask_what_dave_or_someone_else_owes(store):
    [find] = todo_tools(store)
    mine = find.handler({"who": "me", "days": 60})
    assert mine["count"] == 2 and mine["items"][0]["meeting"] == "FoxPoint/TAG"
    assert [t["what"] for t in find.handler({"who": "Garrett", "days": 60})["items"]] == ["Send the deck."]
