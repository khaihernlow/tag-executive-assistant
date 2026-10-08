"""The action policy: every change to the outside world goes through here.

A write tool never acts directly. It validates its input in code and
*proposes* an action, which is stored as `pending` and shown to Dave as an
approval card. Only `approve()` executes it. Kinds listed in ACTION_AUTO
skip the approval and execute at once (still logged), which is how action
types graduate to autonomy once they've earned it.
"""

from __future__ import annotations

import os
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable

from store.db import Store, now_iso

# Set by the assistant around each turn so tools can link actions to the chat.
current_conversation: ContextVar[str | None] = ContextVar("current_conversation", default=None)


@dataclass(frozen=True)
class ActionKind:
    name: str
    execute: Callable[[dict[str, Any], str], dict[str, Any]]  # (payload, action_id) -> result
    editable: tuple[str, ...] = ()  # payload fields Dave may change on the slip (e.g. an email's text)
    on_done: Callable[[dict[str, Any], dict[str, Any]], None] | None = None  # (payload, result) after success


def auto_kinds() -> set[str]:
    return {k.strip() for k in os.environ.get("ACTION_AUTO", "").split(",") if k.strip()}


class Actions:
    def __init__(self, store: Store, kinds: list[ActionKind], auto: set[str] | None = None) -> None:
        self.store = store
        self.kinds = {kind.name: kind for kind in kinds}
        self.auto = auto if auto is not None else auto_kinds()

    def propose(self, kind: str, summary: str, payload: dict[str, Any]) -> dict[str, Any]:
        if kind not in self.kinds:
            raise ValueError(f"Unknown action kind: {kind}")
        aid = self.store.add_action(kind, summary, payload, current_conversation.get())
        if kind in self.auto:
            return self.approve(aid, decided_by="auto")
        return self.store.get_action(aid)

    def approve(self, aid: str, decided_by: str, edits: dict[str, Any] | None = None) -> dict[str, Any]:
        current = self.store.get_action(aid)
        if current is None:
            raise KeyError(aid)
        fields: dict[str, Any] = {"decided_at": now_iso(), "decided_by": decided_by}
        kind = self.kinds.get(current["kind"])
        if edits and kind and current["status"] == "pending":
            # Only fields the kind allows: an edited email body, never the recipient.
            allowed = {k: v for k, v in edits.items() if k in kind.editable and isinstance(v, str) and v.strip()}
            if allowed:
                fields["payload"] = {**current["payload"], **allowed}
        if not self.store.transition_action(aid, "pending", "executing", **fields):
            return self.store.get_action(aid)  # already decided: no double execution
        action = self.store.get_action(aid)
        try:
            result = self.kinds[action["kind"]].execute(action["payload"], aid)
        except Exception as e:  # noqa: BLE001 - failures are recorded, not raised to the UI
            self.store.transition_action(aid, "executing", "failed", error=f"{type(e).__name__}: {e}",
                                         executed_at=now_iso())
        else:
            self.store.transition_action(aid, "executing", "executed", result=result, executed_at=now_iso())
            if kind and kind.on_done:
                try:
                    kind.on_done(action["payload"], result)
                except Exception:  # noqa: BLE001 - follow-up bookkeeping must not undo a done action
                    pass
        return self.store.get_action(aid)

    def reject(self, aid: str, decided_by: str) -> dict[str, Any]:
        self.store.transition_action(aid, "pending", "rejected", decided_at=now_iso(), decided_by=decided_by)
        action = self.store.get_action(aid)
        if action is None:
            raise KeyError(aid)
        return action


def public_action(action: dict[str, Any]) -> dict[str, Any]:
    """What the model and UI see: never the raw payload internals beyond what's useful."""
    out = {
        "action_id": action["id"],
        "kind": action["kind"],
        "status": action["status"],
        "summary": action["summary"],
        "result": action.get("result"),
        "error": action.get("error"),
    }
    payload = action.get("payload") or {}
    if "comment" in payload:
        # Emails: the slip shows (and lets Dave edit) the exact text that will be sent.
        out["email"] = {"to": payload.get("to"), "subject": payload.get("subject"), "comment": payload["comment"]}
    items = payload.get("items")
    if items:
        # Batch actions (e.g. moving several emails): show each one on the slip.
        out["items"] = [{k: i.get(k) for k in ("from", "subject", "reason")} for i in items]
    return out
