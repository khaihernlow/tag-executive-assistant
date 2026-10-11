"""LinkedIn research through Apify: who someone is, their career and education.

Dave asked for prospect research down to "role, background, school". Web search
rarely gets that for a mid-level contact at a small company; LinkedIn does.
HarvestAPI's no-cookie actors are used, so no TAG or Dave LinkedIn account is
involved. A lookup costs about 1.2 cents; results are cached for 30 days, so a
person is never paid for twice in a month.

Picking the right person is code, not the model: profiles are ranked by whether
their current employer matches the company Dave named. Several profiles at the
same current company (the same person's old and new accounts) collapse to the
most complete one; matches at different companies are offered, not guessed.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

SEARCH_ACTOR = "harvestapi/linkedin-profile-search-by-name"
CACHE_DAYS = 30
MAX_CHARGE_USD = 0.03  # a search page and up to 5 profiles at 0.4 cents
_STOP = {"inc", "llc", "ltd", "corp", "corporation", "co", "company", "the", "group", "and", "&", "pc", "llp", "plc"}


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if w not in _STOP}


def company_matches(company: str, text: str) -> bool:
    """'Sawyer Savings' matches 'Sawyer Savings Bank': every distinctive word of what Dave said appears."""
    wanted = _words(company)
    return bool(wanted) and wanted <= _words(text)


def summarize(p: dict[str, Any]) -> dict[str, Any]:
    """The parts of a profile worth reading, in a fixed shape."""
    location = p.get("location")
    if isinstance(location, dict):
        location = location.get("linkedinText") or location.get("parsed", {}).get("text") or ""
    jobs = p.get("experience") or []

    def job(e: dict[str, Any]) -> dict[str, str]:
        return {"title": (e.get("position") or "").strip(), "company": (e.get("companyName") or "").strip(),
                "duration": (e.get("duration") or "").strip(),
                "current": not (e.get("endDate") or {}).get("text") or (e.get("endDate") or {}).get("text") == "Present"}

    roles = [job(e) for e in jobs]
    return {
        "name": f"{p.get('firstName') or ''} {p.get('lastName') or ''}".strip(),
        "headline": (p.get("headline") or "").strip(),
        "location": location or "",
        "linkedin": p.get("linkedinUrl") or "",
        "current": [r for r in roles if r["current"]][:3],
        "past": [r for r in roles if not r["current"]][:5],
        "education": [{"school": (e.get("schoolName") or "").strip(), "degree": (e.get("degree") or "").strip(),
                       "field": (e.get("fieldOfStudy") or "").strip()} for e in (p.get("education") or [])][:3],
        "about": " ".join((p.get("about") or "").split())[:400],
        "skills": [s.get("name") if isinstance(s, dict) else str(s) for s in (p.get("topSkills") or p.get("skills") or [])][:6],
        "completeness": len(jobs) + len(p.get("education") or []) + (1 if p.get("about") else 0),
    }


def one_line(s: dict[str, Any]) -> str:
    """'AVP/Finance at Sawyer Savings Bank (4 yrs 5 mos); before: ...; studied ... at ...'"""
    parts = []
    if s["current"]:
        c = s["current"][0]
        parts.append(f"{c['title']} at {c['company']}" + (f" ({c['duration']})" if c["duration"] else ""))
    elif s["headline"]:
        parts.append(s["headline"])
    if s["past"]:
        parts.append("before: " + "; ".join(f"{r['title']} at {r['company']}" for r in s["past"][:2]))
    if s["education"]:
        e = s["education"][0]
        study = ", ".join(x for x in (e["degree"], e["field"]) if x)
        parts.append(f"studied {study + ' at ' if study else 'at '}{e['school']}")
    return "; ".join(p for p in parts if p)


def pick(profiles: list[dict[str, Any]], company: str = "") -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """(the person, other candidates worth showing). None when it's genuinely unclear."""
    if not profiles:
        return None, []
    if company:
        current = [p for p in profiles if any(company_matches(company, r["company"]) for r in p["current"])
                   or company_matches(company, p["headline"])]
        if current:
            best = max(current, key=lambda p: p["completeness"])
            # Same person, several accounts, all at the named company: keep the fullest.
            return best, [p for p in profiles if p not in current]
        past = [p for p in profiles if any(company_matches(company, r["company"]) for r in p["past"])]
        if len(past) == 1:
            return past[0], [p for p in profiles if p is not past[0]]
        return None, profiles
    if len(profiles) == 1:
        return profiles[0], []
    return None, profiles


