"""What Dave needs to see before answering an invite: what it's about, who's on it, the latest emails
around it, what happened last time with these people, where the company stands in HubSpot, and
whether a brief is ready. All looked up in code when he opens the invite; nothing is written by a model.

Most of his open invites are colleagues booking him into client meetings ("TAG Solutions & CrushBank -
Meeting" from Garrett); with only a title and a time, "which one was this again?" is the usual question.
"""

from __future__ import annotations

import re
from typing import Any

from agent.calendar import event_detail
from agent.changes import get_event
from agent.emailtext import excerpt
from agent.mail import LIST_FIELDS, _address, _local
from agent.people import internal_domain

MAX_EMAILS = 3
# Words in meeting titles that name nothing in particular ("CrushBank - Prep Meeting" is about CrushBank)
FILLER = {"prep", "lunch", "welcome", "kickoff", "kick", "huddle", "internal", "debrief", "planning", "strategy", "update",
          "status", "brainstorm", "team", "staff", "meeting", "call", "sync", "discussion", "review", "session", "partners",
          "partner", "presentation", "demo", "onsite", "visit", "follow", "intro", "check", "monthly", "weekly", "quarterly"}


def key_words(subject: str) -> list[str]:
    from agent.briefs import topic_words
    return [w for w in topic_words(subject) if w.lower().strip(".-'") not in FILLER]
CALENDAR_MAIL = re.compile(r"^(accepted|declined|tentative|tentatively accepted|canceled|cancelled|updated invitation|"
                           r"invitation|new time proposed|forwarded):", re.I)


def _emails(graph: Any, event: Any, outside: list[str]) -> list[dict[str, Any]]:
    """The latest emails with the outside people; for an internal meeting about a client, emails naming that client."""
    found: dict[str, dict[str, Any]] = {}
    for address in outside[:3]:
        for m in graph.get_all(f"/users/{graph.mailbox}/messages",
                               {"$search": f'"participants:{address}"', "$select": LIST_FIELDS, "$top": 10}, limit=10):
            found.setdefault(m["id"], m)
    if not found:
        words = key_words(event.subject)
        if words:
            # An internal meeting about a client: emails that actually name it, not just the newest mail
            for m in graph.get_all(f"/users/{graph.mailbox}/messages",
                                   {"$search": f'"{words[0]}"', "$select": LIST_FIELDS, "$top": 15}, limit=15):
                text = f"{m.get('subject') or ''} {m.get('bodyPreview') or ''}".lower()
                # Two of the title's names when it has two ("James & Morgan" is not any email naming a James)
                if sum(w.lower() in text for w in words) >= min(2, len(words)):
                    found.setdefault(m["id"], m)
    rows = []
    for m in sorted(found.values(), key=lambda m: m.get("receivedDateTime") or "", reverse=True):
        subject = m.get("subject") or ""
        if CALENDAR_MAIL.match(subject) or subject.strip().lower() == event.subject.strip().lower():
            continue          # the invite itself and calendar replies say nothing new
        rows.append({"id": m["id"], "subject": subject or "(no subject)", "from": _address(m.get("from")).get("name"),
                     "received": _local(m["receivedDateTime"]), "excerpt": excerpt(m.get("bodyPreview") or "", subject, 220),
                     "web_link": m.get("webLink")})
        if len(rows) >= MAX_EMAILS:
            break
    return rows


def _hubspot(hub: Any, outside: list[str]) -> list[dict[str, Any]]:
    from agent.research import FREEMAIL
    out, seen = [], set()
    for address in outside:
        domain = address.rsplit("@", 1)[-1]
        if domain in FREEMAIL or domain in seen:
            continue
        seen.add(domain)
        try:
            cid, name, _ = hub.company_id(domain)
            found = hub.hs.search("companies", filters=[{"propertyName": "hs_object_id", "operator": "EQ", "value": cid}],
                                  properties=hub.wanted_props("companies"), limit=1).get("results") or []
        except Exception:  # noqa: BLE001 - not in HubSpot, or HubSpot unreachable: just leave it out
            continue
        if found:
            row = hub.row("companies", found[0])
            out.append({k: row.get(k) for k in ("title", "url", "lifecycle_stage", "owner", "last_contacted")})
        if len(out) >= 2:
            break
    return out


def invite_context(graph: Any, store: Any, event_id: str, hubspot: Any = None) -> dict[str, Any]:
    event, raw = get_event(graph, event_id)
    domain = internal_domain()
    me = graph.mailbox.lower()
    detail = event_detail(event)
    people = [{"name": a.get("name") or a["email"], "email": a["email"],
               "outside": bool(a["email"]) and not (domain and a["email"].lower().endswith("@" + domain))}
              for a in detail["attendees"] if a.get("email", "").lower() != me]
    outside = [p["email"].lower() for p in people if p["outside"]]

    body = graph.get(f"/users/{graph.mailbox}/events/{event_id}", {"$select": "body"},
                     headers={"Prefer": 'outlook.body-content-type="text"'}).get("body", {}).get("content") or ""
    from agent.hubspot import _said
    about = _said("meetings", body)            # the agenda, without the Teams join boilerplate

    out: dict[str, Any] = {"about": about if len(about) >= 15 else "", "people": people[:20], "more_people": max(0, len(people) - 20)}
    out["emails"] = _emails(graph, event, outside)
    try:
        from agent.notes import earlier_notes
        if outside:
            last = earlier_notes(store, outside, limit=1)
            out["last_notes"] = [{"id": n["id"], "title": n["title"], "when": n["when"], "headline": n["headline"]} for n in last]
        else:
            # Internal: the last notes whose meeting named the same thing ("CrushBank"), not just the same colleagues
            words = key_words(event.subject)
            hits = [n for n in store.list_notes(query=words[0], limit=20)
                    if n.get("kind") == "meeting" and words[0].lower() in (n.get("title") or "").lower()] if words else []
            out["last_notes"] = [{"id": n["id"], "title": n["title"], "when": (n.get("started_at") or "")[:10],
                                  "headline": (n.get("digest") or {}).get("headline", "")} for n in hits[:1]]
    except Exception:  # noqa: BLE001
        out["last_notes"] = []
    out["hubspot"] = _hubspot(hubspot, outside) if hubspot is not None and outside else []
    brief = store.get_brief(event_id) if hasattr(store, "get_brief") else None
    out["brief_ready"] = bool(brief and brief.get("status") == "ready")
    clock = lambda t: t.strftime("%I:%M %p").lstrip("0")  # noqa: E731
    # Enough of the meeting for the app to open its brief, whatever day it is
    out["event"] = {"id": event.id, "subject": event.subject, "starts_at": event.start.isoformat(), "start": clock(event.start),
                    "end": clock(event.end), "location": event.location, "all_day": event.all_day, "note": None}
    return out
