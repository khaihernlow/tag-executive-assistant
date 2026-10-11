"""Contract renewals: what's coming up, and opening a renewal opportunity.

Read from Matt's tag_data (a nightly copy of Autotask contracts with their
billing lines), because it's a big, slow-changing overview: renewals are
months out, and the line items give each contract's current value. Opening a
renewal opportunity is a write, so it goes to Autotask through the same
approval card as any new opportunity, in TAG's "Renewals" category.

What counts: active Recurring Service contracts (12-48 month terms) ending in
the window. Fixed Price (projects), Time & Materials and Block Hours don't
renew. Contracts still marked Active after their end date are, at TAG, almost
all old records nobody closed (187 of 190 had no current billing), so they're
counted as a clean-up note, not listed as renewals.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from agent.actions import Actions
from agent.calendar import local_zone
from agent.opportunities import Directory, propose_create
from agent.tools import Tool
from llm.provider import ToolSpec

RENEWALS_SQL = """
with current_lines as (
    -- price is the line's total for its period (8,000 GB at $292 is $292), not a unit price
    select contract_id, sum(price) as period_value
    from ms_contract_line_items
    where start_date <= %(today)s and end_date >= %(today)s
    group by contract_id
)
select c.contract_id, c.autotask_id as company_id, m.name as customer, c.contract_name,
       c.contract_category_text as category, c.contract_period_text as billing,
       c.start_date::date as start_date, c.end_date::date as end_date,
       c.needs_renewal_review as needs_review, coalesce(l.period_value, 0) as period_value
from ms_customer_contracts c
left join current_lines l on l.contract_id = c.contract_id
left join ms_customers_meta m on m.autotask_id = c.autotask_id
where c.status_text = 'Active' and c.contract_type_text = 'Recurring Service'
  and c.end_date::date between %(today)s and %(until)s
