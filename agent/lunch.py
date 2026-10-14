"""Wednesday lunch for the leadership meeting, the way Maria ran it.

Every Wednesday the leadership team (Dave, Joe, Dan, Dom) has lunch from Saati Deli
at the noon meeting. Dave and Joe have standing orders; Dan and Dom are asked on
Teams each week (Dom has a backup order for when he doesn't answer). The order goes
in around 11:00 so it arrives for noon.

Dave isn't asked to approve lunch: it's routine. Code decides when the week is
normal (the meeting is on his calendar, the usual people, every item on the menu,
the total under a cap) and anything else is put to him. He's told what's
happening on Today and can change or skip it.

The model only reads people's replies ("the usual salad, extra chicken") into
menu names; code checks every name against the live menu and prices the order.

Until LUNCH_LIVE=1 it runs dry: it plans and shows everything, but sends nothing.
Placing the order on Mealeo comes later (it needs the assistant's Mealeo login).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, time, timedelta, timezone
from typing import Any

from agent.calendar import local_zone
from agent.mealeo import Menu, price_line
from llm.provider import ToolSpec

log = logging.getLogger("assistant.lunch")

DEFAULTS: dict[str, Any] = {
    "meeting": "leadership",               # the Wednesday meeting's title contains this
    "ask_at": "09:30", "remind_at": "10:30", "deadline": "10:45", "order_at": "11:00",
    "cap": 150.0,                          # dollars of food; above it, Dave decides
    "careful_runs": 2,                     # first orders wait for Dave's go before checkout
    "weekday": 2,                          # Wednesday (Monday is 0)
    "intro": "",                           # put before each message ("(Test run) ")
    "people": [
        {"name": "Dave", "upn": "dvener@tagsolutions.com", "ask": False,
         "usual": {"item": "05 Grilled Chkn Salad", "options": ["Honey Mustard"]}},
        {"name": "Joe", "upn": "jyetto@tagsolutions.com", "ask": False,
         "usual": {"item": "05 Grilled Chkn Salad", "options": ["Balsamic"]}},
        {"name": "Dan", "upn": "dlawrence@tagsolutions.com", "ask": True},
        {"name": "Dom", "upn": "dpetersen@tagsolutions.com", "ask": True,
         "backup": {"item": "06 BLT", "options": ["Wheat Bread", "Macaroni Salad", "Lettuce", "Tomato", "Onion",
                                                  "Mayo", "Add Swiss Cheese", "Add Avocado"],
                    "note": "Maria also added a side of French fries, which isn't on Saati's menu."}},
    ],
}

READ_REPLY = ToolSpec(
    name="read_lunch_reply",
    description="Read someone's reply about what they want for lunch from the menu.",
    input_schema={"type": "object", "properties": {
        "status": {"type": "string", "enum": ["order", "out", "unclear"],
                   "description": "order: they named a menu item; out: ONLY when they say they don't want lunch "
                                  "this week; unclear: you can't tell which item, or what they named isn't on the menu "
                                  "(then ask, suggesting the closest items)"},
        "items": {"type": "array", "description": "Each menu item they want (a sandwich and a soup are two)",
                  "items": {"type": "object", "properties": {
                      "item": {"type": "string", "description": "The exact menu name, copied from the menu, number included"},
                      "wants": {"type": "array", "items": {"type": "string"},
                                "description": "Each change or extra for this item in their own words: 'honey mustard', 'extra chicken', 'on wheat'"},
                  }, "required": ["item"]}},
        "question": {"type": "string", "description": "When unclear: one short question to ask them back"},
    }, "required": ["status"]},
)

PICK_OPTIONS = ToolSpec(
    name="pick_options",
    description="Map what someone asked for onto this item's option names.",
    input_schema={"type": "object", "properties": {
        "options": {"type": "array", "items": {"type": "string"}, "description": "Exact option names, copied from the list"},
        "unavailable": {"type": "array", "items": {"type": "string"}, "description": "Things they asked for that no option covers"},
    }, "required": ["options"]},
)


def config(store: Any) -> dict[str, Any]:
    saved = json.loads(store.get_state("lunch:config") or "{}")
    return {**DEFAULTS, **saved}


def live() -> bool:
    return os.environ.get("LUNCH_LIVE", "").strip() in ("1", "true", "yes")


def _at(day: datetime, hhmm: str) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime.combine(day.date(), time(h, m), tzinfo=day.tzinfo)


def _key(day: datetime) -> str:
    return f"lunch:{day.date().isoformat()}"


def load(store: Any, day: datetime) -> dict[str, Any] | None:
    raw = store.get_state(_key(day))
    return json.loads(raw) if raw else None


def save(store: Any, day: datetime, plan: dict[str, Any]) -> None:
    store.set_state(_key(day), json.dumps(plan))


def find_meeting(graph: Any, day: datetime, match: str) -> dict[str, Any] | None:
    """The leadership meeting on this day, if it's on Dave's calendar and not cancelled."""
    start, end = _at(day, "10:00"), _at(day, "15:00")
    for e in graph.get_all(f"/users/{graph.mailbox}/calendarView", {
            "startDateTime": start.isoformat(), "endDateTime": end.isoformat(),
            "$select": "subject,start,isCancelled", "$top": 50}, limit=50,
            headers={"Prefer": f'outlook.timezone="{local_zone().key}"'}):
        if match.lower() in (e.get("subject") or "").lower() and not e.get("isCancelled"):
            return {"subject": e["subject"], "start": e["start"]["dateTime"][:16]}
    return None


def describe(line: dict[str, Any]) -> str:
    opts = ", ".join(line.get("options") or [])
    return f"{line['item']}{f' ({opts})' if opts else ''}"


def describe_all(lines: list[dict[str, Any]]) -> str:
    return " + ".join(describe(line) for line in lines)


def total_of(lines: list[dict[str, Any]] | None) -> float:
    return round(sum(line.get("price") or 0 for line in lines or []), 2)


def _same(a: list[dict[str, Any]] | None, b: list[dict[str, Any]] | None) -> bool:
    key = lambda lines: sorted((line["item"], tuple(sorted(line.get("options", [])))) for line in lines or [])
    return bool(a and b) and key(a) == key(b)


def resolve(menu: Menu, llm: Any, item_name: str, wants: list[str]) -> dict[str, Any]:
    """A priced order line from a menu name and wishes in plain words. Code checks
    every name against the menu; the model only maps words to option names."""
    item = menu.item(item_name)
    if item is None:
        matches = menu.find(item_name, 3)
        return {"problem": f"\"{item_name}\" isn't on the menu" + (f"; closest: {', '.join(m.name for m in matches)}" if matches else "")}
    options: list[str] = []
    unavailable: list[str] = []
    if wants:
        listing = "\n".join(f"{g.name}{' (required, pick one)' if g.min else ''}: " + "; ".join(
            f"{c.name}{f' +${c.price:.2f}' if c.price else ''}" for c in g.choices) for g in item.groups)
        response = llm.complete([{"role": "user", "content": f"Item: {item.name}\nOptions:\n{listing}\n\nThey asked for: {'; '.join(wants)}"}],
                                system="Map each request to the option name that means it, copied exactly. 'Extra "
                                       "protein' or 'extra chicken' on a salad is the add-meat option. 'No X' or 'without X', "
                                       "when X is something you'd add (mayo, cheese), needs no option: just don't pick X. "
                                       "When there's a 'No X' option (a salad's 'No Red Onion'), pick that. Never pick an "
                                       "option they didn't ask for. Anything they want that no option covers goes in unavailable.",
                                tools=[PICK_OPTIONS], tool_choice=PICK_OPTIONS.name, max_tokens=500)
        for call in response.tool_calls:
            options, unavailable = list(call.input.get("options") or []), list(call.input.get("unavailable") or [])
    price, options, unknown = price_line(item, options)
    line = {"item": item.name, "options": options, "price": price}
    missing = [g.name for g in item.groups if g.min and not any(o.lower() in {c.name.lower() for c in g.choices} for o in options)]
    if missing:
        group = next(g for g in item.groups if g.name == missing[0])
        line["problem"] = f"needs a choice of {group.name.lower()}: {', '.join(c.name for c in group.choices)}"
        line["ask"] = f"The {item.name} comes with a choice of {group.name.lower()}: {', '.join(c.name for c in group.choices)}. Which would you like?"
    if unavailable + unknown:
        line["unavailable"] = unavailable + unknown
    return line


def transcript(talk: list[dict[str, str]] | str) -> str:
    """The lunch conversation so far, in order: the assistant's questions and their answers."""
    if isinstance(talk, str):
        return f"Them: {talk}"
    return "\n".join(f"{'You' if t['from'] == 'assistant' else 'Them'}: {t['text']}" for t in talk)


