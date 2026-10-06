"""Autotask REST client.

Started as a copy of tag-tool-suite's src/ingest/autotask_client.py (query
and zone discovery), plus what the assistant needs on top: entity
information, single-record get, create and update, and error messages that
keep Autotask's own explanation.
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urljoin

import requests


class AutotaskAPIError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class AutotaskClient:
    ZONE_INFORMATION_URL = (
        "https://webservices.autotask.net/atservicesrest/v1.0/zoneInformation"
    )

    def __init__(
        self,
        username: str,
        secret: str,
        integration_code: str,
        zone_url: str | None = None,
        timeout: float = 30.0,
        session: requests.Session | None = None,
    ) -> None:
        if not all((username, secret, integration_code)):
            raise ValueError("Autotask username, secret, and integration code are required.")
        self.username = username
        self.secret = secret
        self.integration_code = integration_code
        self.zone_url = zone_url.rstrip("/") if zone_url else None
        self.timeout = timeout
        self.session = session or requests.Session()

    @classmethod
    def from_env(cls) -> "AutotaskClient":
        return cls(
            username=os.environ.get("AUTOTASK_USERNAME", ""),
            secret=os.environ.get("AUTOTASK_SECRET", ""),
            integration_code=os.environ.get("AUTOTASK_INTEGRATION_CODE", ""),
            zone_url=os.environ.get("AUTOTASK_ZONE_URL") or None,
        )

    @property
    def headers(self) -> dict[str, str]:
        return {
            "UserName": self.username,
            "Secret": self.secret,
            "ApiIntegrationCode": self.integration_code,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def discover_zone(self) -> str:
        try:
            response = self.session.get(
                self.ZONE_INFORMATION_URL, params={"user": self.username}, timeout=self.timeout
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise AutotaskAPIError("Unable to discover the Autotask API zone.") from exc

        zone = payload.get("url") or payload.get("webUrl")
        if not zone:
            raise AutotaskAPIError("Autotask zone response did not include a URL.")
        zone = str(zone).rstrip("/")
        if not zone.lower().endswith("/atservicesrest/v1.0"):
            zone += "/atservicesrest/v1.0"
        self.zone_url = zone
        return zone

    def _api_url(self, path: str) -> str:
        base = self.zone_url or self.discover_zone()
        return f"{base.rstrip('/')}/{path.lstrip('/')}"

    def _request_json(self, method: str, url: str, **kwargs: Any) -> dict:
        try:
            response = self.session.request(method, url, headers=self.headers, timeout=self.timeout, **kwargs)
        except requests.RequestException as exc:
            raise AutotaskAPIError(f"Autotask request failed for {method} {url}: {exc}") from exc
        if not response.ok:
            raise AutotaskAPIError(
                f"Autotask {method} {url} failed ({response.status_code}): {_error_text(response)}",
                response.status_code,
            )
        try:
            return response.json()
        except ValueError as exc:
            raise AutotaskAPIError(f"Autotask {method} {url} returned non-JSON.") from exc

    def query(
        self,
        entity: str,
        filters: list[dict],
        include_fields: list[str] | None = None,
        max_records: int | None = None,
    ) -> list[dict]:
        """Query and fully paginate an entity collection."""
        search: dict[str, object] = {"filter": filters}
        if include_fields:
            search["IncludeFields"] = include_fields
        if max_records:
            search["MaxRecords"] = max_records

        url = self._api_url(f"{entity}/query")
        payload = self._request_json("POST", url, json=search)
        items: list[dict] = []
        while True:
            items.extend(payload.get("items") or [])
            next_url = (payload.get("pageDetails") or {}).get("nextPageUrl")
            if not next_url or (max_records and len(items) >= max_records):
                break
            payload = self._request_json("GET", urljoin(url, next_url))
        return items[:max_records] if max_records else items

    def get(self, entity: str, record_id: int) -> dict | None:
        return self._request_json("GET", self._api_url(f"{entity}/{record_id}")).get("item")

    def entity_information(self, entity: str) -> dict:
        """What the API supports for an entity (canCreate/canQuery/...).

        This describes the entity, not the API user's security level: a
        True here does not prove this user is allowed to create.
        """
        return self._request_json("GET", self._api_url(f"{entity}/entityInformation")).get("info", {})

    def create(self, entity: str, body: dict) -> int:
        """Create a record; returns the new id."""
        payload = self._request_json("POST", self._api_url(entity), json=body)
        return int(payload["itemId"])

    def update(self, entity: str, body: dict) -> int:
        """PATCH a record; `body` must include its `id`."""
        payload = self._request_json("PATCH", self._api_url(entity), json=body)
        return int(payload["itemId"])


def _error_text(response: requests.Response) -> str:
    try:
        errors = response.json().get("errors")
        if errors:
            return "; ".join(str(e) for e in errors)
    except ValueError:
        pass
    return response.text[:300]
