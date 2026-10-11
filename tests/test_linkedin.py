import pytest

from agent.briefs import linkedin_findings
from agent.linkedin import LinkedIn, company_matches, linkedin_tools, one_line, pick, summarize
from connectors.apify import ApifyClient, ApifyError
from store.db import Store


def profile(first, last, jobs, schools=(), headline="", about="", url=None):
    return {"firstName": first, "lastName": last, "headline": headline, "about": about,
            "linkedinUrl": url or f"https://www.linkedin.com/in/{first}-{last}-{len(jobs)}".lower(),
            "location": {"linkedinText": "Albany, New York"},
            "experience": [{"position": t, "companyName": c, "duration": d,
                            "endDate": {"text": "Present" if now else "2020"}} for t, c, d, now in jobs],
            "education": [{"schoolName": s, "degree": deg, "fieldOfStudy": f} for s, deg, f in schools]}


CASEY = profile("Casey", "Morgan", [("VP Finance", "Northwind Savings Bank", "4 yrs", True),
                                     ("Loan Officer", "Northwind Savings Bank", "10 yrs", False)],
                [("Hudson Valley College", "BS", "Accounting")], headline="VP Finance at Northwind Savings Bank")
CASEY_OLD = profile("Casey", "Morgan", [("Teller", "Northwind Savings Bank", "", True)])  # an old, thin account
CASEY_ELSEWHERE = profile("Casey", "Morgan", [("Nurse", "Albany Med", "6 yrs", True)])


class FakeApify:
    def __init__(self, items):
        self.items, self.calls = items, []

    def run(self, actor, run_input, max_charge_usd=0.05):
        self.calls.append((actor, run_input, max_charge_usd))
        return self.items


def test_company_matching_ignores_suffixes_and_needs_every_distinctive_word():
    assert company_matches("Northwind Savings", "Northwind Savings Bank")
    assert company_matches("Northwind Savings Bank, Inc.", "Northwind Savings Bank")
    assert not company_matches("Northwind Dental", "Northwind Savings Bank")
    assert not company_matches("", "Anything")


def test_summary_and_one_line():
    s = summarize(CASEY)
    assert s["current"] == [{"title": "VP Finance", "company": "Northwind Savings Bank", "duration": "4 yrs", "current": True}]
    assert s["past"][0]["title"] == "Loan Officer" and s["location"] == "Albany, New York"
    assert one_line(s) == ("VP Finance at Northwind Savings Bank (4 yrs); before: Loan Officer at Northwind Savings Bank; "
                           "studied BS, Accounting at Hudson Valley College")


def test_pick_prefers_the_named_company_and_collapses_duplicate_accounts():
    profiles = [summarize(p) for p in (CASEY_OLD, CASEY_ELSEWHERE, CASEY)]
    best, others = pick(profiles, "Northwind Savings")
    assert best["name"] == "Casey Morgan" and best["current"][0]["title"] == "VP Finance"   # the fuller account
    assert [o["current"][0]["company"] for o in others] == ["Albany Med"]
    # No company and several people with the name: don't guess.
    assert pick(profiles)[0] is None
    assert pick(profiles, "Some Other Co")[0] is None


def test_lookups_are_cached_and_capped():
    store, apify = Store(":memory:"), FakeApify([CASEY])
    linkedin = LinkedIn(apify, store)
    first = linkedin.find("Casey Morgan", "Northwind Savings")
    second = linkedin.find("Casey  morgan", "Northwind Savings")
    assert first == second and len(apify.calls) == 1     # paid for once
    actor, run_input, cap = apify.calls[0]
    assert actor == "harvestapi/linkedin-profile-search-by-name" and cap <= 0.05
    assert run_input["firstName"] == "Casey" and run_input["lastName"] == "Morgan" and run_input["maxItems"] <= 5
    with pytest.raises(ValueError, match="first and last"):
        linkedin.find("Casey")


def test_budget_guard_stops_runs(monkeypatch):
    client = ApifyClient("token", budget_usd=4.0)
    monkeypatch.setattr(client, "month_usage", lambda: 3.99)
    with pytest.raises(ApifyError, match="budget reached"):
        client.run("harvestapi/linkedin-profile-search-by-name", {}, max_charge_usd=0.03)


def test_chat_tool_says_when_it_is_unclear():
    linkedin = LinkedIn(FakeApify([CASEY, CASEY_ELSEWHERE]), Store(":memory:"))
    [tool] = linkedin_tools(linkedin)
    result = tool.handler({"name": "Casey Morgan"})
    assert result["person"] is None and len(result["other_matches"]) == 2 and "which one" in result["note"]
    result = tool.handler({"name": "Casey Morgan", "company": "Northwind Savings"})
    assert result["person"]["linkedin"] == CASEY["linkedinUrl"] and result["summary"].startswith("VP Finance")


class FakeDirectory:
    def find_companies(self, name="", email=""):
        return [{"company_id": 1, "name": "Northwind Savings Bank", "active": True}] if email.endswith("@northwind.example") else []


def test_briefs_get_linkedin_backgrounds_as_cited_sources():
    linkedin = LinkedIn(FakeApify([CASEY, CASEY_ELSEWHERE]), Store(":memory:"))
    outside = [{"name": "Casey Morgan", "email": "cmorgan@northwind.example"},
               {"name": "Unknown Person", "email": "x@nowhere.example"},
               {"name": "info@vendor.example", "email": "info@vendor.example"}]
    found = linkedin_findings(linkedin, FakeDirectory(), outside, first_label=3)
    # Casey's company comes from Autotask by email domain; the unknown person's search (same fake
    # results, no company) is ambiguous, so they're skipped; an address isn't a name.
    assert found == [{"label": "Web 3", "about": "Casey Morgan (LinkedIn)", "url": CASEY["linkedinUrl"],
                      "fact": one_line(summarize(CASEY))}]


def test_email_facts_are_stated_in_code(monkeypatch):
    from agent import linkedin as li

    mail = [{"subject": "RE: follow-up", "from": {"name": "Dave Vener", "email": "dave@tag.example"}, "received": "Fri Oct 9 8:15 AM"},
            {"subject": "RE: follow-up", "from": {"name": "Casey Morgan", "email": "cm@northwind.example"}, "received": "Thu Oct 8 2:17 PM"},
            {"subject": "Intro", "from": {"name": "Casey Morgan", "email": "cm@northwind.example"}, "received": "Thu Oct 1 9:00 AM"}]
    # By name: everything mentioning them. By address: only their own, newest first.
    monkeypatch.setattr("agent.mail.search_mail",
                        lambda *a, **k: [m for m in mail if m["from"]["email"] == k["sender"]] if k.get("sender") else mail)

    class Graph:
        mailbox = "dave@tag.example"

    facts = li.tag_context(Graph(), None, "Casey Morgan", "")["email_facts"]
    assert facts == ('3 emails mention them (newest first). Latest from them: Thu Oct 8 2:17 PM, "RE: follow-up". '
                     "Latest from Dave: Fri Oct 9 8:15 AM.")
