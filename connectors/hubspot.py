"""HubSpot (TAG's CRM: companies, contacts, deals, and the calls, meetings, notes and tasks on them).

A private-app token (HUBSPOT_PRIVATE_APP_TOKEN). Search is the workhorse: up to 100 records a
page, about 0.3s each, and limited to a few calls a second per account (shared with Zapier and
everything else TAG has connected), so 429s are waited out and retried. What the account looks
like (its fields, owners, pipelines) changes rarely and is cached for a few hours.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

import requests

API = "https://api.hubapi.com"
CACHE_SECONDS = 6 * 3600
# Object type ids, for links into HubSpot: app.hubspot.com/contacts/<portal>/record/<type>/<id>
TYPE_IDS = {"contacts": "0-1", "companies": "0-2", "deals": "0-3", "calls": "0-48", "meetings": "0-47",
            "notes": "0-46", "tasks": "0-27", "communications": "0-18"}


class HubSpotError(RuntimeError):
    def __init__(self, message: str, status: int = 0, category: str = ""):
        super().__init__(message)
        self.status, self.category = status, category


class HubSpotClient:
    def __init__(self, token: str, session: requests.Session | None = None):
        self.token = token
        self.http = session or requests.Session()
        self.http.headers.update({"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        self._cache: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()
        self._last_search = 0.0

    @classmethod
    def from_env(cls) -> "HubSpotClient | None":
        token = os.environ.get("HUBSPOT_PRIVATE_APP_TOKEN", "").strip()
        return cls(token) if token else None

    # ── HTTP ──

    def _call(self, method: str, path: str, *, params: dict | None = None, body: Any = None) -> Any:
        for attempt in range(5):
            resp = self.http.request(method, API + path, params=params, json=body, timeout=30)
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = float(resp.headers.get("Retry-After") or 0) or min(8.0, 0.6 * 2 ** attempt)
                time.sleep(wait)
                continue
            if resp.status_code >= 400:
                try:
                    data = resp.json()
                except ValueError:
                    data = {"message": resp.text[:300]}
                raise HubSpotError(f"HubSpot {method} {path} failed ({resp.status_code}): {data.get('message', '')}",
                                   resp.status_code, data.get("category", ""))
            return resp.json() if resp.content else {}
        raise HubSpotError(f"HubSpot {method} {path}: still rate-limited after retries", 429, "RATE_LIMITS")

    def get(self, path: str, params: dict | None = None) -> Any:
        return self._call("GET", path, params=params)

    def post(self, path: str, body: Any) -> Any:
        return self._call("POST", path, body=body)

    def put(self, path: str, body: Any = None) -> Any:
        return self._call("PUT", path, body=body)

    # ── search ──

    def search(self, obj: str, *, filters: list[dict] | None = None, filter_groups: list[dict] | None = None,
               query: str = "", properties: list[str] | None = None, sorts: list[dict] | None = None,
               limit: int = 100, after: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"limit": max(1, min(limit, 100))}
        if filter_groups is not None:
            body["filterGroups"] = filter_groups
        elif filters:
            body["filterGroups"] = [{"filters": filters}]
        if query:
            body["query"] = query
        if properties:
            body["properties"] = properties
        if sorts:
            body["sorts"] = sorts
        if after:
            body["after"] = after
        # Search is limited per second across the whole account: keep a little space between calls
        with self._lock:
            gap = time.monotonic() - self._last_search
            if gap < 0.22:
                time.sleep(0.22 - gap)
            self._last_search = time.monotonic()
        return self.post(f"/crm/v3/objects/{obj}/search", body)

    def search_all(self, obj: str, *, max_rows: int = 2000, **kwargs: Any) -> tuple[list[dict], int]:
        """Every page up to max_rows (HubSpot stops at 10,000). Returns (rows, total matching)."""
        rows: list[dict] = []
        after, total = None, 0
        while True:
            page = self.search(obj, limit=min(100, max_rows - len(rows)), after=after, **kwargs)
            total = page.get("total", 0)
            rows += page.get("results", [])
            after = ((page.get("paging") or {}).get("next") or {}).get("after")
            if not after or len(rows) >= max_rows or len(rows) >= 10000:
                return rows, total

    # ── what the account looks like (cached) ──

    def _cached(self, key: str, load) -> Any:
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < CACHE_SECONDS:
            return hit[1]
        value = load()
        self._cache[key] = (time.time(), value)
        return value

    def properties(self, obj: str) -> list[dict[str, Any]]:
        return self._cached(f"props:{obj}", lambda: self.get(f"/crm/v3/properties/{obj}")["results"])

    def owners(self) -> list[dict[str, Any]]:
        """Everyone who has owned records, including people who have left (marked archived)."""
        def load():
            out = []
            for archived in ("false", "true"):
                after = None
                while True:
                    page = self.get("/crm/v3/owners", {"limit": 100, "archived": archived, **({"after": after} if after else {})})
                    out += [{**o, "archived": archived == "true"} for o in page.get("results", [])]
                    after = ((page.get("paging") or {}).get("next") or {}).get("after")
                    if not after:
                        break
            return out
        return self._cached("owners", load)

    def call_outcomes(self) -> dict[str, str]:
        """Call outcome ids -> labels ("Connected", "Left voicemail"); the field itself has no options."""
        def load():
            try:
                return {d["id"]: d["label"] for d in self.get("/calling/v1/dispositions")}
            except HubSpotError:
                return {}
        return self._cached("call_outcomes", load)

    def pipelines(self, obj: str = "deals") -> list[dict[str, Any]]:
        return self._cached(f"pipelines:{obj}", lambda: self.get(f"/crm/v3/pipelines/{obj}")["results"])

    def portal_id(self) -> int:
        return self._cached("portal", lambda: self.get("/account-info/v3/details")["portalId"])

    def meeting_links(self) -> list[dict[str, Any]]:
        return self._cached("meeting_links", lambda: self.get("/scheduler/v3/meetings/meeting-links", {"limit": 100}).get("results", []))

    def record_url(self, obj: str, object_id: str) -> str:
        return f"https://app.hubspot.com/contacts/{self.portal_id()}/record/{TYPE_IDS.get(obj, obj)}/{object_id}"
