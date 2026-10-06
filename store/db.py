"""Local persistence: conversations and actions (approvals + activity log).

SQLite for now (no setup, one file under data/). The interface is small on
purpose so it can move to the shared Postgres when deployed.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id           TEXT PRIMARY KEY,
    title        TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    llm_messages TEXT NOT NULL DEFAULT '[]',  -- full model history incl. tool calls
    display      TEXT NOT NULL DEFAULT '[]'   -- what the UI shows: text + cards
);

CREATE TABLE IF NOT EXISTS actions (
    id              TEXT PRIMARY KEY,
    conversation_id TEXT,
    kind            TEXT NOT NULL,
    summary         TEXT NOT NULL,
    payload         TEXT NOT NULL,
    status          TEXT NOT NULL,  -- pending | executing | executed | failed | rejected
    result          TEXT,
    error           TEXT,
    created_at      TEXT NOT NULL,
    decided_at      TEXT,
    decided_by      TEXT,
    executed_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_actions_status ON actions(status, created_at);
"""

_JSON_FIELDS = {"llm_messages", "display", "payload", "result"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_path() -> Path:
    return Path(os.environ.get("ASSISTANT_DB_PATH", "data/assistant.db"))


class Store:
    def __init__(self, path: Path | str | None = None) -> None:
        path = Path(path) if path else default_path()
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)

    # ── helpers ──────────────────────────────────────────────────────────────

    def _row(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        data = dict(row)
        for key in _JSON_FIELDS & data.keys():
            if data[key] is not None:
                data[key] = json.loads(data[key])
        return data

    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cursor = self._conn.execute(sql, params)
            self._conn.commit()
            return cursor

    # ── conversations ────────────────────────────────────────────────────────

    def create_conversation(self, title: str = "") -> str:
        cid = uuid.uuid4().hex
        stamp = now_iso()
        self._execute("INSERT INTO conversations (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                      (cid, title, stamp, stamp))
        return cid

    def get_conversation(self, cid: str) -> dict[str, Any] | None:
        return self._row(self._execute("SELECT * FROM conversations WHERE id = ?", (cid,)).fetchone())

    def save_conversation(self, cid: str, llm_messages: list, display: list, title: str | None = None) -> None:
        self._execute(
            "UPDATE conversations SET llm_messages = ?, display = ?, updated_at = ?, "
            "title = COALESCE(?, title) WHERE id = ?",
            (json.dumps(llm_messages), json.dumps(display), now_iso(), title, cid),
        )

    def list_conversations(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._execute(
            "SELECT id, title, created_at, updated_at FROM conversations ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ── actions ──────────────────────────────────────────────────────────────

    def add_action(self, kind: str, summary: str, payload: dict, conversation_id: str | None) -> str:
        aid = uuid.uuid4().hex
        self._execute(
            "INSERT INTO actions (id, conversation_id, kind, summary, payload, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
            (aid, conversation_id, kind, summary, json.dumps(payload), now_iso()),
        )
        return aid

    def get_action(self, aid: str) -> dict[str, Any] | None:
        return self._row(self._execute("SELECT * FROM actions WHERE id = ?", (aid,)).fetchone())

    def transition_action(self, aid: str, from_status: str, to_status: str, **fields: Any) -> bool:
        """Atomically move an action between states. False if it wasn't in
        `from_status` (e.g. a double-tapped Approve), so it can't run twice."""
        columns = {"status": to_status, **fields}
        assignments = ", ".join(f"{k} = ?" for k in columns)
        values = [json.dumps(v) if k in _JSON_FIELDS else v for k, v in columns.items()]
        cursor = self._execute(
            f"UPDATE actions SET {assignments} WHERE id = ? AND status = ?", (*values, aid, from_status)
        )
        return cursor.rowcount == 1

    def list_actions(self, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if status:
            rows = self._execute("SELECT * FROM actions WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                                 (status, limit)).fetchall()
        else:
            rows = self._execute("SELECT * FROM actions ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(r) for r in rows]
