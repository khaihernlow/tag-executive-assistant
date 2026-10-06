"""Finding people: "Khai" -> a ranked list of real people with email addresses.

Code, not the LLM, decides who someone is. Sources, merged by email:
  1. People API: Dave's relevance-ranked contacts (internal and external,
     including people he only emails).
  2. Entra directory: everyone at TAG.
If neither finds the name (typos, "Khai" vs "Kai"), fall back to fuzzy
matching over Dave's top contacts and the directory.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from typing import Any, Protocol

from agent.tools import Tool
from llm.provider import ToolSpec

FUZZY_THRESHOLD = 0.72


class GraphSource(Protocol):
    def get_all(self, path: str, params: dict[str, Any] | None = None, limit: int = 500,
                headers: dict[str, str] | None = None) -> list[dict[str, Any]]: ...


@dataclass
class Person:
    name: str
    email: str
    title: str = ""
    department: str = ""
    company: str = ""
    internal: bool = False
    score: float = 0.0


def internal_domain() -> str:
    mailbox = os.environ.get("GRAPH_MAILBOX", "")
    return os.environ.get("INTERNAL_DOMAIN", mailbox.split("@")[-1] if "@" in mailbox else "").lower()


def name_score(query: str, name: str, email: str) -> float:
    """How well `query` names this person, 0-1.

    1.0 exact full name; 0.95 first name; 0.9 another name part or the
    mailbox name; 0.9 near-complete prefix ("Garret"); 0.85 prefix of 4+
    letters ("Khai" -> "Khaihern"); 0.8 nickname-like (same first letter,
    query letters in order near the start: "Kai" -> "Khaihern"); 0.75
    short prefix ("Kai" -> "Kaitlyn"); fuzzy below that.
    """
    q = query.strip().lower().replace(",", "")
    n = name.strip().lower().replace(",", "")
    if not q or not (n or email):
        return 0.0
    if q == n:
        return 1.0
    name_tokens = n.split()
    tokens = name_tokens + [email.split("@")[0].lower()]
    if name_tokens and q == name_tokens[0]:
        return 0.95
    if q in tokens:
        return 0.9

    def part_score(part: str) -> float:
        best = 0.0
        for t in tokens:
            if t.startswith(part):
                if len(part) >= 0.75 * len(t):
                    best = max(best, 0.9)
                else:
                    best = max(best, 0.85 if len(part) >= 4 else 0.75)
            elif t[:1] == part[:1] and _is_subsequence(part, t[: len(part) + 2]):
                best = max(best, 0.8)
        return best

    parts = q.split()
    if all(part_score(p) for p in parts):
        return min(part_score(p) for p in parts)
    best_token = max((SequenceMatcher(None, q, t).ratio() for t in tokens), default=0.0)
    return max(SequenceMatcher(None, q, n).ratio(), best_token) * 0.85


def _is_subsequence(needle: str, haystack: str) -> bool:
    it = iter(haystack)
    return all(ch in it for ch in needle)


def _is_relay_address(email: str) -> bool:
    # Marketing/transactional senders encode the real address in the local
    # part (gstone=tag.example@...hs-send.com); never a real contact.
    return "=" in email.split("@")[0]


def _from_people_api(raw: dict[str, Any], domain: str) -> Person | None:
    emails = raw.get("scoredEmailAddresses") or []
    if not emails or not emails[0].get("address"):
        return None
    email = emails[0]["address"].lower()
    return Person(
        name=raw.get("displayName") or email,
        email=email,
        title=raw.get("jobTitle") or "",
        department=raw.get("department") or "",
        company=raw.get("companyName") or "",
        internal=bool(domain) and email.endswith("@" + domain),
    )


def _from_directory(raw: dict[str, Any], domain: str) -> Person | None:
    email = (raw.get("mail") or raw.get("userPrincipalName") or "").lower()
    if not email:
        return None
    return Person(
        name=raw.get("displayName") or email,
        email=email,
        title=raw.get("jobTitle") or "",
        department=raw.get("department") or "",
        company="TAG Solutions" if domain and email.endswith("@" + domain) else "",
        internal=bool(domain) and email.endswith("@" + domain),
    )


PEOPLE_SELECT = "displayName,scoredEmailAddresses,jobTitle,department,companyName"
DIRECTORY_SELECT = "displayName,mail,userPrincipalName,jobTitle,department,accountEnabled"


def find_people(graph: GraphSource, query: str, limit: int = 5) -> list[Person]:
    domain = internal_domain()
    found: dict[str, Person] = {}

    def add(person: Person | None) -> None:
        if person and person.email not in found and not _is_relay_address(person.email):
            found[person.email] = person

    # People API is delegated-only and always about the signed-in user (Dave).
    for raw in graph.get_all("/me/people", {"$search": f'"{query}"', "$top": 15, "$select": PEOPLE_SELECT}, limit=15):
        add(_from_people_api(raw, domain))
    safe = query.replace('"', "")
    for raw in graph.get_all(
        "/users",
        {"$search": f'"displayName:{safe}" OR "mail:{safe}"', "$top": 15, "$select": DIRECTORY_SELECT},
        limit=15,
        headers={"ConsistencyLevel": "eventual"},
    ):
        if raw.get("accountEnabled", True):
            add(_from_directory(raw, domain))

    candidates = list(found.values())
    for person in candidates:
        person.score = name_score(query, person.name, person.email)

    if not any(p.score >= 0.9 for p in candidates):
        # No confident match: likely a misspelling or nickname ("Khai", "Kai"
        # for Khaihern) that Graph's prefix search can't see. Score a wider
        # pool locally: Dave's top contacts plus the whole directory.
        pool = [_from_people_api(raw, domain)
                for raw in graph.get_all("/me/people", {"$top": 100, "$select": PEOPLE_SELECT}, limit=100)]
        pool += [_from_directory(raw, domain)
                 for raw in graph.get_all("/users", {"$top": 999, "$select": DIRECTORY_SELECT}, limit=2000)
                 if raw.get("accountEnabled", True)]
        for person in pool:
            if person and person.email not in found and not _is_relay_address(person.email):
                person.score = name_score(query, person.name, person.email)
                found[person.email] = person
        candidates = list(found.values())

    ranked = sorted(
        (p for p in candidates if p.score >= FUZZY_THRESHOLD * 0.85),
        key=lambda p: (p.score, p.internal),
        reverse=True,
    )
    return ranked[:limit]


def is_known_address(graph: GraphSource, email: str) -> bool:
    """Exact check: is this address in TAG's directory or among people Dave
    deals with? (Name matching is fuzzy on purpose; this must not be.)"""
    email = email.strip().lower()
    for raw in graph.get_all("/me/people", {"$search": f'"{email}"', "$top": 5, "$select": PEOPLE_SELECT}, limit=5):
        if any((e.get("address") or "").lower() == email for e in raw.get("scoredEmailAddresses") or []):
            return True
    odata = email.replace("'", "''")
    return bool(graph.get_all("/users", {"$filter": f"mail eq '{odata}' or userPrincipalName eq '{odata}'",
                                         "$select": "mail"}, limit=1))


def match_note(matches: list[Person]) -> str:
    """Whether the top match is safe to act on. Ranking already prefers TAG
    staff on ties, so an internal match only counts as ambiguous against
    another internal match with the same score (two Joes at TAG)."""
    if not matches:
        return "No one found. Ask Dave for an email address."
    top = matches[0]
    rivals = [p for p in matches[1:] if p.score == top.score and p.internal == top.internal]
    if rivals:
        return "Several equally good matches; ask Dave which one before acting."
    if top.score < 0.9:
        return f"Best guess only ({top.name}); confirm with Dave before acting."
    return "Confident match."


def people_tools(graph: GraphSource) -> list[Tool]:
    def find_person(args: dict[str, Any]) -> dict[str, Any]:
        matches = find_people(graph, args["name"])
        note = match_note(matches)
        if note == "Confident match.":
            # Only the winner: listing near-misses makes the model second-guess
            # a decision code already made. Dave can still correct it.
            return {"matches": [asdict(matches[0]) | {"score": round(matches[0].score, 2)}],
                    "other_matches": len(matches) - 1, "note": note}
        return {"matches": [asdict(p) | {"score": round(p.score, 2)} for p in matches], "note": note}

    return [
        Tool(
            spec=ToolSpec(
                name="find_person",
                description=(
                    "Look up a person by name (colleague, client or prospect) and get their email, title "
                    "and company. Handles misspellings. Always use this instead of guessing an address."
                ),
                input_schema={
                    "type": "object",
                    "properties": {"name": {"type": "string", "description": "Name as Dave said it, e.g. 'Khai'"}},
                    "required": ["name"],
                },
            ),
            handler=find_person,
        )
    ]
