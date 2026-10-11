"""Quotes: what TAG quoted whom, for how much, and whether the client has approved.

Quotes are built in QuoteWerks and land in Autotask (created by the QuoteWerks
user) as real quotes with line items, each linked to an opportunity. The
assistant only reads them, from the Autotask API (they aren't in tag_data).

Totals: each line is quantity x (unit price - unit discount) - line discount,
added up separately for one-time lines and recurring ones (shown per month).
Approval status is how TAG tracks a quote sent to the client: Not Requested,
Waiting Approval, Approved, Declined.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

from agent.calendar import local_zone
from agent.opportunities import OPEN_STATUSES, Directory, dave_resource_id, web_link
from agent.tools import Tool
from llm.provider import ToolSpec

QUOTE_FIELDS = ["id", "name", "quoteNumber", "companyID", "opportunityID", "approvalStatus", "createDate",
                "expirationDate", "primaryQuote", "creatorResourceID"]
ITEM_FIELDS = ["quoteID", "name", "quantity", "unitPrice", "unitDiscount", "lineDiscount", "periodType", "isOptional"]
APPROVAL = {1: "Not sent for approval", 2: "Waiting on client", 3: "Approved", 4: "Declined"}
# periodType -> months per period (1 is one-time)
PER_MONTH = {2: 1, 3: 3, 4: 6, 5: 12}


def _day(value: str | None) -> str:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%b %d, %Y").replace(" 0", " ") if value else ""


def _line_total(item: dict[str, Any]) -> float:
    qty = float(item.get("quantity") or 0)
    return qty * (float(item.get("unitPrice") or 0) - float(item.get("unitDiscount") or 0)) - float(item.get("lineDiscount") or 0)


def quote_totals(items: list[dict[str, Any]]) -> dict[str, float]:
    one_time = monthly = 0.0
    for item in items:
        if item.get("isOptional"):
            continue  # optional lines aren't part of the price unless the client picks them
        months = PER_MONTH.get(item.get("periodType"))
        if months:
            monthly += _line_total(item) / months
        else:
            one_time += _line_total(item)
    return {"one_time": round(one_time, 2), "monthly": round(monthly, 2)}


def find_quotes(at: Any, directory: Directory, company_id: int | None = None, about: str = "", mine: bool = False,
                status: str = "", days: int = 90, today: date | None = None) -> dict[str, Any]:
    """Quotes, newest first: for a company, on Dave's open opportunities, or recent."""
    today = today or datetime.now(local_zone()).date()
    filters: list[dict[str, Any]] = []
    opp_ids: list[int] = []
    if company_id:
        filters.append({"op": "eq", "field": "companyID", "value": company_id})
    if mine:
        opp_ids = [o["id"] for o in at.query("Opportunities", [
            {"op": "eq", "field": "ownerResourceID", "value": dave_resource_id()},
            {"op": "in", "field": "status", "value": list(OPEN_STATUSES)}], ["id"])]
        if not opp_ids:
            return {"count": 0, "quotes": []}
        filters.append({"op": "in", "field": "opportunityID", "value": opp_ids})
    if not company_id and not mine:
        filters.append({"op": "gte", "field": "createDate", "value": (today - timedelta(days=days)).isoformat()})
    if about:
        filters.append({"op": "contains", "field": "name", "value": about})
    wanted = {v.lower(): k for k, v in APPROVAL.items()}.get(status.lower()) if status else None
    if status and wanted is None:
        raise ValueError(f"status must be one of: {', '.join(APPROVAL.values())}")
    if wanted:
        filters.append({"op": "eq", "field": "approvalStatus", "value": wanted})
    quotes = at.query("Quotes", filters, QUOTE_FIELDS, 200)
    quotes.sort(key=lambda q: q.get("createDate") or "", reverse=True)
    quotes = quotes[:40]

    items: dict[int, list[dict[str, Any]]] = defaultdict(list)
    if quotes:
        for item in at.query("QuoteItems", [{"op": "in", "field": "quoteID", "value": [q["id"] for q in quotes]}], ITEM_FIELDS):
            items[item["quoteID"]].append(item)
    opps = {o["id"]: o for o in at.query("Opportunities", [{"op": "in", "field": "id", "value":
                                                            sorted({q["opportunityID"] for q in quotes if q.get("opportunityID")}) or [0]}],
                                         ["id", "title", "stage", "ownerResourceID"])}
    directory.prefetch_companies([q["companyID"] for q in quotes])
    rows = []
    for q in quotes:
        opp = opps.get(q.get("opportunityID"))
        expires = (q.get("expirationDate") or "")[:10]
        rows.append({
            "quote_number": q.get("quoteNumber"), "name": (q.get("name") or "").strip(),
            "company": directory.company_name(q["companyID"]),
            "status": APPROVAL.get(q.get("approvalStatus"), str(q.get("approvalStatus"))),
            "created": _day(q.get("createDate")), "expires": _day(q.get("expirationDate")),
            "expired": bool(expires) and expires < today.isoformat() and q.get("approvalStatus") != 3,
            **quote_totals(items.get(q["id"], [])),
            "lines": [f"{float(i.get('quantity') or 0):g} x {(i.get('name') or '').strip()[:60]}" for i in items.get(q["id"], [])][:8],
            "opportunity": opp["title"] if opp else "", "stage": directory.stage_label(opp["stage"]) if opp else "",
            "owner": directory.resource_name(opp["ownerResourceID"]) if opp else "",
            "link": web_link("opportunity", q["opportunityID"]) if q.get("opportunityID") else "",
        })
    return {"count": len(rows), "quotes": rows}


def quote_tools(at: Any, directory: Directory) -> list[Tool]:
    return [Tool(spec=ToolSpec(
        name="find_quotes",
        description=("Quotes in Autotask (built in QuoteWerks): for a company (company_id from find_company), on Dave's "
                     "open opportunities (mine=true), or the last N days. Each has one-time and monthly totals, its "
                     "lines, expiry, and status: Not sent for approval, Waiting on client, Approved, Declined. Read-only."),
        input_schema={"type": "object", "properties": {
            "company_id": {"type": "integer"}, "about": {"type": "string", "description": "Words in the quote name"},
            "mine": {"type": "boolean"}, "days": {"type": "integer", "description": "How far back (default 90)"},
            "status": {"type": "string", "enum": list(APPROVAL.values())}}}),
        handler=lambda a: find_quotes(at, directory, a.get("company_id"), a.get("about") or "", bool(a.get("mine")),
                                      a.get("status") or "", int(a.get("days") or 90)))]
