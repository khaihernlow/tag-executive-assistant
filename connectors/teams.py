"""Teams 1:1 chats from the assistant's own account (assistant@).

The assistant messages people as itself ("Executive Assistant"), so this uses a
Graph client signed in as the assistant, not the one working in Dave's mailbox.
"""

from __future__ import annotations

import html
import re
from typing import Any


def _plain(content: str) -> str:
    text = re.sub(r"(?i)<br\s*/?>|</p>", "\n", content or "")
    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip()


class Teams:
    def __init__(self, graph: Any, me: str, store: Any) -> None:
        self.graph, self.me, self.store = graph, me.lower(), store
        self._my_id: str | None = None

    def my_id(self) -> str:
        if self._my_id is None:
            self._my_id = self.graph.get(f"/users/{self.me}", {"$select": "id"})["id"]
        return self._my_id

    def _member(self, upn: str) -> dict[str, Any]:
        return {"@odata.type": "#microsoft.graph.aadUserConversationMember", "roles": ["owner"],
                "user@odata.bind": f"https://graph.microsoft.com/v1.0/users('{upn}')"}

    def chat_with(self, upn: str) -> str:
        """The 1:1 chat with this person (Teams returns the existing one if there is one)."""
        key = f"teams:chat:{upn.lower()}"
        chat_id = self.store.get_state(key)
        if not chat_id:
            chat_id = self.graph.post("/chats", {"chatType": "oneOnOne",
                                                 "members": [self._member(self.me), self._member(upn)]})["id"]
            self.store.set_state(key, chat_id)
        return chat_id

    def send(self, upn: str, text: str) -> dict[str, Any]:
        body = "<br>".join(html.escape(line) for line in text.split("\n"))
        message = self.graph.post(f"/chats/{self.chat_with(upn)}/messages",
                                  {"body": {"contentType": "html", "content": body}})
        return {"id": message["id"], "at": message.get("createdDateTime")}

    def replies(self, upn: str, since: str) -> list[dict[str, str]]:
        """What this person wrote in the chat after `since` (ISO time), oldest first."""
        out = []
        for m in self.graph.get(f"/chats/{self.chat_with(upn)}/messages", {"$top": 30}).get("value", []):
            sender = ((m.get("from") or {}).get("user") or {})
            if m.get("messageType") != "message" or not sender or (m.get("createdDateTime") or "") <= since:
                continue
            if sender.get("id") == self.my_id():   # the assistant's own messages
                continue
            text = _plain((m.get("body") or {}).get("content", ""))
            if text:
                out.append({"text": text, "at": m["createdDateTime"]})
        return sorted(out, key=lambda r: r["at"])
