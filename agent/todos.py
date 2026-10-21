"""What Dave owes, and what's owed to him: the action items from his meeting notes, in one list.

TeamsMaestro gives every action item an owner, and Dave's own recordings are written up the same
way, so the list is built from the notes rather than typed in. Code decides whose each item is
(TeamsMaestro writes him as "Dave Vener" or "David Vener"), how old it is, and whether it's done;
Dave ticks items off and that's remembered here. No due dates come from TeamsMaestro, so age stands
in: an open item two weeks old is flagged.

Not on Today (Today is that day only); it lives beside the meeting notes, and in chat.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta
from typing import Any

from agent.calendar import local_zone
from agent.tools import Tool
from llm.provider import ToolSpec

STATE = "todos_done"
OLD_DAYS = 14
EVERYONE = {"", "all employees", "everyone", "all", "team", "all staff", "unassigned"}


def _one_is_dave(name: str) -> bool:
    words = re.findall(r"[a-z]+", (name or "").lower())
    return bool(words) and words[0] in ("dave", "david") and (len(words) == 1 or words[-1] == "vener")


def owners(owner: str) -> list[str]:
    """'Dave Vener, Ximena Morales' / 'Matt and Dave Vener' -> each person."""
    return [p.strip() for p in re.split(r",|&|\band\b", owner or "") if p.strip()]


def is_dave(owner: str) -> bool:
    """Dave's, alone or shared ('Dave Vener, Ximena Morales'); not 'Dave Vener's assistant'."""
    return any(_one_is_dave(p) for p in owners(owner))


def _id(note_id: str, what: str) -> str:
    return hashlib.sha1(f"{note_id}|{what.strip().lower()}".encode()).hexdigest()[:12]


def _plain(text: str) -> str:
    return re.sub(r"\s*\[L\d+(?:\s*[-,]\s*L?\d+)*\]", "", text or "").strip()


def done_ids(store: Any) -> dict[str, str]:
    try:
        return json.loads(store.get_state(STATE) or "{}")
    except ValueError:
        return {}


def set_done(store: Any, item_id: str, done: bool) -> None:
    marks = done_ids(store)
    if done:
        marks[item_id] = datetime.now(local_zone()).isoformat(timespec="minutes")
    else:
        marks.pop(item_id, None)
    store.set_state(STATE, json.dumps(marks))


def collect(store: Any, days: int = 60, today: date | None = None) -> list[dict[str, Any]]:
    """Every action item from the last `days` of meeting notes, newest meeting first."""
    today = today or datetime.now(local_zone()).date()
    since = (today - timedelta(days=days)).isoformat()
    marks = done_ids(store)
    out = []
    for note in store.list_notes(limit=2000):
        if note.get("kind") != "meeting" or (note.get("started_at") or "")[:10] < since:
            continue
        held = (note.get("started_at") or "")[:10]
        for a in (note.get("digest") or {}).get("action_items") or []:
            what = _plain(a.get("what") or "")
            if not what:
                continue
            owner = (a.get("owner") or "").strip()
            item_id = _id(note["id"], what)
            age = (today - date.fromisoformat(held)).days if held else None
            shared = [p for p in owners(owner) if not _one_is_dave(p)] if is_dave(owner) else []
            out.append({"id": item_id, "what": what, "owner": owner, "mine": is_dave(owner), "shared_with": shared,
                        "everyone": owner.lower() in EVERYONE, "note_id": note["id"], "meeting": note["title"],
                        "held": held, "age_days": age, "due": a.get("due"),
                        "old": age is not None and age >= OLD_DAYS and item_id not in marks,
                        "done": item_id in marks, "done_at": marks.get(item_id)})
    out.sort(key=lambda t: (t["held"] or ""), reverse=True)
    return out


def view(store: Any, days: int = 60, query: str = "") -> dict[str, Any]:
    """The to-do list as the app shows it: Dave's open items, then what others owe, then what's done."""
    items = collect(store, days)
    q = query.strip().lower()
    if q:
        items = [t for t in items if q in f"{t['what']} {t['owner']} {t['meeting']}".lower()]
    mine = [t for t in items if t["mine"] and not t["done"]]
    others: dict[str, list[dict[str, Any]]] = {}
    for t in items:
        if not t["mine"] and not t["done"]:
            others.setdefault("Everyone / unassigned" if t["everyone"] else t["owner"], []).append(t)
    ordered = sorted(others.items(), key=lambda kv: (kv[0] == "Everyone / unassigned", -len(kv[1]), kv[0]))
    done = sorted((t for t in items if t["done"]), key=lambda t: t["done_at"] or "", reverse=True)[:30]
    return {"days": days, "mine": mine, "others": [{"owner": o, "items": its} for o, its in ordered], "done": done,
            "counts": {"mine": len(mine), "mine_old": sum(1 for t in mine if t["old"]),
                       "others": sum(len(v) for v in others.values())}}


def todo_tools(store: Any) -> list[Tool]:
    def find(args: dict[str, Any]) -> dict[str, Any]:
        days = int(args.get("days") or 30)
        who = (args.get("who") or "me").strip()
        items = [t for t in collect(store, days) if args.get("include_done") or not t["done"]]
        if who.lower() in ("me", "dave", "mine"):
            items = [t for t in items if t["mine"]]
        elif who.lower() in ("others", "everyone else", "team"):
            items = [t for t in items if not t["mine"]]
        else:
            items = [t for t in items if who.lower() in t["owner"].lower()]
        rows = [{k: t[k] for k in ("what", "owner", "meeting", "held", "age_days", "done", "old", "note_id")} for t in items[:60]]
        return {"who": who, "days": days, "count": len(items), "items": rows,
                "note": "From the action items in Dave's meeting notes (TeamsMaestro and recordings). 'old' means open "
                        "two weeks or more. There are no due dates; give the meeting and its date instead."}

    return [Tool(ToolSpec(
        "find_todos",
        "Action items from Dave's meetings: what Dave owes (who='me'), what someone owes him (who='<name>'), or "
        "everyone else's (who='others'). Use for 'what do I owe', 'what's on my plate from this week', "
        "'what is Garrett supposed to do'.",
        {"type": "object", "properties": {
            "who": {"type": "string", "description": "'me' (default), 'others', or a person's name"},
            "days": {"type": "integer", "description": "How far back, in days (default 30)"},
            "include_done": {"type": "boolean"}}}), handler=find)]