class LinkedIn:
    def __init__(self, apify: Any, store: Any) -> None:
        self.apify, self.store = apify, store

    def search(self, name: str, location: str = "") -> list[dict[str, Any]]:
        """Profiles for a full name (cached 30 days)."""
        parts = name.split()
        if len(parts) < 2:
            raise ValueError("Give the person's first and last name.")
        key = f"linkedin:{' '.join(parts).lower()}|{location.lower()}"
        cached = self.store.get_state(key) if self.store is not None else None
        if cached:
            data = json.loads(cached)
            if datetime.now(timezone.utc) - datetime.fromisoformat(data["at"]) < timedelta(days=CACHE_DAYS):
                return data["profiles"]
        run_input: dict[str, Any] = {"firstName": parts[0], "lastName": " ".join(parts[1:]), "profileScraperMode": "Full",
                                     "maxPages": 1, "maxItems": 5, "strictSearch": True}
        if location:
            run_input["locations"] = [location]
        profiles = [summarize(p) for p in self.apify.run(SEARCH_ACTOR, run_input, MAX_CHARGE_USD)]
        if self.store is not None:
            self.store.set_state(key, json.dumps({"at": datetime.now(timezone.utc).isoformat(), "profiles": profiles}))
        return profiles

    def find(self, name: str, company: str = "", location: str = "") -> dict[str, Any]:
        person, others = pick(self.search(name, location), company)
        return {"person": person, "summary": one_line(person) if person else "",
                "other_matches": [{"name": p["name"], "headline": p["headline"], "location": p["location"],
                                   "linkedin": p["linkedin"]} for p in others[:4]]}


# ── what TAG already knows ───────────────────────────────────────────────────

def tag_context(graph: Any, directory: Any, name: str, company: str) -> dict[str, Any]:
    """Past email with Dave, and whether the company is in Autotask with open deals. Often
    more useful than the web: 'we quoted them in June' beats 'they have 40 employees'."""
    from agent.mail import search_mail
    from agent.opportunities import find_opportunities

    out: dict[str, Any] = {}
    if graph is not None:
        try:
            mail = search_mail(graph, about=f'"{name}"', since_days=730, limit=10, broaden=False)
            out["emails_with_dave"] = [{"subject": m["subject"], "from": m["from"].get("name") or m["from"].get("email"),
                                        "received": m["received"]} for m in mail]
            # Stated in code: the model once read this list and said her "last reply" was a week too early.
            mine = graph.mailbox.lower()
            theirs = [m for m in mail if name.lower() in (m["from"].get("name") or "").lower()]
            if theirs and theirs[0]["from"].get("email"):
                # A name search ranks by relevance and can miss their newest email; ask for it by address.
                by_address = search_mail(graph, sender=theirs[0]["from"]["email"], since_days=730, limit=3, broaden=False)
                theirs = by_address or theirs
            daves = [m for m in mail if (m["from"].get("email") or "").lower() == mine]
            out["email_facts"] = (f"{len(mail)} emails mention them (newest first). "
                                  + (f"Latest from them: {theirs[0]['received']}, \"{theirs[0]['subject']}\". " if theirs else
                                     "None of these are from them. ")
                                  + (f"Latest from Dave: {daves[0]['received']}." if daves else ""))
        except Exception:  # noqa: BLE001 - context is a bonus, never a blocker
            pass
    if directory is not None and company:
        try:
            companies = directory.find_companies(name=company)[:1]
            if companies:
                c = companies[0]
                opps = find_opportunities(directory, company_id=c["company_id"], mine=False)
                out["autotask"] = {"company": c["name"], "active": c["active"],
                                   "open_opportunities": [f"{o['title']} ({o['stage']}, owner {o['owner']})" for o in opps[:4]]}
        except Exception:  # noqa: BLE001
            pass
    return out


def linkedin_tools(linkedin: LinkedIn, graph: Any = None, directory: Any = None) -> list[Any]:
    from agent.tools import Tool
    from llm.provider import ToolSpec

    def research(args: dict[str, Any]) -> dict[str, Any]:
        name, company = (args.get("name") or "").strip(), (args.get("company") or "").strip()
        found = linkedin.find(name, company, (args.get("location") or "").strip())
        found["tag"] = tag_context(graph, directory, name, company)
        if not found["person"]:
            found["note"] = ("No single match. Show Dave the other_matches and ask which one, or ask for their company."
                             if found["other_matches"] else "No LinkedIn profile found for that name.")
        return found

    return [Tool(spec=ToolSpec(
        name="research_person",
        description=("Look someone up on LinkedIn (via Apify, about a cent): current role and how long, past roles, "
                     "education, plus past email with Dave and their company in Autotask. Give their company when "
                     "known so the right person is picked. For email history, say only what email_facts says."),
        input_schema={"type": "object", "properties": {
            "name": {"type": "string", "description": "First and last name"},
            "company": {"type": "string", "description": "Where they work, if known"},
            "location": {"type": "string", "description": "City or region, only to narrow a common name"}},
            "required": ["name"]}),
        handler=research)]
