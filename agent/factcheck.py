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


# Also " -- ", the double hyphen the model types when it can't use a real dash.
_LONG_DASH = re.compile(r"[ \t]*[\u2014\u2013][ \t]*|[ \t]+--[ \t]+")
_DASH_RANGE = re.compile(r"(\d)[ \t]*[\u2013\u2014][ \t]*(\d)")


def no_long_dashes(text: str) -> str:
    """Em/en dashes become commas (ranges like 3:00–3:30 become 3:00-3:30).
    Only long dashes: markdown bullets and hyphenated words are left alone."""
    text = _DASH_RANGE.sub(r"\1-\2", text)
    return _LONG_DASH.sub(", ", text)


_NAME = re.compile(r"\b([A-Z][a-z'\u2019]+)\s+([A-Z][A-Za-z'\u2019]+)\b")


def verify_names(reply: str, known: set[str]) -> tuple[str, list[str]]:
    """Fix or flag staff names that blend two real people.

    In a time-entry answer the model wrote "Cory Lawrence" for Cory Keller and
    Chris Lawrence. Only names that borrow a known person's first or last name
    are checked, so ordinary capitalised phrases pass. A blend that fits one
    person is corrected to them; one that fits several is marked unclear.
    """
    if not known:
        return reply, []
    full = {n.lower() for n in known}
    firsts: dict[str, set[str]] = {}
    lasts: dict[str, set[str]] = {}
    for n in known:
        parts = n.split()
        if len(parts) >= 2:
            firsts.setdefault(parts[0].lower(), set()).add(n)
            lasts.setdefault(parts[-1].lower(), set()).add(n)
    changes: list[str] = []

    def check(match: re.Match) -> str:
        name = match.group(0)
        if name.lower() in full:
            return name
        first, last = match.group(1).lower(), match.group(2).lower()
        candidates = firsts.get(first, set()) | lasts.get(last, set())
        if not candidates or (first not in firsts and last not in lasts):
            return name
        if len(candidates) == 1:
            fixed = next(iter(candidates))
            changes.append(f"{name} -> {fixed}")
            return fixed
        changes.append(f"{name} unclear")
        return f"{name} (name unclear: {' or '.join(sorted(candidates))})"

    return _NAME.sub(check, reply), changes


# "$153K", "$1,989/mo", "$16.8K per month": the period is part of the amount, so a flag goes after it.
_DOLLARS = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)\s?([kKmM])?(?![\w])(\s?(?:/|per\s)\s?(?:mo|month|yr|year)\b)?")
_NUMBER = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?")


def known_numbers(*sources: str) -> list[float]:
    found = set()
    for text in sources:
        for raw in _NUMBER.findall(text or ""):
            try:
                found.add(abs(float(raw.replace(",", ""))))
            except ValueError:
                continue
    return sorted(found)


def verify_dollars(reply: str, known: list[float]) -> tuple[str, list[str]]:
    """Mark dollar amounts no tool returned.

    The model retypes money: "$153K", "$31,939", "$16.8K/mo". Each must match a
    number from Dave or a tool, to the precision it's written ($153K covers
    152,500 to 153,499), or it's marked "(unverified)". Only dollars: plain
    counts are too often legitimately derived to check this way.
    """
    if not known:
        return reply, []
    changes: list[str] = []

    def check(match: re.Match) -> str:
        digits, unit = match.group(1).replace(",", ""), (match.group(2) or "").lower()
        scale = {"k": 1_000, "m": 1_000_000}.get(unit, 1)
        value = float(digits) * scale
        decimals = len(digits.split(".")[1]) if "." in digits else 0
        # Half a unit of the last digit shown, and at least a dollar for cents rounding.
        tolerance = max(0.5 * scale / (10 ** decimals), 1.0 if not unit else 0)
        if any(abs(value - k) <= tolerance for k in known):
            return match.group(0)
        changes.append(f"{match.group(0)} unverified")
        return f"{match.group(0)} (unverified)"

    return _DOLLARS.sub(check, reply), changes
