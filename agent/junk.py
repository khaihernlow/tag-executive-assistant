"""Junk sweep: what Maria does when spam slips past the filter.

Dave's mail passes through an upstream filter (Mailprotector) and Exchange
skips its own spam checks, so SCL/SPF/DKIM headers carry no signal here.
The decision is built from what does:

  1. Relationship (code): anyone Dave has emailed, TAG staff and the people
     Graph ranks as his contacts are never junk. Most mail stops here.
  2. Bulk markers (code): List-Unsubscribe, campaign ids, Precedence: bulk.
  3. Content (model): only unknown senders are classified, as junk / keep /
     unsure, with instructions to keep anything that might be a real person.

A message is proposed only when code says "unknown sender" AND the model
says "junk". It becomes one approval slip; approving moves the messages to
Junk Email (never deletes), and they stay recoverable there.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

from agent.actions import ActionKind, Actions, public_action
from agent.people import internal_domain
from agent.tools import Tool
from llm.provider import ToolSpec

KIND = "move_to_junk"
INBOX_FIELDS = "id,subject,from,receivedDateTime,bodyPreview,internetMessageHeaders"
BULK_HEADERS = ("list-unsubscribe", "x-campaign", "x-campaignid", "x-mailgun-tag", "x-sg-eid", "x-hs-cta-tracking")


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


CORRESPONDENTS_TTL = 12 * 60 * 60  # who Dave writes to barely changes within a day
_correspondents: dict[str, tuple[float, set[str]]] = {}


def known_correspondents(graph: Any, days: int = 365) -> set[str]:
    """Everyone Dave has written to in the last year, plus his top contacts.

    Scanning a year of Sent Items takes ~20s, so the result is cached."""
    cached = _correspondents.get(graph.mailbox)
    if cached and time.monotonic() - cached[0] < CORRESPONDENTS_TTL:
        return cached[1]
    known = _scan_correspondents(graph, days)
    _correspondents[graph.mailbox] = (time.monotonic(), known)
    return known


def _scan_correspondents(graph: Any, days: int) -> set[str]:
    since = _iso(datetime.now(timezone.utc) - timedelta(days=days))
    known: set[str] = set()
    for m in graph.get_all(f"/users/{graph.mailbox}/mailFolders/sentitems/messages",
                           {"$select": "toRecipients,ccRecipients", "$filter": f"sentDateTime ge {since}",
                            "$top": 250}, limit=2000):
        for r in (m.get("toRecipients") or []) + (m.get("ccRecipients") or []):
            address = ((r.get("emailAddress") or {}).get("address") or "").lower()
            if address:
                known.add(address)
    for p in graph.get_all("/me/people", {"$top": 300, "$select": "scoredEmailAddresses"}, limit=300):
        for e in p.get("scoredEmailAddresses") or []:
            if e.get("address"):
                known.add(e["address"].lower())
    return known


def bulk_markers(headers: list[dict[str, str]]) -> list[str]:
    found = []
    for h in headers or []:
        name, value = h.get("name", "").lower(), (h.get("value") or "").lower()
        if name in BULK_HEADERS:
            found.append(name)
        elif name == "precedence" and value in ("bulk", "list", "junk"):
            found.append(f"precedence:{value}")
    return sorted(set(found))


def unknown_senders(messages: list[dict[str, Any]], known: set[str], domain: str) -> list[dict[str, Any]]:
    out = []
    for m in messages:
        sender = (m.get("from") or {}).get("emailAddress") or {}
        address = (sender.get("address") or "").lower()
        if not address or address in known or (domain and address.endswith("@" + domain)):
            continue
        out.append({
            "id": m["id"],
            "from_name": sender.get("name") or "",
            "from": address,
            "subject": m.get("subject") or "(no subject)",
            "preview": (m.get("bodyPreview") or "")[:300],
            "bulk": bulk_markers(m.get("internetMessageHeaders")),
            "received": m.get("receivedDateTime", ""),
        })
    return out


CLASSIFY = ToolSpec(
    name="classify_emails",
    description="Record a verdict for every email.",
    input_schema={
        "type": "object",
        "properties": {
            "verdicts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "n": {"type": "integer", "description": "the email's number"},
                        "verdict": {"type": "string", "enum": ["junk", "keep", "unsure"]},
                        "reason": {"type": "string", "description": "under 12 words"},
                    },
                    "required": ["n", "verdict", "reason"],
                },
            }
        },
        "required": ["verdicts"],
    },
)

CLASSIFY_PROMPT = """You triage email for Dave, CEO of TAG Solutions (an IT managed services provider).
These emails are from senders Dave has never written to. Decide for each:
- junk: unsolicited sales or marketing pitches, cold outreach selling services/leads/lists,
  newsletters and promotions, scams and phishing.
