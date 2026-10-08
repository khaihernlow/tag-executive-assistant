"""Dave's pipeline check: open opportunities that are past their close date.

When this was built, 48 of his 52 open opportunities were overdue. The idea:
a short clean-up queue (oldest first) with one-tap answers: push the close
date out a month, pick a date, put it on hold, or mark it lost. A tap is
Dave's decision, so it saves straight away.

Autotask is slow to query (company names, paging), so it's meant to be read
from a snapshot rather than live.

Not shown anywhere yet: a 48-item backlog didn't belong on Today. Kept for
wherever it ends up (a weekly review, say); Dave can ask in chat meanwhile.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import Any

from agent.actions import Actions, public_action
from agent.calendar import local_zone
from agent.opportunities import Directory, find_opportunities, propose_update

STATE = "pipeline_snapshot"
REFRESH_MINUTES = 60
PUSH_DAYS = 30
QUICK = {"push": "pushed", "date": "moved", "on_hold": "put on hold", "lost": "marked lost"}


def refresh_pipeline(directory: Directory, store: Any, today: date | None = None) -> dict[str, Any]:
    today = today or datetime.now(local_zone()).date()
    rows = find_opportunities(directory, today=today)
    overdue = sorted((o for o in rows if o["overdue"]), key=lambda o: o.get("close_iso") or "")
    snapshot = {"checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "open": len(rows), "overdue": overdue}
    store.set_state(STATE, json.dumps(snapshot))
    return snapshot


def pipeline_is_stale(store: Any) -> bool:
    raw = store.get_state(STATE)
    if not raw:
        return True
    checked = datetime.fromisoformat(json.loads(raw)["checked_at"])
    return datetime.now(timezone.utc) - checked > timedelta(minutes=REFRESH_MINUTES)


def pipeline_view(store: Any, today: date | None = None) -> dict[str, Any] | None:
    raw = store.get_state(STATE)
    if not raw:
        return None
    snapshot = json.loads(raw)
    today = today or datetime.now(local_zone()).date()
    for o in snapshot["overdue"]:
        if o.get("close_iso"):
            days = (today - date.fromisoformat(o["close_iso"])).days
            o["late"] = (f"{days} days" if days < 45 else f"{round(days / 30)} months") + " past close"
    return snapshot


def quick_update(directory: Directory, actions: Actions, store: Any, opportunity_id: int, choice: str,
                 close_date: str = "", decided_by: str = "dave", today: date | None = None) -> dict[str, Any]:
    """Dave tapped an answer on Today: save it now and drop it from the queue."""
    today = today or datetime.now(local_zone()).date()
    if choice == "push":
        args = {"close_date": (today + timedelta(days=PUSH_DAYS)).isoformat()}
    elif choice == "date":
        if not close_date:
            raise ValueError("Pick a date.")
        args = {"close_date": close_date}
    elif choice in ("on_hold", "lost"):
        args = {"stage": choice}
    else:
        raise ValueError(f"choice must be one of {', '.join(QUICK)}")
    slip = propose_update(directory, actions, {"opportunity_id": opportunity_id, **args}, today=today)
    done = actions.approve(slip["action_id"], decided_by=decided_by)
    if done["status"] == "executed":
        raw = store.get_state(STATE)
        if raw:
            snapshot = json.loads(raw)
            snapshot["overdue"] = [o for o in snapshot["overdue"] if o["opportunity_id"] != opportunity_id]
            store.set_state(STATE, json.dumps(snapshot))
    return public_action(done)
