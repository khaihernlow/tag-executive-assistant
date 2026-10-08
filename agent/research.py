"""Web research for briefs: who are the outside people and companies?

Uses HatzAI's built-in web search (firecrawl) on its native chat endpoint,
so no extra API key. Code decides what to look up (attendee companies from
their email domains, attendee names); the search model returns findings that
must each carry a URL, and anything without one is dropped.
"""

from __future__ import annotations

import json
import re
from typing import Any

FREEMAIL = {
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "msn.com", "yahoo.com", "icloud.com",
    "me.com", "aol.com", "proton.me", "protonmail.com", "comcast.net", "verizon.net", "att.net",
}
MAX_FINDINGS = 6

RESEARCH_PROMPT = """You research the outside participants of a business meeting for a CEO's brief.
Meeting: {subject}
Look up:
{targets}

Return ONLY a JSON array (no prose) of at most {limit} findings, most useful first:
[{{"about": "who/what this is about", "fact": "one short factual sentence", "url": "https://source"}}]
Rules: every finding needs a real source URL from your search. Prefer facts that help in THIS meeting:
what the company does and offers (especially anything related to the meeting topic), who they serve,
the person's current role and relevant background. Skip generic facts about large well-known companies
(revenue, headcount, stock). For a person, include a finding only if the source names the same
company or role; social profiles with just a matching name are someone else until proven otherwise.
No guesses, no marketing adjectives."""


def research_targets(attendees: list[dict[str, str]], internal_domain: str) -> list[str]:
    """Plain-language lookup list from outside attendees: companies by domain, people by name."""
    targets, seen_domains = [], set()
    for a in attendees:
        email = (a.get("email") or "").lower()
        name = (a.get("name") or "").strip()
        domain = email.split("@")[-1] if "@" in email else ""
        if not domain or domain == internal_domain:
            continue
        if domain not in FREEMAIL and domain not in seen_domains:
            seen_domains.add(domain)
            targets.append(f"- The company at {domain}")
        if name and "@" not in name and " " in name:
            where = f" (email domain {domain})" if domain not in FREEMAIL else ""
            targets.append(f"- {name}{where}")
    return targets[:6]


def parse_findings(text: str) -> list[dict[str, str]]:
    match = re.search(r"\[[\s\S]*\]", text or "")
    if not match:
        return []
    try:
        raw = json.loads(match.group(0))
    except ValueError:
        return []
    findings = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        fact, url = str(item.get("fact", "")).strip(), str(item.get("url", "")).strip()
        if fact and url.startswith(("https://", "http://")):
            findings.append({"about": str(item.get("about", "")).strip()[:80], "fact": fact[:300], "url": url[:500]})
    return findings[:MAX_FINDINGS]


def web_research(searcher: Any, subject: str, targets: list[str]) -> list[dict[str, str]]:
    """Labelled findings ([Web 1], ...) or [] if nothing worth searching or the search failed."""
    if not targets:
        return []
    prompt = RESEARCH_PROMPT.format(subject=subject, targets="\n".join(targets), limit=MAX_FINDINGS)
    try:
        findings = parse_findings(searcher.web_answer(prompt))
    except Exception:  # noqa: BLE001 - research is a bonus; a brief without it is still a brief
        return []
    return [{"label": f"Web {i + 1}", **f} for i, f in enumerate(findings)]
