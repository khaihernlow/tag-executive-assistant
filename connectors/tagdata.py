"""Read-only access to Matt's tag_data database (PostgreSQL).

It aggregates TAG's systems (Autotask contracts, customers, myITprocess,
security tools) nightly, on the same server the assistant deploys to. The
assistant reads big, slow-changing overviews from it (renewals, contract
revenue) and never writes to it; changes go to Autotask through its API.

Settings follow tag-tool-suite's: DB_POSTGRESDB_HOST, _PORT, _DATABASE,
_USER, _PASSWORD. Without a host, there's no tag_data and the features that
need it are simply absent.
"""

from __future__ import annotations

import os
from typing import Any


class TagData:
    def __init__(self, host: str, port: int, database: str, user: str, password: str) -> None:
        self.params = {"host": host, "port": port, "dbname": database, "user": user, "password": password,
                       "connect_timeout": 10}

    @classmethod
    def from_env(cls) -> "TagData | None":
        host = os.environ.get("DB_POSTGRESDB_HOST")
        if not host:
            return None
        return cls(host, int(os.environ.get("DB_POSTGRESDB_PORT", "5432")),
                   os.environ.get("DB_POSTGRESDB_DATABASE", "tag_data"),
                   os.environ.get("DB_POSTGRESDB_USER", ""), os.environ.get("DB_POSTGRESDB_PASSWORD", ""))

    def rows(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        import psycopg2
        import psycopg2.extras

        conn = psycopg2.connect(**self.params)
        try:
            conn.set_session(readonly=True, autocommit=True)  # never writes, even by mistake
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, params or {})
                return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()
