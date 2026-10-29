"""HatzAI's apps and workflows: the tools people at TAG build in Hatz (an RFP responder,
an HR specialist...), run from here with the same API key the assistant talks through.

Apps answer in one call. Workflows run in the background on Hatz's side: start one,
then poll the job until it's complete. See https://api-docs.hatz.ai/workflows
"""

from __future__ import annotations

import os
import time
from typing import Any

import requests

BASE_URL = "https://ai.hatz.ai/v1"
PAGE = 100


class HatzAppsError(RuntimeError):
    pass


class HatzAppsClient:
    def __init__(self, api_key: str, timeout: int = 60) -> None:
        self.session = requests.Session()
        self.session.headers["X-API-Key"] = api_key
        self.timeout = timeout

    @classmethod
    def from_env(cls) -> "HatzAppsClient | None":
        key = os.environ.get("HATZAI_API_KEY")
        return cls(key) if key else None

    def _call(self, method: str, path: str, timeout: int | None = None, **kwargs: Any) -> dict[str, Any]:
        resp = self.session.request(method, BASE_URL + path, timeout=timeout or self.timeout, **kwargs)
        if not resp.ok:
            raise HatzAppsError(f"Hatz {method} {path} failed ({resp.status_code}): {resp.text[:300]}")
        return resp.json()

    def list_apps(self) -> list[dict[str, Any]]:
        """Every app and workflow the key can see. Hatz rejects a whole page when one app on it is
        malformed (an uploaded file missing its description), so such a page is read one by one."""
        out: list[dict[str, Any]] = []
        offset = 0
        while True:
            try:
                batch = self._call("GET", "/app/list", params={"limit": PAGE, "offset": offset}).get("data") or []
            except HatzAppsError:
                batch = []
                for i in range(offset, offset + PAGE):
                    try:
                        one = self._call("GET", "/app/list", params={"limit": 1, "offset": i}).get("data") or []
                    except HatzAppsError:
                        continue          # the broken one: skip it
                    if not one:
                        return out + batch
                    batch += one
                out += batch
                offset += PAGE
                continue
            out += batch
            if len(batch) < PAGE:
                return out
            offset += PAGE

    def search_apps(self, name: str) -> list[dict[str, Any]]:
        return self._call("GET", "/app/list", params={"name": name, "limit": 50}).get("data") or []

    def get_app(self, app_id: str) -> dict[str, Any]:
        return self._call("GET", f"/app/{app_id}")

    def query_app(self, app_id: str, inputs: dict[str, Any]) -> str:
        body = self._call("POST", f"/app/{app_id}/query", timeout=180, json={"inputs": inputs, "stream": False})
        choices = body.get("choices") or [{}]
        return ((choices[0].get("message") or {}).get("content") or "").strip()

    def run_workflow(self, app_id: str, inputs: dict[str, Any]) -> str:
        return self._call("POST", "/workflows/run", json={"app_id": app_id, "inputs": inputs})["job_id"]

    def job(self, job_id: str) -> dict[str, Any]:
        return self._call("GET", f"/workflows/{job_id}")

    def output_url(self, job_id: str, step_id: str) -> str:
        return self._call("POST", "/workflows/presigned-url",
                          json={"job_id": job_id, "step_id": step_id, "expires_in": 3600})["url"]

    def upload(self, data: bytes, name: str, scope_type: str, scope_id: str, wait: int = 120) -> str:
        """Upload a file for an app's file input; returns its id once Hatz has processed it."""
        resp = self.session.post(BASE_URL + "/files/upload", timeout=120, files={"file": (name, data)},
                                 data={"scope_type": scope_type, "scope_id": scope_id})
        if not resp.ok:
            raise HatzAppsError(f"Hatz upload of {name} failed ({resp.status_code}): {resp.text[:300]}")
        file_id = resp.json()["file_id"]
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            status = self._call("GET", f"/files/{file_id}/status").get("status")
            if status == "ready":
                return file_id
            if status == "failed":
                raise HatzAppsError(f"Hatz couldn't process {name}")
            time.sleep(2)
        raise HatzAppsError(f"Hatz was still processing {name} after {wait}s")
