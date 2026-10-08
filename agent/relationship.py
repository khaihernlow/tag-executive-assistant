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


def thread_deleted_before(graph: Any, message: dict[str, Any]) -> bool:
    """Was an earlier message of this conversation already deleted (Dave or Maria said no once)?"""
    thread, received = message.get("conversationId"), message.get("receivedDateTime")
    deleted = deleted_folder_id(graph)
    if not thread or not received or not deleted:
        return False
    earlier = graph.get_all(f"/users/{graph.mailbox}/messages", {
        "$select": "id,parentFolderId",
        "$filter": f"conversationId eq '{thread}' and receivedDateTime lt {received}", "$top": 10}, limit=10)
    return any(m.get("parentFolderId") == deleted for m in earlier)


def describe(address: str, correspondents: set[str], store: Any, internal_domain: str) -> dict[str, Any]:
    """{'kind', 'label', 'known'} for a sender, from what code already knows."""
    address = address.lower()
    if internal_domain and address.endswith("@" + internal_domain):
        return {"kind": "staff", "label": "TAG colleague", "known": True}
    if address in correspondents:
        return {"kind": "correspondent", "label": "someone you've emailed before", "known": True}
    verdict = store.sender_verdicts([address]).get(address) if store is not None else None
    if verdict == "keep":
        return {"kind": "kept", "label": "a sender whose mail you keep", "known": True}
    if verdict == "junk":
        return {"kind": "junk", "label": "a sender you've junked", "known": False}
    return {"kind": "first_contact", "label": "first contact: you've never emailed them", "known": False}
