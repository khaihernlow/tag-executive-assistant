"""Microsoft Graph access to Dave's mailbox and calendar.

Two auth modes (GRAPH_AUTH_MODE):
- "delegated" (default): Dave signed in once via scripts/connect_microsoft.py;
  tokens renew silently from the saved MSAL cache. See connectors/ms_auth.py.
- "app": client credentials, admin-granted and scoped to Dave's mailbox with
  Exchange RBAC for Applications. No user sign-in to keep alive.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any, Callable

import requests

GRAPH_URL = "https://graph.microsoft.com/v1.0"
TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"

EVENT_FIELDS = "id,subject,start,end,location,attendees,organizer,isAllDay,isCancelled,showAs,isOnlineMeeting,webLink"


class GraphError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class GraphClient:
    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        mailbox: str,
        session: requests.Session | None = None,
        timeout: float = 30.0,
        token_provider: Callable[[], str] | None = None,
    ) -> None:
        if not all((tenant_id, client_id, mailbox)) or not (client_secret or token_provider):
            raise ValueError("Graph tenant id, client id, mailbox and a client secret or token provider are required.")
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.mailbox = mailbox
        self.timeout = timeout
        self.session = session or requests.Session()
        self.token_provider = token_provider
        self._token: str | None = None
        self._token_expires_at = 0.0

    @classmethod
    def from_env(cls) -> "GraphClient":
        tenant_id = os.environ.get("GRAPH_TENANT_ID", "")
        client_id = os.environ.get("GRAPH_CLIENT_ID", "")
        mailbox = os.environ.get("GRAPH_MAILBOX", "")
        if os.environ.get("GRAPH_AUTH_MODE", "delegated").lower() == "app":
            return cls(tenant_id, client_id, os.environ.get("GRAPH_CLIENT_SECRET", ""), mailbox)

        from connectors.ms_auth import DelegatedTokenProvider

        provider = DelegatedTokenProvider(tenant_id, client_id, mailbox)
        return cls(tenant_id, client_id, "", mailbox, token_provider=provider)

    def token(self) -> str:
        if self.token_provider:
            # MSAL caches and renews delegated tokens itself.
            return self.token_provider()
        # Refresh a minute early so a request never goes out with a token
        # that expires in flight.
        if self._token and time.time() < self._token_expires_at - 60:
            return self._token
        resp = self.session.post(
            TOKEN_URL.format(tenant=self.tenant_id),
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "scope": "https://graph.microsoft.com/.default",
            },
            timeout=self.timeout,
        )
        if not resp.ok:
            raise GraphError(f"Token request failed ({resp.status_code}): {_error_text(resp)}", resp.status_code)
        data = resp.json()
        self._token = data["access_token"]
        self._token_expires_at = time.time() + int(data.get("expires_in", 3600))
        return self._token

    def _headers(self, extra: dict[str, str] | None) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token()}", **(extra or {})}

    def get(
        self,
        path_or_url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        url = path_or_url if path_or_url.startswith("https://") else f"{GRAPH_URL}{path_or_url}"
        resp = self.session.get(url, params=params, headers=self._headers(headers), timeout=self.timeout)
        if not resp.ok:
            raise GraphError(f"GET {url} failed ({resp.status_code}): {_error_text(resp)}", resp.status_code)
        return resp.json()

    def post(self, path: str, body: dict[str, Any], headers: dict[str, str] | None = None) -> dict[str, Any]:
        url = f"{GRAPH_URL}{path}"
        resp = self.session.post(url, json=body, headers=self._headers(headers), timeout=self.timeout)
        if not resp.ok:
            raise GraphError(f"POST {url} failed ({resp.status_code}): {_error_text(resp)}", resp.status_code)
        return resp.json() if resp.content else {}

    def get_all(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        limit: int = 500,
        headers: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page = self.get(path, params, headers)
        while True:
            items.extend(page.get("value", []))
            next_link = page.get("@odata.nextLink")
            if not next_link or len(items) >= limit:
                return items[:limit]
            # nextLink already carries the query string.
            page = self.get(next_link, headers=headers)

    def calendar_view(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        """Events (recurrences expanded) overlapping [start, end). Times come back in UTC."""
        return self.get_all(
            f"/users/{self.mailbox}/calendarView",
            {
                "startDateTime": _utc_iso(start),
                "endDateTime": _utc_iso(end),
                "$select": EVENT_FIELDS,
                "$orderby": "start/dateTime",
                "$top": 100,
            },
        )


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("Pass timezone-aware datetimes to Graph calls.")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _error_text(resp: requests.Response) -> str:
    try:
        error = resp.json().get("error")
        if isinstance(error, dict):
            return f"{error.get('code')}: {error.get('message')}"
        if error:
            return f"{error}: {resp.json().get('error_description', '')}"
    except ValueError:
        pass
    return resp.text[:300]
