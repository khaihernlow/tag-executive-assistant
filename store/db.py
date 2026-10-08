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

CREATE TABLE IF NOT EXISTS briefs (
    event_id     TEXT PRIMARY KEY,
    subject      TEXT NOT NULL,
    starts_at    TEXT NOT NULL,          -- ISO, local time
    fingerprint  TEXT NOT NULL,          -- changes when the invite changes -> regenerate
    status       TEXT NOT NULL,          -- preparing | ready | failed
    brief        TEXT,                   -- JSON
    error        TEXT,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meeting_requests (
    message_id   TEXT PRIMARY KEY,
    thread_id    TEXT,
    status       TEXT NOT NULL,          -- ignored | new | replied | booked | dismissed
    received_at  TEXT NOT NULL,
    request      TEXT,                   -- JSON: who, purpose, length, timeframe... (null when ignored)
    action_id    TEXT,                   -- the reply/booking awaiting or given approval
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_requests_status ON meeting_requests(status, received_at);

CREATE TABLE IF NOT EXISTS senders (
    address     TEXT PRIMARY KEY,
    domain      TEXT NOT NULL,
    verdict     TEXT NOT NULL,          -- junk | keep
    source      TEXT NOT NULL,          -- junk_folder | filed | dave | undo
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_senders_domain ON senders(domain, verdict);

CREATE TABLE IF NOT EXISTS state (
    key         TEXT PRIMARY KEY,       -- small bits of worker state, e.g. the junk sweep checkpoint
    value       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory (
    key        TEXT PRIMARY KEY,   -- e.g. "alias:kai", "pref:day_start", "note:<id>"
    kind       TEXT NOT NULL,      -- alias | pref | note
    value      TEXT NOT NULL,      -- JSON
    updated_at TEXT NOT NULL
);
"""

_JSON_FIELDS = {"llm_messages", "display", "payload", "result", "value", "brief", "topic", "request"}


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
            self._migrate()

    def _migrate(self) -> None:
        """Additive changes for databases created by earlier versions."""
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(conversations)")}
        if "topic" not in columns:
            # What a conversation is about, e.g. {"brief": "<event id>"}
            self._conn.execute("ALTER TABLE conversations ADD COLUMN topic TEXT")
        self._conn.commit()

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

    def create_conversation(self, title: str = "", topic: dict | None = None) -> str:
        cid = uuid.uuid4().hex
        stamp = now_iso()
        self._execute("INSERT INTO conversations (id, title, created_at, updated_at, topic) VALUES (?, ?, ?, ?, ?)",
                      (cid, title, stamp, stamp, json.dumps(topic) if topic else None))
        return cid

    def find_conversation(self, topic: dict) -> dict[str, Any] | None:
        """The most recent conversation about this topic (e.g. one meeting's brief)."""
        return self._row(self._execute(
            "SELECT * FROM conversations WHERE topic = ? ORDER BY updated_at DESC LIMIT 1", (json.dumps(topic),)
        ).fetchone())

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

    # ── memory ───────────────────────────────────────────────────────────────

    def set_memory(self, key: str, kind: str, value: Any) -> None:
        self._execute(
            "INSERT INTO memory (key, kind, value, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET kind = excluded.kind, value = excluded.value, "
            "updated_at = excluded.updated_at",
            (key, kind, json.dumps(value), now_iso()),
        )

    def delete_memory(self, key: str) -> bool:
        return self._execute("DELETE FROM memory WHERE key = ?", (key,)).rowcount == 1

    def list_memory(self, kind: str | None = None) -> list[dict[str, Any]]:
        if kind:
            rows = self._execute("SELECT * FROM memory WHERE kind = ? ORDER BY key", (kind,)).fetchall()
        else:
            rows = self._execute("SELECT * FROM memory ORDER BY kind, key").fetchall()
        return [self._row(r) for r in rows]

    # ── briefs ───────────────────────────────────────────────────────────────

    def get_brief(self, event_id: str) -> dict[str, Any] | None:
        return self._row(self._execute("SELECT * FROM briefs WHERE event_id = ?", (event_id,)).fetchone())

    def claim_brief(self, event_id: str, subject: str, starts_at: str, fingerprint: str,
                    force: bool = False) -> bool:
        """Mark a brief as being prepared. False if it's already being prepared,
        or (unless forced) is ready for this exact version of the invite."""
        with self._lock:
            row = self._conn.execute("SELECT status, fingerprint FROM briefs WHERE event_id = ?", (event_id,)).fetchone()
            if row and row["status"] == "preparing":
                return False
            if row and not force and row["status"] == "ready" and row["fingerprint"] == fingerprint:
                return False
            self._conn.execute(
                "INSERT INTO briefs (event_id, subject, starts_at, fingerprint, status, updated_at) "
                "VALUES (?, ?, ?, ?, 'preparing', ?) ON CONFLICT(event_id) DO UPDATE SET subject = excluded.subject, "
                "starts_at = excluded.starts_at, fingerprint = excluded.fingerprint, status = 'preparing', "
                "error = NULL, updated_at = excluded.updated_at",
                (event_id, subject, starts_at, fingerprint, now_iso()),
            )
            self._conn.commit()
            return True

    def finish_brief(self, event_id: str, brief: dict | None = None, error: str | None = None) -> None:
        self._execute(
            "UPDATE briefs SET status = ?, brief = ?, error = ?, updated_at = ? WHERE event_id = ?",
            ("ready" if brief is not None else "failed", json.dumps(brief) if brief is not None else None,
             error, now_iso(), event_id),
        )

    def brief_statuses(self, event_ids: list[str]) -> dict[str, str]:
        if not event_ids:
            return {}
        marks = ",".join("?" * len(event_ids))
        rows = self._execute(f"SELECT event_id, status FROM briefs WHERE event_id IN ({marks})", tuple(event_ids)).fetchall()
        return {r["event_id"]: r["status"] for r in rows}

    def reset_stuck_briefs(self) -> None:
        """A restart mid-generation leaves 'preparing' rows behind; let them be retried."""
        self._execute("UPDATE briefs SET status = 'failed', error = 'interrupted' WHERE status = 'preparing'")

    # ── meeting requests ─────────────────────────────────────────────────────

    def seen_request_ids(self, message_ids: list[str]) -> set[str]:
        if not message_ids:
            return set()
        marks = ",".join("?" * len(message_ids))
        rows = self._execute(f"SELECT message_id FROM meeting_requests WHERE message_id IN ({marks})",
                             tuple(message_ids)).fetchall()
        return {r["message_id"] for r in rows}

    def save_request(self, message_id: str, thread_id: str, received_at: str, status: str,
                     request: dict | None = None) -> None:
        self._execute(
            "INSERT INTO meeting_requests (message_id, thread_id, status, received_at, request, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(message_id) DO NOTHING",
            (message_id, thread_id, status, received_at, json.dumps(request) if request else None, now_iso()),
        )

    def get_request(self, message_id: str) -> dict[str, Any] | None:
        return self._row(self._execute("SELECT * FROM meeting_requests WHERE message_id = ?", (message_id,)).fetchone())

    def open_requests(self) -> list[dict[str, Any]]:
        rows = self._execute("SELECT * FROM meeting_requests WHERE status = 'new' ORDER BY received_at DESC").fetchall()
        return [self._row(r) for r in rows]

    def update_request(self, message_id: str, **fields: Any) -> None:
        columns = {**fields, "updated_at": now_iso()}
        assignments = ", ".join(f"{k} = ?" for k in columns)
        values = [json.dumps(v) if k in _JSON_FIELDS else v for k, v in columns.items()]
        self._execute(f"UPDATE meeting_requests SET {assignments} WHERE message_id = ?", (*values, message_id))

    def requests_in(self, statuses: tuple[str, ...], since: str | None = None) -> list[dict[str, Any]]:
        marks = ",".join("?" * len(statuses))
        sql = f"SELECT * FROM meeting_requests WHERE status IN ({marks})"
        params: tuple = statuses
        if since:
            sql += " AND updated_at >= ?"
            params += (since,)
        rows = self._execute(sql + " ORDER BY received_at DESC", params).fetchall()
        return [self._row(r) for r in rows]

    # ── sender reputation (junk learning) ────────────────────────────────────

    # Dave's own decisions outrank what was learned from folders.
    _SOURCE_RANK = {"auto": 0, "junk_folder": 0, "filed": 1, "dave": 2, "undo": 2}

    def set_sender(self, address: str, verdict: str, source: str) -> None:
        address = address.strip().lower()
        if "@" not in address:
            return
        with self._lock:
            row = self._conn.execute("SELECT source FROM senders WHERE address = ?", (address,)).fetchone()
            if row and self._SOURCE_RANK.get(row["source"], 0) > self._SOURCE_RANK.get(source, 0):
                return  # a learned guess never overrides what Dave decided
            self._conn.execute(
                "INSERT INTO senders (address, domain, verdict, source, updated_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(address) DO UPDATE SET verdict = excluded.verdict, source = excluded.source, "
                "updated_at = excluded.updated_at",
                (address, address.split("@", 1)[1], verdict, source, now_iso()),
            )
            self._conn.commit()

    def sender_verdicts(self, addresses: list[str]) -> dict[str, str]:
        if not addresses:
            return {}
        marks = ",".join("?" * len(addresses))
        rows = self._execute(f"SELECT address, verdict FROM senders WHERE address IN ({marks})",
                             tuple(a.lower() for a in addresses)).fetchall()
        return {r["address"]: r["verdict"] for r in rows}

    def domain_counts(self, domain: str) -> dict[str, int]:
        rows = self._execute("SELECT verdict, COUNT(*) AS n FROM senders WHERE domain = ? GROUP BY verdict",
                             (domain.lower(),)).fetchall()
        return {r["verdict"]: r["n"] for r in rows}

    def sender_count(self) -> int:
        return self._execute("SELECT COUNT(*) FROM senders").fetchone()[0]

    # ── small worker state ───────────────────────────────────────────────────

    def get_state(self, key: str) -> str | None:
        row = self._execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_state(self, key: str, value: str) -> None:
        self._execute("INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                      (key, value))

    # ── pending actions that grow (one rolling clean-up slip) ────────────────

    def extend_pending_action(self, aid: str, summary: str, payload: dict) -> bool:
        cursor = self._execute("UPDATE actions SET summary = ?, payload = ? WHERE id = ? AND status = 'pending'",
                               (summary, json.dumps(payload), aid))
        return cursor.rowcount == 1

    def actions_since(self, kind: str, since: str) -> list[dict[str, Any]]:
        rows = self._execute("SELECT * FROM actions WHERE kind = ? AND created_at >= ? ORDER BY created_at DESC",
                             (kind, since)).fetchall()
        return [self._row(r) for r in rows]

    def update_action_result(self, aid: str, result: dict) -> None:
        self._execute("UPDATE actions SET result = ? WHERE id = ?", (json.dumps(result), aid))
