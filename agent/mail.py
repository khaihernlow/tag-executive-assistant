"""Mail tools: search Dave's mailbox and read a message.

Search and cleanup are code. Reading returns Graph's `uniqueBody` (just the
new part of the message, without the quoted thread) as plain text, so the
LLM sees what the sender actually wrote and not twenty quoted replies.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from agent.calendar import local_zone
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


def build_search(sender: str | None, about: str | None) -> str:
    """KQL for Graph `$search`, which must be wrapped in double quotes as a
    whole (`"from:x project"`); terms are ANDed. Graph rejects `$search`
    combined with date filters, so the date window is applied in code."""
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
        parts.append(_clean(about))
    return f'"{" ".join(parts)}"' if parts else ""


def _clean(value: str) -> str:
    return " ".join(value.replace('"', " ").replace("'", " ").split())


def _address(field: dict[str, Any] | None) -> dict[str, str]:
    email = (field or {}).get("emailAddress") or {}
    return {"name": email.get("name", ""), "email": (email.get("address") or "").lower()}


def _local(stamp: str) -> str:
    when = datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(local_zone())
    return when.strftime("%a %b %d %I:%M %p").replace(" 0", " ")


def summarize(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": raw["id"],
        "subject": raw.get("subject") or "(no subject)",
        "from": _address(raw.get("from")),
        "received": _local(raw["receivedDateTime"]),
        "preview": (raw.get("bodyPreview") or "")[:240],
        "has_attachments": bool(raw.get("hasAttachments")),
        "unread": not raw.get("isRead", True),
    }


def search_mail(
    graph: GraphSource,
    sender: str | None = None,
    about: str | None = None,
    since_days: int = 30,
    limit: int = 10,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=since_days)
    query = build_search(sender, about)
    path = f"/users/{graph.mailbox}/messages"
    if query:
        # $search results come back newest first; fetch extra to survive the date cut.
        raw = graph.get_all(path, {"$search": query, "$select": LIST_FIELDS, "$top": 25}, limit=50)
    else:
        raw = graph.get_all(path, {
            "$filter": f"receivedDateTime ge {cutoff.strftime('%Y-%m-%dT%H:%M:%SZ')}",
            "$orderby": "receivedDateTime desc",
            "$select": LIST_FIELDS,
            "$top": 25,
        }, limit=limit)
    recent = [m for m in raw
              if datetime.fromisoformat(m["receivedDateTime"].replace("Z", "+00:00")) >= cutoff]
    recent.sort(key=lambda m: m["receivedDateTime"], reverse=True)
    return [summarize(m) for m in recent[:limit]]


def read_email(graph: GraphSource, message_id: str) -> dict[str, Any]:
    raw = graph.get(f"/users/{graph.mailbox}/messages/{message_id}", {"$select": READ_FIELDS}, TEXT_BODY)
    body = ((raw.get("uniqueBody") or {}).get("content") or (raw.get("body") or {}).get("content") or "").strip()
    truncated = len(body) > MAX_BODY_CHARS
    result = {
        **summarize({**raw, "bodyPreview": ""}),
        "to": [_address(r) for r in raw.get("toRecipients") or []],
        "cc": [_address(r) for r in raw.get("ccRecipients") or []],
        "body": body[:MAX_BODY_CHARS] + ("\n[...truncated]" if truncated else ""),
        "conversation_id": raw.get("conversationId"),
    }
    result.pop("preview", None)
    if raw.get("hasAttachments"):
        attachments = graph.get_all(f"/users/{graph.mailbox}/messages/{message_id}/attachments",
                                    {"$select": "name,contentType,size"}, limit=20)
        result["attachments"] = [{"name": a.get("name"), "type": a.get("contentType"), "size": a.get("size")}
                                 for a in attachments]
    return result


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

    return [
        Tool(
            spec=ToolSpec(
                name="search_mail",
                description=(
                    "Search Dave's mailbox. Returns newest first with id, subject, sender, time and a preview. "
                    "Use read_email with an id to see the full message."
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
    ]
