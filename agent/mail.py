"""Mail tools: search Dave's mailbox and read a message.

Search and cleanup are code. Reading returns Graph's `uniqueBody` (just the
new part of the message, without the quoted thread) as plain text, so the
LLM sees what the sender actually wrote and not twenty quoted replies.
"""

from __future__ import annotations

import re

from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from agent.calendar import local_zone
from agent.documents import extract_text
from agent.emailtext import FORWARD, readable
from agent.tools import Tool
from llm.provider import ToolSpec

MAX_BODY_CHARS = 8000
LIST_FIELDS = "id,subject,from,receivedDateTime,bodyPreview,hasAttachments,isRead,conversationId,webLink"
READ_FIELDS = "id,subject,from,toRecipients,ccRecipients,receivedDateTime,uniqueBody,body,hasAttachments,conversationId,webLink"
TEXT_BODY = {"Prefer": 'outlook.body-content-type="text"'}


class GraphSource(Protocol):
    mailbox: str

    def get(self, path_or_url: str, params: dict[str, Any] | None = None,
            headers: dict[str, str] | None = None) -> dict[str, Any]: ...

    def get_all(self, path: str, params: dict[str, Any] | None = None, limit: int = 500,
                headers: dict[str, str] | None = None) -> list[dict[str, Any]]: ...


def build_search(sender: str | None, about: str | None, any_word: bool = False) -> str:
    """KQL for Graph `$search`, which must be wrapped in double quotes as a
    whole (`"from:x project"`). Terms are ANDed, or ORed with `any_word`.
    Graph rejects `$search` combined with date filters, so the date window
    is applied in code."""
    parts = []
    if sender:
        name = _clean(sender)
        if "@" in name:
            parts.append(f"from:{name}")  # exact address
        else:
            # from: takes one term; extra name words become plain terms.
            first, *rest = name.split()
            parts += [f"from:{first}", *rest]
    if about:
        words = _clean(about).split()
        if any_word and len(words) > 1:
            parts.append("(" + " OR ".join(words) + ")")
        else:
            parts.append(" ".join(words))
    return f'"{" ".join(parts)}"' if parts else ""


def _clean(value: str) -> str:
    return " ".join(value.replace('"', " ").replace("'", " ").split())


def _address(field: dict[str, Any] | None) -> dict[str, str]:
    email = (field or {}).get("emailAddress") or {}
    return {"name": email.get("name", ""), "email": (email.get("address") or "").lower()}


def _local(stamp: str) -> str:
    when = datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(local_zone())
    # Without the year a model reading "Jan 12" will guess one; show it unless it's this year.
    fmt = "%a %b %d %I:%M %p" if when.year == datetime.now(local_zone()).year else "%a %b %d %Y %I:%M %p"
    return when.strftime(fmt).replace(" 0", " ")


def summarize(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": raw["id"],
        "subject": raw.get("subject") or "(no subject)",
        "from": _address(raw.get("from")),
        "received": _local(raw["receivedDateTime"]),
        "preview": (raw.get("bodyPreview") or "")[:240],
        "has_attachments": bool(raw.get("hasAttachments")),
        # Real files only; inline images are signatures and logos.
        "attachments": [a.get("name") for a in raw.get("attachments") or [] if not a.get("isInline")],
        "unread": not raw.get("isRead", True),
    }


ATTACHMENT_NAMES = "attachments($select=name,isInline)"


