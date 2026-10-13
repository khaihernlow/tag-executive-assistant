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
_DOLLARS = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)(?:\s?([kKmM]))?(?![\w])(\s?(?:/|per\s)\s?(?:mo|month|yr|year)\b)?")
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


def _amount(digits: str, unit: str | None) -> tuple[float, float]:
    """The value written, and how far off it may be at the precision it's written."""
    digits, unit = digits.replace(",", ""), (unit or "").lower()
    scale = {"k": 1_000, "m": 1_000_000}.get(unit, 1)
    decimals = len(digits.split(".")[1]) if "." in digits else 0
    # Half a unit of the last digit shown, and at least a dollar for cents rounding.
    return float(digits) * scale, max(0.5 * scale / (10 ** decimals), 1.0 if not unit else 0)


# Working shown in a reply: "42 seats x $135", "$135 × 42", "42 seats at $135", "$2,500 + $5,670".
# Up to two words may sit between a number and the operator ("42 seats x", "$135 per seat x").
# A number never ends in a comma ("$5,670, plus" is $5,670), and matches may overlap
# ("$5,670 + $2,500" shares its first number with the step before), hence the lookahead.
_OPERAND = r"(\$?)\s?(\d(?:[\d,]*\d)?(?:\.\d+)?)\s?([kKmM])?(?![\w])"
_CALCULATION = re.compile(r"(?<![\d,.$])(?=" + _OPERAND +
                          r"(?:\s+[A-Za-z/-]+){0,2}?\s*(×|\*|\bx\b|\btimes\b|\+|\bplus\b|\bat\b)\s*" + _OPERAND + ")")


def shown_calculations(reply: str, known: list[float]) -> list[float]:
    """Results of the arithmetic a reply writes out, when every number in it is known.

    The model multiplies ("Monthly total is $5,670 (42 seats x $135)"); code can't
    vouch for a sum it never sees, but it can redo one that's written down. A result
    counts once both operands match known numbers (or earlier results, so a chain
    works), and the stated total then has to equal it like any other amount."""
    values = list(known)

    def is_known(value: float, tolerance: float) -> bool:
        return any(abs(value - k) <= tolerance for k in values)

    derived: list[float] = []
    for _ in range(3):   # chains: "$135 x 42 = $5,670, + $2,500 onboarding"
        added = False
        for m in _CALCULATION.finditer(reply):
            a, a_tol = _amount(m.group(2), m.group(3))
            b, b_tol = _amount(m.group(6), m.group(7))
            op = m.group(4).lower()
            if op == "at" and not m.group(5):   # "at" only prices something: "42 seats at $135"
                continue
            if not (is_known(a, a_tol) and is_known(b, b_tol)):
                continue
            result = a + b if op in ("+", "plus") else a * b
            if not is_known(result, 0.005):
                values.append(result)
                derived.append(result)
                added = True
        if not added:
            break
    return derived


def verify_dollars(reply: str, known: list[float]) -> tuple[str, list[str]]:
    """Mark dollar amounts no tool returned.

    The model retypes money: "$153K", "$31,939", "$16.8K/mo". Each must match a
    number from Dave or a tool, to the precision it's written ($153K covers
    152,500 to 153,499), or a calculation written out in the reply from such
    numbers, or it's marked "(unverified)". Only dollars: plain counts are too
    often legitimately derived to check this way.
    """
    if not known:
        return reply, []
    known = known + shown_calculations(reply, known)
    changes: list[str] = []

    def check(match: re.Match) -> str:
        value, tolerance = _amount(match.group(1), match.group(2))
        if any(abs(value - k) <= tolerance for k in known):
            return match.group(0)
        changes.append(f"{match.group(0)} unverified")
        return f"{match.group(0)} (unverified)"

    return _DOLLARS.sub(check, reply), changes
