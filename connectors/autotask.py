"""Autotask REST client.

Started as a copy of tag-tool-suite's src/ingest/autotask_client.py (query
and zone discovery), plus what the assistant needs on top: entity
information, single-record get, create and update, and error messages that
keep Autotask's own explanation.
"""

from __future__ import annotations

import os
from typing import Any

import requests

PAGE_SIZE = 500  # Autotask's maximum per query


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
        # Autotask answers with e.g. "https://webservices1.autotask.net/ATServicesRest/".
        zone = str(zone).rstrip("/")
        if zone.lower().endswith("/atservicesrest"):
            zone += "/v1.0"
        elif not zone.lower().endswith("/atservicesrest/v1.0"):
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
        """Query and fully paginate an entity collection.

        Pages by id (results come back in id order): ask for 500, then for ids
        above the last one seen. Autotask's own nextPageUrl can't be trusted: GET
        on it answers 405, and POSTing the search to it pages forward for a while
        and then loops on garbage ids indefinitely on large result sets.
        """
        if include_fields and "id" not in include_fields:
            include_fields = ["id", *include_fields]
        url = self._api_url(f"{entity}/query")
        items: list[dict] = []
        last_id = None
        while True:
            want = PAGE_SIZE if not max_records else min(PAGE_SIZE, max_records - len(items))
            page_filters = filters + ([{"op": "gt", "field": "id", "value": last_id}] if last_id is not None else [])
            search: dict[str, object] = {"filter": page_filters, "MaxRecords": want}
            if include_fields:
                search["IncludeFields"] = include_fields
            batch = self._request_json("POST", url, json=search).get("items") or []
            items.extend(batch)
            if len(batch) < want or (max_records and len(items) >= max_records):
                break
            last_id = max(item["id"] for item in batch)
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