order by c.end_date, c.contract_name
"""

STALE_SQL = """
select count(*) as n from ms_customer_contracts
where status_text = 'Active' and contract_type_text = 'Recurring Service' and end_date::date < %(today)s
"""

CONTRACT_SQL = RENEWALS_SQL.replace("and c.end_date::date between %(today)s and %(until)s",
                                    "and c.contract_id = %(contract_id)s")

PER_MONTH = {"Monthly": 1, "Quarterly": 3, "Semi-Annual": 6, "Yearly": 12}


def _monthly(row: dict[str, Any]) -> float:
    return round(float(row["period_value"] or 0) / PER_MONTH.get(row.get("billing") or "Monthly", 1), 2)


def _row(directory: Directory | None, r: dict[str, Any], today: date) -> dict[str, Any]:
    company_id = int(r["company_id"]) if str(r.get("company_id") or "").isdigit() else None
    customer = r.get("customer") or (directory.company_name(company_id) if directory and company_id else "")
    term = (r["end_date"] - r["start_date"]).days // 30 if r.get("start_date") else None
    return {
        "contract_id": r["contract_id"], "company_id": company_id, "customer": (customer or "").strip(),
        "contract": (r["contract_name"] or "").strip(), "category": r.get("category"),
        "ends": r["end_date"].strftime("%b %d, %Y").replace(" 0", " "), "ends_iso": r["end_date"].isoformat(),
        "days_left": (r["end_date"] - today).days, "term_months": term,
        "monthly_value": _monthly(r), "needs_review": r.get("needs_review") == "Yes",
    }


def upcoming_renewals(db: Any, directory: Directory | None, days: int = 90, customer: str = "",
                      today: date | None = None) -> dict[str, Any]:
    today = today or datetime.now(local_zone()).date()
    until = date.fromordinal(today.toordinal() + days)
    rows = db.rows(RENEWALS_SQL, {"today": today, "until": until})
    if directory:
        # Matt's customer list covers managed-services clients; others come from Autotask, in one lookup.
        directory.prefetch_companies([int(r["company_id"]) for r in rows
                                      if not r.get("customer") and str(r.get("company_id") or "").isdigit()])
    items = [_row(directory, r, today) for r in rows]
    if customer:
        items = [i for i in items if customer.lower() in i["customer"].lower()]
    stale = db.rows(STALE_SQL, {"today": today})[0]["n"]
    soon = [i for i in items if i["days_left"] <= 30]
    label = lambda i: f"{i['customer']}: {i['contract']} (${i['monthly_value']:,.0f}/mo, ends {i['ends']})"  # noqa: E731
    return {
        "window": f"next {days} days", "count": len(items),
        "monthly_value": round(sum(i["monthly_value"] for i in items), 2),
        "highlights": {
            "biggest": [label(i) for i in sorted(items, key=lambda i: -i["monthly_value"])[:3]],
            "flagged_needs_review": [label(i) for i in items if i["needs_review"]][:5],
            "needs_review_count": sum(i["needs_review"] for i in items),
            "ending_within_30_days": {"count": len(soon), "monthly_value": round(sum(i["monthly_value"] for i in soon), 2)},
        },
        "renewals": items,
        "note": (f"Active recurring contracts ending by {until.strftime('%b %d, %Y').replace(' 0', ' ')}, from the nightly "
                 f"copy of Autotask. {stale} more are still marked Active past their end date; nearly all are old "
                 "records nobody closed, not renewals."),
    }


def propose_renewal(db: Any, directory: Directory, actions: Actions, contract_id: int,
                    today: date | None = None) -> dict[str, Any]:
    """A renewal opportunity for one contract, on the usual approval card."""
    today = today or datetime.now(local_zone()).date()
    rows = db.rows(CONTRACT_SQL, {"today": today, "contract_id": contract_id})
    if not rows:
        raise ValueError("No active recurring contract with that id. Look it up with find_renewals first.")
    c = _row(directory, rows[0], today)
    if not c["company_id"]:
        raise ValueError("That contract isn't linked to an Autotask company.")
    value = f"${c['monthly_value']:,.2f}/month" if c["monthly_value"] else "value not on file"
    description = (f"Renewal of {c['contract']}, ending {c['ends']}.\n\n"
                   f"Customer:\n{c['customer']}\n\n"
                   f"Current contract:\n- {c['category'] or 'Recurring service'}\n- {c['term_months']}-month term\n"
                   f"- {value}\n\nSales Contact:\nDave Vener")
    return propose_create(directory, actions, {
        "company_id": c["company_id"], "title": f"Renewal: {c['contract']}", "stage": "business_case",
        "close_date": c["ends_iso"], "description": description, "category": "renewal",
    }, today=today)


def renewal_tools(db: Any, directory: Directory, actions: Actions) -> list[Tool]:
    def pending_note(result: dict[str, Any]) -> dict[str, Any]:
        if result["status"] == "pending":
            result["note"] = "Not saved yet. Dave sees an approval card; tell him it's ready for his approval."
        return result

    return [
        Tool(spec=ToolSpec(
            name="find_renewals",
            description=("Recurring client contracts coming up for renewal (from the nightly copy of Autotask), "
                         "soonest first, with each one's monthly value and whether it's flagged 'needs renewal review'. "
                         "Dave sees the full list on a card: reply with a short summary (count, value, the few "
                         "that stand out), not the list again."),
            input_schema={"type": "object", "properties": {
                "days": {"type": "integer", "description": "How far ahead to look (default 90)"},
                "customer": {"type": "string", "description": "Only this client"}}}),
            handler=lambda a: upcoming_renewals(db, directory, int(a.get("days") or 90), a.get("customer") or "")),
        Tool(spec=ToolSpec(
            name="start_renewal",
            description=("Propose a renewal opportunity in Autotask for one contract (Renewals category, closing on "
                         "the contract's end date). Saved once Dave approves; he can change stage, date and owner."),
            input_schema={"type": "object", "properties": {
                "contract_id": {"type": "integer", "description": "From find_renewals"}},
                "required": ["contract_id"]}),
            handler=lambda a: pending_note(propose_renewal(db, directory, actions, int(a["contract_id"])))),
    ]