def read_reply(menu: Menu, llm: Any, talk: list[dict[str, str]] | str) -> dict[str, Any]:
    # Names only, no prices: the list shows about 10% under what's charged, and a question
    # quoting one price before the confirmation gives another looks careless.
    response = llm.complete([{"role": "user", "content": f"Menu:\n{menu.listing(prices=False)}\n\n"
                                                         f"The conversation so far (You is the assistant):\n{transcript(talk)}"}],
                            system="Someone was asked what they want for lunch from this deli menu. Read the whole "
                                   "conversation: their later messages answer your questions ('from the grill is good' "
                                   "after you offered two tuna items means the one from the grill). "
                                   "List each item they want (a sandwich and a soup are two items), copying its menu "
                                   "name exactly, number included. Put bread, dressing, sides and extras in that item's "
                                   "wants in their words: they're checked against the item's own options later, so never "
                                   "say whether an option exists. It's unclear only if you can't tell which item "
                                   "('same as usual', or two items fit equally, like a soup's bowl or quart); "
                                   "then ask which, naming the candidates. Their latest wish wins: if they change "
                                   "their mind after you confirmed, return the new order; if their latest message "
                                   "doesn't change anything ('thanks', 'perfect'), return the order you confirmed.",
                            tools=[READ_REPLY], tool_choice=READ_REPLY.name, max_tokens=500)
    verdict = next((c.input for c in response.tool_calls), {"status": "unclear", "question": "What would you like?"})
    if verdict.get("item") and not verdict.get("items"):          # a single item, the older shape
        verdict["items"] = [{"item": verdict["item"], "wants": verdict.get("wants") or []}]
    return verdict


