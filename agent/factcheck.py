"""Code checks on the model's reply before Dave sees it.

The model once wrote "samuel@staffing.example" when every tool had
returned "sam@staffing.example" (it blended in "Samuel" from another
email). Addresses are where a typo turns into a misdirected invite, so
every address in a reply must come from a tool result or Dave's message.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def known_addresses(*sources: str) -> set[str]:
    return {m.lower() for text in sources for m in EMAIL.findall(text or "")}


def verify_addresses(reply: str, known: set[str]) -> tuple[str, list[str]]:
    """Fix or flag addresses the tools never returned.

    A wrong address with exactly one real address at the same domain is
    corrected to it; anything else is marked "(unverified)". Returns the
    new text and a list of what changed, for logging.
    """
    changes: list[str] = []

    def check(match: re.Match) -> str:
        address = match.group(0)
        if address.lower() in known:
            return address
        domain = address.split("@", 1)[1].lower()
        same_domain = [k for k in known if k.endswith("@" + domain)]
        if len(same_domain) == 1:
            changes.append(f"{address} -> {same_domain[0]}")
            return same_domain[0]
        if same_domain:
            best = max(same_domain, key=lambda k: SequenceMatcher(None, k, address.lower()).ratio())
            changes.append(f"{address} -> {best} (closest of {len(same_domain)})")
            return best
        changes.append(f"{address} unverified")
        return f"{address} (unverified)"

    return EMAIL.sub(check, reply), changes
