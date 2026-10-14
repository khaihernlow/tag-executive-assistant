"""Saati Deli's menu on Mealeo, read from the public restaurant page.

Mealeo has no API. Its restaurant page lists every item with a price, and each
item's page lists its option groups (dressing, bread, add-ons) with prices. Code
reads both, so an order is matched to real menu names and priced exactly, and
the menu is never a stale copy (read once a day).
"""

from __future__ import annotations

import html as htmllib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

import requests

SITE = "https://www.mealeo.com"
# Saati Deli Latham (586 Loudon Rd): the one that delivers to the office (12 Elmwood Rd, Menands is
# outside the Albany location's zone), and the menu Maria's orders come from ("#13 Grilled Chicken Salad").
SAATI = f"{SITE}/restaurants/latham/saati-deli-latham"
HEADERS = {"User-Agent": "Mozilla/5.0 (TAG Solutions assistant)"}


@dataclass
class Choice:
    name: str
    price: float = 0.0


@dataclass
class OptionGroup:
    name: str
    choices: list[Choice]
    min: int = 0
    limit: int = 0   # 0 = no limit


@dataclass
class Item:
    id: str
    name: str
    section: str
    price: float
    path: str
    groups: list[OptionGroup] = field(default_factory=list)


def _text(fragment: str) -> str:
    return " ".join(htmllib.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def _money(text: str) -> float:
    m = re.search(r"\$\s?(\d[\d,]*(?:\.\d+)?)", text or "")
    return float(m.group(1).replace(",", "")) if m else 0.0


def parse_menu(page: str) -> list[Item]:
    """Every item on the restaurant page, once (the Popular section repeats some)."""
    items: dict[str, Item] = {}
    for section_html in re.split(r'data-category-name="', page)[1:]:
        section = htmllib.unescape(section_html.split('"', 1)[0])
        if section.lower() == "popular":
            continue
        for path, item_id, name, price in re.findall(
                r'data-load-url="(/menu/\d+/(\d+))".*?<h4 title="([^"]*)".*?<span class="price">([^<]*)</span>',
                section_html, re.S):
            items.setdefault(item_id, Item(item_id, htmllib.unescape(name), section, _money(price), path))
    return list(items.values())


def _choice_name(label: str) -> str:
    """"Add Add Swiss Cheese (+$1.25)" -> "Add Swiss Cheese"."""
    name = re.sub(r"\s*\(\+?\$[\d.,]+\)\s*$", "", _text(label))
    return re.sub(r"^Add Add ", "Add ", name)


_BLOCK = r'<div[^>]*\boptions-block\b[^>]*>'


def parse_item(page: str) -> tuple[float, list[OptionGroup]]:
    """An item's own price (its page is the one checkout charges) and its option
    groups: name, how many may be picked, and each choice's price."""
    base = re.search(r'data-base-price="([\d.]+)"', page)
    page = re.sub(r"(?is)<(script|style).*?</\1>", "", page)
    groups = []
    for opening, body in re.findall(r"(" + _BLOCK + r")(.*?)(?=" + _BLOCK + r"|<h4[^>]*>\s*Special Requests|$)", page, re.S):
        title = re.search(r"<h4[^>]*>(.*?)</h4>", body, re.S)
        choices = [Choice(_choice_name(label), float(price or 0))
                   for price, label in re.findall(r'<input[^>]*data-incprice="([\d.]*)"[^>]*>.*?<span>(.*?)</span>', body, re.S)]
        if not (title and choices):
            continue
        name = re.sub(r"\s*Required\.?$", "", _text(title.group(1))).strip()
        if "isReq" in opening:           # "Sandwich Sides: Required" - pick exactly one
            low, cap = 1, 1
        else:
            low = int(m.group(1)) if (m := re.search(r'data-min="(\d+)"', opening)) else 0
            cap = int(m.group(1)) if (m := re.search(r'data-limit="(\d+)"', opening)) else 0
        groups.append(OptionGroup(name, choices, low, cap))
    return (float(base.group(1)) if base else 0.0), groups


def _words(text: str) -> set[str]:
    text = re.sub(r"^\s*\d+\.?\s*", "", text.lower())          # "05 Grilled Chkn Salad" -> "grilled chkn salad"
    text = text.replace("chkn", "chicken").replace("san ", "sandwich ").replace("&", " and ")
    return {w for w in re.findall(r"[a-z]+", text) if w not in {"a", "the", "with", "and", "of", "on"}}


class Menu:
    """The day's menu, read from Mealeo and kept in the store until tomorrow."""

    def __init__(self, store: Any, url: str = SAATI, session: requests.Session | None = None) -> None:
        self.store, self.url = store, url
        self.session = session or requests.Session()
        self._items: list[Item] | None = None

    def _get(self, url: str) -> str:
        resp = self.session.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        return resp.text

    def items(self) -> list[Item]:
        if self._items is None:
            key = f"mealeo:menu:{self.url}"
            cached = json.loads(self.store.get_state(key) or "{}")
            if cached.get("day") == date.today().isoformat():
                self._items = [Item(**{**i, "groups": [OptionGroup(g["name"], [Choice(**c) for c in g["choices"]], g["min"], g["limit"])
                                                       for g in i["groups"]]}) for i in cached["items"]]
            else:
                self._items = parse_menu(self._get(self.url))
                self._save()
        return self._items

    def _save(self) -> None:
        self.store.set_state(f"mealeo:menu:{self.url}", json.dumps(
            {"day": date.today().isoformat(), "items": [asdict(i) for i in self._items or []]}))

    def item(self, name: str) -> Item | None:
        """The item with this menu name, with its options loaded. The number Mealeo puts in
        front is optional ("Tuna Salad Sandwich" is "01 Tuna Salad Sandwich"), as long as
        only one item has that name."""
        wanted = (name or "").strip().lower()
        found = next((i for i in self.items() if i.name.lower() == wanted), None)
        if found is None:
            bare = lambda n: re.sub(r"^\s*\d+\.?\s*", "", n).strip().lower()
            same = [i for i in self.items() if bare(i.name) == bare(wanted)]
            found = same[0] if len({i.name for i in same}) == 1 else None
        if found and not found.groups:
            base, found.groups = parse_item(self._get(SITE + found.path))
            # The list shows prices about 10% under the item's own (an online discount, it
            # seems); price from the item page so an estimate never comes in under the charge.
            found.price = base or found.price
            self._save()
        return found

    def find(self, text: str, limit: int = 5) -> list[Item]:
        """Menu items that best match words like "grilled chicken salad"."""
        want = _words(text)
        scored = [(len(want & _words(i.name)) / max(len(want | _words(i.name)), 1), i) for i in self.items()]
        out, names = [], set()
        for score, i in sorted(scored, key=lambda s: -s[0]):
            if score > 0 and i.name not in names:     # some names appear twice (two sizes)
                names.add(i.name)
                out.append(i)
        return out[:limit]

    def listing(self, prices: bool = True) -> str:
        """The whole menu in a compact form, for reading someone's order against."""
        out, section, seen = [], None, set()
        for i in self.items():
            if i.section != section:
                section = i.section
                out.append(f"## {section}")
            if (i.section, i.name) in seen:
                continue
            seen.add((i.section, i.name))
            out.append(f"{i.name} ${i.price:.2f}" if prices else i.name)
        return "\n".join(out)


def price_line(item: Item, options: list[str]) -> tuple[float, list[str], list[str]]:
    """The price of an item with these options, the options by their menu names, and any
    not on it. A price copied along with a name ("Add Grilled Chicken +$4.25") is ignored."""
    total, named, unknown = item.price, [], []
    for wanted in options:
        bare = re.sub(r"\s*[\[(]?\+?\$[\d.,]+[\])]?\s*$", "", wanted).strip().lower()
        match = next((c for g in item.groups for c in g.choices if c.name.lower() == bare), None)
        if match is None:
            unknown.append(wanted)
        else:
            total += match.price
            named.append(match.name)
    return round(total, 2), named, unknown
