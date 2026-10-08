"""Who is this sender to Dave? Facts code can establish before any model judges an email.

Used by the junk sweep and meeting-request detection. A cold staffing pitch
("Re: IT Support role requirement... let me know a good time to talk") was
shown as a meeting request because neither classifier knew that Dave had
never emailed the sender, had already deleted their first email, and that the
"follow-up" was an automated sequence ("Reply 'Stop'..."). These facts are
cheap to check and decide most of those cases.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

# Sales-automation and bulk footers.
AUTOMATION = re.compile(r"reply\s+['\"‘’“”]?stop|unsubscribe|opt[ -]?out|manage (?:your )?preferences|"
                        r"don'?t want to (?:hear|receive)|prefer not to receive", re.I)
# Mailprotector stamps these onto messages; they're noise to a model reading the preview.
_BANNERS = re.compile(r"\[This email is from [^\]]*\]|New sender\.\s*Do you want to Trust or Silence them\?\s*(?:Trust\s+Silence)?|"
                      r"<https?://[^>\s]*>?", re.I)

_deleted_folder: dict[str, str] = {}


def clean_preview(text: str) -> str:
    return " ".join(_BANNERS.sub(" ", text or "").split())


def automated(text: str) -> bool:
    return bool(AUTOMATION.search(text or ""))


def deleted_folder_id(graph: Any) -> str:
    if graph.mailbox not in _deleted_folder:
        try:
            _deleted_folder[graph.mailbox] = graph.get(f"/users/{graph.mailbox}/mailFolders/deleteditems",
                                                       {"$select": "id"})["id"]
        except Exception:  # noqa: BLE001
            _deleted_folder[graph.mailbox] = ""
    return _deleted_folder[graph.mailbox]


def thread_history(graph: Any, message: dict[str, Any], internal_domain: str = "") -> dict[str, Any]:
    """What happened earlier in this conversation, from one lookup across every folder.

    started_by: the first sender, if they're a TAG colleague (name, date)
    dave_replied: when Dave last wrote in the thread
    deleted_before: an earlier message from this same sender is in Deleted Items and
                    nobody at TAG ever wrote in the thread (Dave or Maria already said no)
    """
    out: dict[str, Any] = {"started_by": None, "dave_replied": None, "deleted_before": False}
    thread, received = message.get("conversationId"), message.get("receivedDateTime")
    if not thread or not received:
        return out
    earlier = graph.get_all(f"/users/{graph.mailbox}/messages", {
        "$select": "id,from,receivedDateTime,parentFolderId",
        "$filter": f"conversationId eq '{thread}' and receivedDateTime lt {received}", "$top": 20}, limit=20)
    earlier.sort(key=lambda m: m.get("receivedDateTime") or "")
    sender = _address(message)
    mailbox = graph.mailbox.lower()

    def internal(address: str) -> bool:
        return address == mailbox or bool(internal_domain and address.endswith("@" + internal_domain))

    if earlier and internal(_address(earlier[0])) and _address(earlier[0]) != mailbox:
        first = earlier[0]
        out["started_by"] = {"name": ((first.get("from") or {}).get("emailAddress") or {}).get("name") or _address(first),
                             "date": first.get("receivedDateTime")}
    replies = [m for m in earlier if _address(m) == mailbox]
    if replies:
        out["dave_replied"] = replies[-1].get("receivedDateTime")
    if not any(internal(_address(m)) for m in earlier):
        deleted = deleted_folder_id(graph)
        out["deleted_before"] = bool(deleted) and any(
            m.get("parentFolderId") == deleted and _address(m) == sender for m in earlier)
    return out


def thread_deleted_before(graph: Any, message: dict[str, Any], internal_domain: str = "") -> bool:
    return thread_history(graph, message, internal_domain)["deleted_before"]


_emailed: dict[str, bool] = {}


def has_emailed(graph: Any, address: str) -> bool:
    """Has Dave ever sent mail to (or copied) this address? Exact, unlike the sampled
    correspondents list, which only covers his most recent few thousand sent emails."""
    if address not in _emailed:
        try:
            hits = graph.get(f"/users/{graph.mailbox}/mailFolders/sentitems/messages",
                             {"$search": f'"participants:{address}"', "$select": "id", "$top": 1})
            _emailed[address] = bool(hits.get("value"))
        except Exception:  # noqa: BLE001
            return False
    return _emailed[address]


def _address(message: dict[str, Any]) -> str:
    return (((message.get("from") or {}).get("emailAddress") or {}).get("address") or "").lower()


def _day(iso: str | None) -> str:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%b %d").replace(" 0", " ") if iso else ""


def relationship_label(info: dict[str, Any], history: dict[str, Any]) -> str:
    """One plain line: who this sender is to Dave, as the card shows it."""
    parts = []
    if history.get("started_by"):
        parts.append(f"{history['started_by']['name']} (TAG) started this thread {_day(history['started_by']['date'])}")
    if history.get("dave_replied"):
        parts.append(f"you replied {_day(history['dave_replied'])}")
    if parts:
        return ", ".join(parts)
    return info["label"]


def describe(address: str, correspondents: set[str], store: Any, internal_domain: str) -> dict[str, Any]:
    """{'kind', 'label', 'known'} for a sender, from what code already knows."""
    address = address.lower()
    if internal_domain and address.endswith("@" + internal_domain):
        return {"kind": "staff", "label": "TAG colleague", "known": True}
    if address in correspondents:
        return {"kind": "correspondent", "label": "you've emailed them before", "known": True}
    verdict = store.sender_verdicts([address]).get(address) if store is not None else None
    if verdict == "keep":
        return {"kind": "kept", "label": "a sender whose mail you keep", "known": True}
    if verdict == "junk":
        return {"kind": "junk", "label": "a sender you've junked", "known": False}
    return {"kind": "first_contact", "label": "first contact: you've never emailed them", "known": False}
