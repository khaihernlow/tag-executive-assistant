"""Turn tool results into UI cards, in code.

The model's text is commentary; facts reach Dave through these cards, built
straight from tool output, so nothing can be dropped or misquoted on the way.
"""

from __future__ import annotations

import json
from typing import Any

from agent.loop import ToolTrace


def _load(trace: ToolTrace) -> dict[str, Any] | None:
    if trace.is_error:
        return None
    try:
        return json.loads(trace.output)
    except ValueError:
        return None


def build_cards(trace: list[ToolTrace]) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    opened_mail = any(t.name in ("read_email", "read_attachment") and not t.is_error for t in trace)

    for t in trace:
        data = _load(t)
        if data is None:
            continue

        if t.name in ("find_free_time", "find_mutual_time"):
            cards.append({
                "type": "slots",
                "duration_minutes": int(t.input.get("duration_minutes", 30)),
                "with": [a for a in t.input.get("attendees") or []],
                "slots": [{"start": w["from"], "until": w["to"]} for w in data.get("free_windows", [])],
                "note": data.get("note"),
            })

        elif t.name in ("list_calendar_events", "find_events") and data.get("events"):
            cards.append({"type": "events", "events": data.get("events", [])})

        elif t.name == "search_mail" and data.get("messages") and not opened_mail:
            # Once the model opened emails, those are the answer and the search list
            # is noise. Otherwise show exact matches, falling back to partial ones.
            exact = [m for m in data["messages"] if m.get("matched") != "some words"]
            cards.append({"type": "emails", "emails": exact or data["messages"]})

        elif t.name == "read_email":
            cards.append({
                "type": "email",
                "subject": data.get("subject"),
                "from": data.get("from"),
                "received": data.get("received"),
                "attachments": [a["name"] for a in data.get("attachments", [])],
            })

        elif t.name == "find_person" and data.get("note") != "Confident match.":
            cards.append({"type": "people", "note": data.get("note"), "people": data.get("matches", [])})

        elif t.name in ("remember", "forget"):
            items = [data["remembered"]] if "remembered" in data else data.get("forgot", [])
            if items:
                verb = "Remembered" if t.name == "remember" else "Forgot"
                cards.append({"type": "memory", "text": f"{verb}: " + "; ".join(items)})

        elif t.name == "sweep_junk":
            if data.get("proposal"):
                p = data["proposal"]
                cards.append({"type": "action", **{k: p.get(k) for k in
                                                    ("action_id", "kind", "status", "summary", "result", "error", "items")}})
            if data.get("unsure"):
                cards.append({"type": "unsure", "items": data["unsure"]})

        elif t.name == "find_opportunities" and data.get("opportunities"):
            cards.append({"type": "opportunities", "opportunities": data["opportunities"][:25]})

        elif t.name in ("create_event", "move_event", "cancel_event", "respond_to_invite",
                        "create_opportunity", "update_opportunity"):
            card = {"type": "action", **{k: data.get(k) for k in ("action_id", "kind", "status", "summary", "result", "error")}}
            for extra in ("display", "email"):  # the record's fields, or an editable note
                if data.get(extra):
                    card[extra] = data[extra]
            cards.append(card)

    return _dedupe(cards)


def _dedupe(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the last card of each repeated lookup (the model often refines a search)."""
    last_index: dict[str, int] = {}
    for i, card in enumerate(cards):
        if card["type"] not in ("action", "memory", "unsure"):
            last_index[card["type"]] = i
    return [c for i, c in enumerate(cards) if c["type"] in ("action", "memory", "unsure") or last_index[c["type"]] == i]