class Lunch:
    def __init__(self, graph: Any, teams: Any, llm: Any, store: Any, menu: Menu | None = None) -> None:
        self.graph, self.teams, self.llm, self.store = graph, teams, llm, store
        self.menu = menu or Menu(store)

    # ── messages: sent for real only when live ──
    def _say(self, plan: dict[str, Any], person: dict[str, Any], text: str, now: datetime) -> None:
        text = text.replace("**", "")   # the model's bold shows as asterisks in a Teams message
        entry = {"to": person["name"], "text": text, "at": now.isoformat(timespec="minutes"), "sent": live()}
        if live():
            try:
                self.teams.send(person["upn"], text)
            except Exception as e:  # noqa: BLE001
                entry["error"] = str(e)[:200]
        plan.setdefault("messages", []).append(entry)
        if person["name"] in plan.get("people", {}):
            plan["people"][person["name"]].setdefault("talk", []).append({"from": "assistant", "text": text})

    def tick(self, now: datetime | None = None) -> dict[str, Any] | None:
        """One step of Wednesday's lunch; safe to call every few minutes."""
        now = now or datetime.now(local_zone())
        cfg = config(self.store)
        if now.weekday() != cfg["weekday"] or now < _at(now, "07:00") or now > _at(now, cfg["order_at"]) + timedelta(hours=3):
            return None
        plan = load(self.store, now)
        if plan is None:
            meeting = find_meeting(self.graph, now, cfg["meeting"])
            plan = {"day": now.date().isoformat(), "live": live(), "meeting": meeting,
                    "times": {k: cfg[k] for k in ("ask_at", "remind_at", "deadline", "order_at")},
                    "status": "planned" if meeting else "no_meeting",
                    "people": {p["name"]: {"upn": p["upn"], "state": "usual" if p.get("usual") else "to_ask"}
                               for p in cfg["people"]}}
            for p in cfg["people"]:
                if p.get("usual"):
                    plan["people"][p["name"]]["lines"] = [self._line(p["usual"])]
            save(self.store, now, plan)
        if plan["status"] in ("no_meeting", "skipped", "ordered", "ready", "needs_dave"):
            return plan
        people = {p["name"]: p for p in cfg["people"]}

        if plan["status"] == "planned" and now >= _at(now, cfg["ask_at"]):
            for name, who in plan["people"].items():
                if who["state"] == "to_ask":
                    self._say(plan, {**people[name], "upn": who["upn"]},
                              f"{cfg['intro']}Hi {name}, it's Dave's assistant. Lunch for today's leadership meeting is from Saati Deli "
                              f"(menu: https://www.mealeo.com/restaurants/albany/saati-deli-albany). What would you like? "
                              f"I'm ordering at {cfg['order_at']}, so please reply by {cfg['deadline']}." , now)
                    # Teams stamps messages in UTC ("2026-10-14T13:31:02.5Z"); compare like with like.
                    who.update(state="asked", asked_at=now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"))
            plan["status"] = "collecting"

        if plan["status"] == "collecting":
            self._read_replies(plan, people, now)
            if _at(now, cfg["remind_at"]) <= now < _at(now, cfg["deadline"]) and not plan.get("reminded"):
                for name, who in plan["people"].items():
                    if who["state"] == "asked":
                        self._say(plan, {**people[name], "upn": who["upn"]},
                                  f"Quick reminder: I'm ordering lunch at {cfg['order_at']}. What would you like?", now)
                plan["reminded"] = True
            # Open until the deadline even when everyone has answered: "actually make it a BLT" counts.
            if now >= _at(now, cfg["deadline"]):
                self._close(plan, people, cfg)
        save(self.store, now, plan)
        return plan

    def _line(self, order: dict[str, Any]) -> dict[str, Any]:
        item = self.menu.item(order["item"])
        if item is None:
            return {"item": order["item"], "options": order.get("options", []), "price": 0, "problem": "no longer on the menu"}
        price, named, unknown = price_line(item, order.get("options", []))
        line = {"item": item.name, "options": named, "price": price}
        if unknown:
            line["unavailable"] = unknown
        return line

    def _read_replies(self, plan: dict[str, Any], people: dict[str, Any], now: datetime) -> None:
        if not live():
            return   # dry run: nobody was asked
        for name, who in plan["people"].items():
            # Everyone who was asked, answered or not: a later message can change their order.
            if who["state"] not in ("asked", "clarifying", "ordered", "out"):
                continue
            new = self.teams.replies(who["upn"], who.get("read_until") or who["asked_at"])
            if not new:
                continue
            who["read_until"] = new[-1]["at"]
            text = "\n".join(r["text"] for r in new)
            who.setdefault("replies", []).append(text)
            who.setdefault("talk", []).append({"from": name, "text": text})
            verdict = read_reply(self.menu, self.llm, who["talk"])
            if verdict.get("status") == "out":
                who.update(state="out")
                self._say(plan, people[name] | {"upn": who["upn"]}, "Got it, no lunch for you this week.", now)
            elif verdict.get("status") == "order" and verdict.get("items"):
                lines = [resolve(self.menu, self.llm, i.get("item", ""), i.get("wants") or []) for i in verdict["items"]]
                stuck = [line for line in lines if line.get("problem")]
                unavailable = [u for line in lines for u in line.get("unavailable", [])]
                if stuck:
                    # Ask about the first thing that needs an answer; what was clear is remembered on the next read.
                    who.update(state="clarifying")
                    missing = f"{', '.join(unavailable)} isn't on the menu. " if unavailable else ""
                    self._say(plan, people[name] | {"upn": who["upn"]},
                              missing + (stuck[0].get("ask") or f"Sorry, {stuck[0]['problem']}. Which would you like?"), now)
                else:
                    unchanged = who["state"] == "ordered" and _same(who.get("lines"), lines)
                    who.update(state="ordered", lines=lines)
                    if not unchanged:   # "thanks!" after a confirmation needs no second one
                        extra = f" ({', '.join(unavailable)} isn't on the menu, so I left it off.)" if unavailable else ""
                        each = "; ".join(f"{describe(line)}, ${line['price']:.2f}" for line in lines)
                        total = f" ${total_of(lines):.2f} in all." if len(lines) > 1 else ""
                        self._say(plan, people[name] | {"upn": who["upn"]},
                                  f"Got it: {each}.{total}{extra} Ordering at {config(self.store)['order_at']}.", now)
            else:
                who.update(state="clarifying")
                self._say(plan, people[name] | {"upn": who["upn"]}, verdict.get("question") or "Which item would you like from the menu?", now)

    def _close(self, plan: dict[str, Any], people: dict[str, Any], cfg: dict[str, Any]) -> None:
        """Deadline: fill in backups and decide whether Dave needs to look."""
        for name, who in plan["people"].items():
            if who["state"] == "clarifying" and who.get("lines"):
                # They had an order, then started changing it and didn't finish: keep the one they confirmed.
                who.update(state="ordered", note="Kept their first order: the change wasn't finished by the deadline.")
            elif who["state"] in ("asked", "clarifying"):
                backup = people[name].get("backup")
                if backup:
                    who.update(state="backup", lines=[self._line(backup)], note=backup.get("note"))
                else:
                    who.update(state="no_reply")
        total = round(sum(total_of(w.get("lines")) for w in plan["people"].values() if w["state"] != "out"), 2)
        plan["total"] = total
        reasons = []
        if plan.get("meeting") is None and not plan.get("override"):
            reasons.append("the leadership meeting isn't on the calendar")
        if total > cfg["cap"]:
            reasons.append(f"food comes to ${total:.2f}, over the ${cfg['cap']:.0f} cap")
        for name, who in plan["people"].items():
            if who["state"] == "no_reply":
                reasons.append(f"{name} didn't reply (left off)")
            for line in who.get("lines") or []:
                if line.get("problem"):
                    reasons.append(f"{name}'s order: {line['problem']}")
        careful = int(json.loads(self.store.get_state("lunch:orders_placed") or "0")) < cfg["careful_runs"]
        if careful:
            reasons.append("one of the first orders, so it waits for your go before checkout")
        plan["reasons"] = reasons
        plan["status"] = "needs_dave" if reasons else "ready"


def today_view(store: Any, now: datetime | None = None) -> dict[str, Any] | None:
    """What Today shows about lunch on a Wednesday (or None)."""
    now = now or datetime.now(local_zone())
    plan = load(store, now)
    if plan is None:
        return None
    cfg = {**config(store), **plan.get("times", {})}   # the times this day's plan was made with
    lines = []
    for name, who in plan["people"].items():
        theirs = who.get("lines") or []
        unavailable = [u for line in theirs for u in line.get("unavailable", [])]
        lines.append({"name": name, "state": who["state"],
                      "what": describe_all(theirs) if theirs else None,
                      "price": total_of(theirs) if theirs else None,
                      "note": who.get("note") or (", ".join(unavailable) + " not on the menu" if unavailable else None)})
    return {"status": plan["status"], "live": plan.get("live", False), "meeting": plan.get("meeting"),
            "ask_at": cfg["ask_at"], "order_at": cfg["order_at"], "deadline": cfg["deadline"], "lines": lines,
            "total": plan.get("total") or round(sum(l["price"] or 0 for l in lines if l["state"] != "out"), 2),
            "reasons": plan.get("reasons", []), "messages": plan.get("messages", [])}


def skip_week(store: Any, now: datetime | None = None) -> dict[str, Any] | None:
    now = now or datetime.now(local_zone())
    plan = load(store, now)
    if plan and plan["status"] != "skipped":
        plan["before_skip"] = plan["status"]
        plan["status"] = "skipped"
        save(store, now, plan)
    return plan


def order_anyway(store: Any, now: datetime | None = None) -> dict[str, Any] | None:
    """Dave says lunch is on although the meeting isn't on the calendar (or he'd skipped it)."""
    now = now or datetime.now(local_zone())
    plan = load(store, now)
    if plan and plan["status"] in ("no_meeting", "skipped"):
        # Un-skipping picks up where it was (people may already have been asked).
        plan["status"] = plan.pop("before_skip", None) or "planned"
        if plan["status"] == "no_meeting":
            plan["status"] = "planned"
        plan["override"] = True
        save(store, now, plan)
    return plan
