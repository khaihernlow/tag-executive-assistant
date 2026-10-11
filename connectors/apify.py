"""Apify: runs scraping "actors" (LinkedIn profiles, for now) and returns their results.

Spending is capped twice: every run carries a maximum charge, and runs stop for
the month once usage nears the budget (APIFY_MONTHLY_BUDGET_USD, default $4 of
the free plan's $5), so the credit can't run dry unnoticed.
"""

from __future__ import annotations

import os
import time
from typing import Any

import requests

API = "https://api.apify.com/v2"


class ApifyError(RuntimeError):
    pass


class ApifyClient:
    def __init__(self, token: str, budget_usd: float = 4.0, timeout: int = 120) -> None:
        self.token, self.budget, self.timeout = token, budget_usd, timeout
        self._usage: tuple[float, float] | None = None  # (checked at, usd)

    @classmethod
    def from_env(cls) -> "ApifyClient | None":
        token = os.environ.get("APIFY_TOKEN")
        if not token:
            return None
        return cls(token, float(os.environ.get("APIFY_MONTHLY_BUDGET_USD", "4")))

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def month_usage(self) -> float:
        """This month's spend in USD, checked at most every 10 minutes."""
        if self._usage and time.monotonic() - self._usage[0] < 600:
            return self._usage[1]
        resp = requests.get(f"{API}/users/me/usage/monthly", headers=self._headers(), timeout=60)
        resp.raise_for_status()
        data = resp.json().get("data") or {}
        usd = float(data.get("totalUsageCreditsUsdAfterVolumeDiscount")
                    or data.get("totalUsageCreditsUsdBeforeVolumeDiscount") or 0)
        self._usage = (time.monotonic(), usd)
        return usd

    def run(self, actor: str, run_input: dict[str, Any], max_charge_usd: float = 0.05) -> list[dict[str, Any]]:
        """Run an actor and wait for its results (a minute or two at most)."""
        spent = self.month_usage()
        if spent + max_charge_usd > self.budget:
            raise ApifyError(f"Apify budget reached for this month (${spent:.2f} of ${self.budget:.2f}).")
        resp = requests.post(
            f"{API}/acts/{actor.replace('/', '~')}/run-sync-get-dataset-items",
            params={"maxTotalChargeUsd": max_charge_usd, "timeout": self.timeout},
            json=run_input, headers=self._headers(), timeout=self.timeout + 30)
        if not resp.ok:
            raise ApifyError(f"Apify {actor} failed ({resp.status_code}): {resp.text[:300]}")
        if self._usage:
            self._usage = (self._usage[0], self._usage[1] + max_charge_usd)  # assume the worst until rechecked
        items = resp.json()
        return items if isinstance(items, list) else []