def search_mail(
    graph: GraphSource,
    sender: str | None = None,
    about: str | None = None,
    since_days: int = 30,
    limit: int = 10,
    now: datetime | None = None,
    broaden: bool = True,
) -> list[dict[str, Any]]:
    """Newest first. Messages matching every word come first; if that finds
    few, messages matching some of the words follow (marked "some words"),
    because an all-words search misses the email that never uses one of
    them (a resume forward that doesn't say "interview")."""
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=since_days)
    path = f"/users/{graph.mailbox}/messages"

    def recent(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
        kept = [m for m in raw if datetime.fromisoformat(m["receivedDateTime"].replace("Z", "+00:00")) >= cutoff]
        return sorted(kept, key=lambda m: m["receivedDateTime"], reverse=True)

    def run(query: str) -> list[dict[str, Any]]:
        # $search can't take a date filter; fetch extra to survive the date cut.
        return recent(graph.get_all(
            path, {"$search": query, "$select": LIST_FIELDS, "$expand": ATTACHMENT_NAMES, "$top": 25}, limit=50))

    if sender and re.fullmatch(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", sender.strip()):
        # An exact address: filter on the sender field. Text search ($search) misses recent
        # mail: "from:cpadilla@..." returned one of her four emails, not her two newest.
        address = sender.strip().replace("'", "''")
        raw = graph.get_all(path, {
            "$filter": f"receivedDateTime ge {cutoff.strftime('%Y-%m-%dT%H:%M:%SZ')} "
                       f"and from/emailAddress/address eq '{address}'",
            "$orderby": "receivedDateTime desc",
            "$select": LIST_FIELDS,
            "$expand": ATTACHMENT_NAMES,
            "$top": 25,
        }, limit=50)
        if about:
            words = [w.lower() for w in re.findall(r"\w+", about) if len(w) > 2]
            raw = [m for m in raw if all(w in f"{m.get('subject', '')} {m.get('bodyPreview', '')}".lower() for w in words)]
        if raw or not about:
            return [summarize(m) | {"matched": "all words"} for m in recent(raw)[:limit]]

    query = build_search(sender, about)
    if not query:
        raw = graph.get_all(path, {
            "$filter": f"receivedDateTime ge {cutoff.strftime('%Y-%m-%dT%H:%M:%SZ')}",
            "$orderby": "receivedDateTime desc",
            "$select": LIST_FIELDS,
            "$expand": ATTACHMENT_NAMES,
            "$top": 25,
        }, limit=limit)
        return [summarize(m) for m in recent(raw)[:limit]]

    results = [summarize(m) | {"matched": "all words"} for m in run(query)]
    broader = build_search(sender, about, any_word=True)
    if broaden and len(results) < limit and broader != query:
        seen = {m["id"] for m in results}
        results += [summarize(m) | {"matched": "some words"} for m in run(broader) if m["id"] not in seen]
    return results[:limit]


def read_email(graph: GraphSource, message_id: str) -> dict[str, Any]:
    raw = graph.get(f"/users/{graph.mailbox}/messages/{message_id}", {"$select": READ_FIELDS}, TEXT_BODY)
    unique = ((raw.get("uniqueBody") or {}).get("content") or "").strip()
    full = ((raw.get("body") or {}).get("content") or "").strip()
    body = unique or full
    if FORWARD.match(raw.get("subject") or "") and len(readable(unique)) < 40:
        body = full   # uniqueBody leaves out what was forwarded
    truncated = len(body) > MAX_BODY_CHARS
    result = {
        **summarize({**raw, "bodyPreview": ""}),
        "to": [_address(r) for r in raw.get("toRecipients") or []],
        "cc": [_address(r) for r in raw.get("ccRecipients") or []],
        "body": body[:MAX_BODY_CHARS] + ("\n[...truncated]" if truncated else ""),
        "conversation_id": raw.get("conversationId"),
        "web_link": raw.get("webLink"),
    }
    result.pop("preview", None)
    if raw.get("hasAttachments"):
        attachments = graph.get_all(f"/users/{graph.mailbox}/messages/{message_id}/attachments",
                                    {"$select": "id,name,contentType,size,isInline"}, limit=20)
        # Inline images (signatures, logos) aren't documents worth reading.
        result["attachments"] = [{"id": a.get("id"), "name": a.get("name"), "type": a.get("contentType"),
                                  "size": a.get("size")}
                                 for a in attachments if not a.get("isInline")]
    return result


def _attachment(graph: Any, message_id: str, attachment: str) -> tuple[bytes, dict[str, Any]]:
    """`attachment` may be the attachment id or its file name (search results
    show names, so the model often has only the name)."""
    listing = graph.get_all(f"/users/{graph.mailbox}/messages/{message_id}/attachments",
                            {"$select": "id,name,contentType,size"}, limit=50)
    match = next((a for a in listing if a.get("id") == attachment), None) or next(
        (a for a in listing if (a.get("name") or "").lower() == attachment.strip().lower()), None)
    if match is None and len(listing) == 1 and not attachment.strip():
        match = listing[0]   # "the attachment" on an email with just one
    if match is None:
        names = ", ".join(a.get("name") or "?" for a in listing) or "none"
        raise ValueError(f"No attachment {attachment!r} on that email. Attachments: {names}")
    return graph.get_bytes(f"/users/{graph.mailbox}/messages/{message_id}/attachments/{match['id']}/$value"), match


def attachment_bytes(graph: Any, message_id: str, attachment: str) -> tuple[bytes, str]:
    """The file itself, for handing on (to a Hatz workflow)."""
    data, match = _attachment(graph, message_id, attachment)
    return data, match.get("name") or "attachment"


def read_attachment(graph: Any, message_id: str, attachment: str) -> dict[str, Any]:
    data, match = _attachment(graph, message_id, attachment)
    return {"name": match.get("name"),
            "text": extract_text(data, match.get("name") or "", match.get("contentType") or "")}


def mail_tools(graph: GraphSource) -> list[Tool]:
    def search(args: dict[str, Any]) -> dict[str, Any]:
        messages = search_mail(
            graph,
            sender=args.get("sender"),
            about=args.get("about"),
            since_days=int(args.get("since_days", 30)),
            limit=int(args.get("limit", 10)),
        )
        return {"messages": messages} if messages else {"messages": [], "note": "No matching email found."}

    def read(args: dict[str, Any]) -> dict[str, Any]:
        return read_email(graph, args["message_id"])

    def attachment(args: dict[str, Any]) -> dict[str, Any]:
        return read_attachment(graph, args["message_id"], args["attachment_id"])

    return [
        Tool(
            spec=ToolSpec(
                name="search_mail",
                description=(
                    "Search Dave's mailbox. Returns id, subject, sender, time, preview and attachment names; "
                    "all-words matches first, then some-words matches. Use read_email to see a full message. "
                    "Search by the most distinctive word (a surname) rather than a long phrase."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "sender": {"type": "string", "description": "Sender name or email address"},
                        "about": {"type": "string", "description": "Words to search for, e.g. 'Project X'"},
                        "since_days": {"type": "integer", "description": "How far back to look, default 30"},
                        "limit": {"type": "integer", "description": "Max results, default 10"},
                    },
                },
            ),
            handler=search,
        ),
        Tool(
            spec=ToolSpec(
                name="read_email",
                description="Read one email in full (new content only, quoted history removed), with recipients and attachments.",
                input_schema={
                    "type": "object",
                    "properties": {"message_id": {"type": "string", "description": "id from search_mail"}},
                    "required": ["message_id"],
                },
            ),
            handler=read,
        ),
        Tool(
            spec=ToolSpec(
                name="read_attachment",
                description=(
                    "Read the text of an email attachment (PDF, Word, text, calendar file). "
                    "Pass the message id and the attachment's file name (or id), e.g. to read a resume or proposal."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "message_id": {"type": "string"},
                        "attachment_id": {"type": "string",
                                          "description": "The attachment's id from read_email, or its file name"},
                    },
                    "required": ["message_id", "attachment_id"],
                },
            ),
            handler=attachment,
        ),
    ]