- keep: anything that could be a real person or business Dave deals with: prospects or clients
  asking about IT services, partners, vendors he buys from (invoices, renewals, account notices),
  recruiting he is engaged in, security or system alerts, banks, government, personal mail.
- unsure: anything else. When in doubt, keep or unsure, never junk.
A prospect asking TAG for IT help is the opposite of junk."""


def classify(llm: Any, candidates: list[dict[str, Any]]) -> dict[str, tuple[str, str]]:
    """{message id: (verdict, reason)}. Missing or malformed verdicts count as 'unsure'."""
    if not candidates:
        return {}
    listing = "\n\n".join(
        f"#{i}\nFrom: {c['from_name']} <{c['from']}>\nSubject: {c['subject']}\n"
        f"Bulk-mail headers: {', '.join(c['bulk']) or 'none'}\nPreview: {c['preview']}"
        for i, c in enumerate(candidates)
    )
    response = llm.complete([{"role": "user", "content": listing}], system=CLASSIFY_PROMPT,
                            tools=[CLASSIFY], tool_choice=CLASSIFY.name, max_tokens=4096)
    verdicts: dict[str, tuple[str, str]] = {}
    for call in response.tool_calls:
        for v in call.input.get("verdicts") or []:
            n = v.get("n")
            if isinstance(n, int) and 0 <= n < len(candidates) and v.get("verdict") in ("junk", "keep", "unsure"):
                verdicts[candidates[n]["id"]] = (v["verdict"], str(v.get("reason", ""))[:120])
    return verdicts


def sweep(graph: Any, llm: Any, days: int = 3, limit: int = 80) -> dict[str, Any]:
    since = _iso(datetime.now(timezone.utc) - timedelta(days=days))
    inbox = graph.get_all(f"/users/{graph.mailbox}/mailFolders/inbox/messages", {
        "$select": INBOX_FIELDS, "$filter": f"receivedDateTime ge {since}",
        "$orderby": "receivedDateTime desc", "$top": 50}, limit=limit)
    candidates = unknown_senders(inbox, known_correspondents(graph), internal_domain())
    verdicts = classify(llm, candidates)

    junk, unsure = [], []
    for c in candidates:
        verdict, reason = verdicts.get(c["id"], ("unsure", "no verdict"))
        item = {"id": c["id"], "from": c["from_name"] or c["from"], "address": c["from"],
                "subject": c["subject"], "reason": reason}
        if verdict == "junk":
            junk.append(item)
        elif verdict == "unsure":
            unsure.append(item)
    return {"checked": len(inbox), "unknown_senders": len(candidates), "junk": junk, "unsure": unsure}


def junk_kind(graph: Any) -> ActionKind:
    def execute(payload: dict[str, Any], action_id: str) -> dict[str, Any]:
        moved, failed = 0, []
        for item in payload["items"]:
            try:
                graph.post(f"/users/{graph.mailbox}/messages/{item['id']}/move", {"destinationId": "junkemail"})
                moved += 1
            except Exception as e:  # noqa: BLE001 - one bad message shouldn't stop the rest
                failed.append({"subject": item["subject"], "error": str(e)[:200]})
        return {"moved": moved, "failed": failed}

    return ActionKind(KIND, execute)


def summarize_junk(items: list[dict[str, Any]]) -> str:
    senders = sorted({i["from"] for i in items})
    shown = ", ".join(senders[:5]) + (f" and {len(senders) - 5} more" if len(senders) > 5 else "")
    return f"Move {len(items)} email{'s' if len(items) != 1 else ''} to Junk · from {shown}"


def junk_tools(graph: Any, llm: Any, actions: Actions) -> list[Tool]:
    def run(args: dict[str, Any]) -> dict[str, Any]:
        result = sweep(graph, llm, days=int(args.get("days", 3)))
        out: dict[str, Any] = {
            "checked": result["checked"],
            "unknown_senders": result["unknown_senders"],
            "unsure": [{k: i[k] for k in ("from", "subject", "reason")} for i in result["unsure"]],
        }
        if result["junk"]:
            action = actions.propose(KIND, summarize_junk(result["junk"]), {"items": result["junk"]})
            out["proposal"] = public_action(action)
            out["note"] = "Nothing moved yet: Dave approves the slip. Mention the unsure ones briefly."
        else:
            out["note"] = "No junk found."
        return out

    return [Tool(
        spec=ToolSpec(
            name="sweep_junk",
            description=(
                "Check Dave's inbox for spam/junk that got past the filter (like Maria does) and propose "
                "moving it to Junk. Senders Dave has corresponded with are never touched. Returns a pending "
                "approval plus a list of 'unsure' emails for Dave to glance at."
            ),
            input_schema={"type": "object", "properties": {
                "days": {"type": "integer", "description": "How far back to check, default 3"}}},
        ),
        handler=run,
    )]
