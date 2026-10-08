"""Autotask opportunities: look them up, create them, move them along.

Maria creates most of Dave's opportunities (43 of his 72 in the last year), so
these follow her pattern: Dave as owner, the client contact attached, the
probability that goes with the stage, amount and cost at 0 until a quote
exists, and a description in the house layout (Customer / Primary Contact /
Scope / Sales Contact). Creating and updating wait on Dave's sign-off.
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta
from typing import Any

from agent.actions import ActionKind, Actions, public_action
from agent.calendar import local_zone
from agent.tools import Tool
from llm.provider import ToolSpec

CREATE_KIND = "create_opportunity"
UPDATE_KIND = "update_opportunity"

# Stage keys the model uses -> the leading number of TAG's stage labels, the
# probability TAG uses at that stage, and the opportunity status it implies.
STAGES = {
    "identification": ("1", 10, 1),
    "business_case": ("2", 30, 1),
    "scoping": ("3", 50, 1),
    "proposal": ("4", 70, 1),
    "final_approval": ("5", 90, 1),
    "won": ("6", 100, 3),        # Closed
    "on_hold": ("7", 0, 0),      # Not Ready To Buy
    "lost": ("8", 0, 2),         # Lost
}
STATUS_LABELS = {0: "Not ready to buy", 1: "Active", 2: "Lost", 3: "Closed", 4: "Implemented"}
OPEN_STATUSES = (0, 1)
FIELDS = ["id", "title", "companyID", "contactID", "ownerResourceID", "stage", "status", "probability",
          "projectedCloseDate", "amount", "createDate", "lastActivity", "description"]


def dave_resource_id() -> int:
    return int(os.environ.get("AUTOTASK_DAVE_RESOURCE_ID") or 0)


class Directory:
    """Autotask lookups the opportunity tools share, with picklists and names cached."""

    def __init__(self, at: Any) -> None:
        self.at = at
        self._stages: dict[str, int] | None = None
        self._stage_labels: dict[int, str] = {}
        self._companies: dict[int, str] = {}
        self._resources: dict[int, str] = {}

    # stages ─────────────────────────────────────────────────────────────────
    def stage_ids(self) -> dict[str, int]:
        """'proposal' -> TAG's picklist id, found by the label's leading number
        (skipping the '**DO NOT USE**' ones)."""
        if self._stages is None:
            info = self.at._request_json("GET", self.at._api_url("Opportunities/entityInformation/fields"))
            field = next(f for f in info["fields"] if f["name"] == "stage")
            by_number = {}
            for v in field.get("picklistValues") or []:
                self._stage_labels[int(v["value"])] = re.sub(r"^\d+\.\s*", "", v["label"]).strip()
                m = re.match(r"^(\d+)\.", v["label"])
                if m and v.get("isActive", True) and "DO NOT USE" not in v["label"]:
                    by_number[m.group(1)] = int(v["value"])
            self._stages = {key: by_number[num] for key, (num, _, _) in STAGES.items() if num in by_number}
        return self._stages

    def stage_label(self, stage_id: int) -> str:
        self.stage_ids()
        return self._stage_labels.get(stage_id, str(stage_id))

    def stage_key(self, stage_id: int) -> str | None:
        return next((k for k, v in self.stage_ids().items() if v == stage_id), None)

    # names ──────────────────────────────────────────────────────────────────
    def company_name(self, company_id: int) -> str:
        if company_id and company_id not in self._companies:
            rows = self.at.query("Companies", [{"op": "eq", "field": "id", "value": company_id}], ["id", "companyName"], 1)
            self._companies[company_id] = rows[0]["companyName"] if rows else str(company_id)
        return self._companies.get(company_id, "")

    def resource_name(self, resource_id: int) -> str:
        if not self._resources:
            for r in self.at.query("Resources", [{"op": "exist", "field": "id"}], ["id", "firstName", "lastName"]):
                self._resources[r["id"]] = f"{r['firstName']} {r['lastName']}".strip()
        return self._resources.get(resource_id, str(resource_id))

    def resource_by_name(self, name: str) -> int | None:
        self.resource_name(0)
        wanted = name.lower().strip()
        hits = [rid for rid, full in self._resources.items() if wanted in full.lower()]
        return hits[0] if len(hits) == 1 else None

    # companies and contacts ─────────────────────────────────────────────────
    def find_companies(self, name: str = "", email: str = "") -> list[dict[str, Any]]:
        ids: list[int] = []
        if email:
            domain = email.split("@")[-1].lower()
            for c in self.at.query("Contacts", [{"op": "contains", "field": "emailAddress", "value": "@" + domain}],
                                   ["id", "companyID"], 50):
                if c["companyID"] not in ids:
                    ids.append(c["companyID"])
        rows: list[dict[str, Any]] = []
        if ids:
            rows = self.at.query("Companies", [{"op": "in", "field": "id", "value": ids}],
                                 ["id", "companyName", "isActive", "companyType"])
        if name and not rows:
            rows = self.at.query("Companies", [{"op": "contains", "field": "companyName", "value": name}],
                                 ["id", "companyName", "isActive", "companyType"], 15)
        for r in rows:
            self._companies[r["id"]] = r["companyName"]
        rows.sort(key=lambda r: (not r.get("isActive"), len(r["companyName"])))
        return [{"company_id": r["id"], "name": r["companyName"], "active": bool(r.get("isActive"))} for r in rows[:10]]

    def find_contact(self, company_id: int, email: str = "", name: str = "") -> dict[str, Any] | None:
        filters: list[dict[str, Any]] = [{"op": "eq", "field": "companyID", "value": company_id}]
        rows = self.at.query("Contacts", filters, ["id", "firstName", "lastName", "emailAddress", "isActive"], 200)
        if email:
            rows = [r for r in rows if (r.get("emailAddress") or "").lower() == email.lower()]
        elif name:
            words = name.lower().split()
            rows = [r for r in rows if all(w in f"{r.get('firstName', '')} {r.get('lastName', '')}".lower() for w in words)]
        else:
            return None
        if len(rows) != 1:
            return None
        r = rows[0]
        return {"contact_id": r["id"], "name": f"{r.get('firstName', '')} {r.get('lastName', '')}".strip(),
                "email": r.get("emailAddress") or ""}


def web_link(record: str, record_id: int) -> str:
    """Opens the record in Autotask (zone LR01 -> ww1)."""
    base = os.environ.get("AUTOTASK_WEB_URL", "https://ww1.autotask.net").rstrip("/")
    code = {"opportunity": "OpenOpportunity&OpportunityID", "company": "OpenAccount&AccountID"}[record]
    return f"{base}/Autotask/AutotaskExtend/ExecuteCommand.aspx?Code={code}={record_id}"


def _day(value: str | None) -> str:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%b %d, %Y").replace(" 0", " ") if value else ""


def describe_opportunity(directory: Directory, o: dict[str, Any], today: date) -> dict[str, Any]:
    close = (o.get("projectedCloseDate") or "")[:10]
    status = o.get("status")
    return {
        "opportunity_id": o["id"],
        "title": o.get("title"),
        "company": directory.company_name(o.get("companyID")),
        "stage": directory.stage_label(o.get("stage")),
        "probability": o.get("probability"),
        "status": STATUS_LABELS.get(status, status),
        "close_date": _day(o.get("projectedCloseDate")),
        "overdue": bool(close) and status in OPEN_STATUSES and close < today.isoformat(),
        "amount": o.get("amount"),
        "owner": directory.resource_name(o.get("ownerResourceID")),
        "last_activity": _day(o.get("lastActivity")),
        "link": web_link("opportunity", o["id"]),
    }


def find_opportunities(directory: Directory, company_id: int | None = None, about: str = "", mine: bool = True,
                       open_only: bool = True, overdue_only: bool = False, today: date | None = None) -> list[dict[str, Any]]:
    today = today or datetime.now(local_zone()).date()
    filters: list[dict[str, Any]] = []
    if company_id:
        filters.append({"op": "eq", "field": "companyID", "value": company_id})
    if mine:
        filters.append({"op": "eq", "field": "ownerResourceID", "value": dave_resource_id()})
    if open_only or overdue_only:
        filters.append({"op": "in", "field": "status", "value": list(OPEN_STATUSES)})
    if overdue_only:
        filters.append({"op": "lt", "field": "projectedCloseDate", "value": today.isoformat()})
    if about:
        filters.append({"op": "contains", "field": "title", "value": about})
    if not filters:
        raise ValueError("Say whose opportunities, or for which company.")
    rows = directory.at.query("Opportunities", filters, FIELDS, 200)
    rows.sort(key=lambda o: o.get("projectedCloseDate") or "")
    return [describe_opportunity(directory, o, today) for o in rows]


# ── proposing changes ────────────────────────────────────────────────────────

def _at_date(day: date) -> str:
    """Autotask keeps these dates as midnight UTC."""
    return f"{day.isoformat()}T00:00:00Z"


def _close_date(value: str | None, today: date) -> date:
    if not value:
        return today + timedelta(days=30)
    try:
        return date.fromisoformat(value[:10])
    except ValueError as e:
        raise ValueError("close_date must be YYYY-MM-DD") from e


def propose_create(directory: Directory, actions: Actions, args: dict[str, Any], today: date | None = None) -> dict[str, Any]:
    today = today or datetime.now(local_zone()).date()
    title = (args.get("title") or "").strip()
    if not title:
        raise ValueError("title is required")
    company_id = int(args.get("company_id") or 0)
    company = directory.company_name(company_id) if company_id else ""
    if not company or company == str(company_id):
        raise ValueError("Unknown company_id. Look it up with find_company first.")
    stage_key = args.get("stage") or "business_case"
    if stage_key not in STAGES or stage_key not in directory.stage_ids():
        raise ValueError(f"stage must be one of {', '.join(STAGES)}")
    _, probability, status = STAGES[stage_key]
    owner = dave_resource_id()
    if args.get("owner_name"):
        owner = directory.resource_by_name(args["owner_name"]) or 0
        if not owner:
            raise ValueError(f"No single Autotask user matches '{args['owner_name']}'.")
    contact = None
    if args.get("contact_email") or args.get("contact_name"):
        contact = directory.find_contact(company_id, args.get("contact_email", ""), args.get("contact_name", ""))
    close = _close_date(args.get("close_date"), today)

    body = {
        "title": title, "companyID": company_id, "ownerResourceID": owner,
        "stage": directory.stage_ids()[stage_key], "status": status, "probability": probability,
        "startDate": _at_date(today), "projectedCloseDate": _at_date(close),
        "amount": float(args.get("amount") or 0), "cost": 0.0, "useQuoteTotals": True, "opportunityCategoryID": 1,
        "description": (args.get("description") or "").strip(),
    }
    if contact:
        body["contactID"] = contact["contact_id"]

    warnings, new_contact = [], None
    name_parts = (args.get("contact_name") or "").split()
    if not contact and args.get("contact_email") and len(name_parts) >= 2:
        # Not in Autotask yet: add them as Maria would, then link them.
        new_contact = {"companyID": company_id, "firstName": name_parts[0], "lastName": " ".join(name_parts[1:]),
                       "emailAddress": args["contact_email"].strip(), "isActive": 1}
        if args.get("contact_title"):
            new_contact["title"] = args["contact_title"].strip()
    elif (args.get("contact_email") or args.get("contact_name")) and not contact:
        warnings.append(f"contact {args.get('contact_email') or args.get('contact_name')} isn't in Autotask under {company}; "
                        "created without a contact (give both name and email to add them)")
    existing = find_opportunities(directory, company_id=company_id, mine=False, today=today)
    if existing:
        names = ", ".join(f"“{o['title']}” ({o['stage']})" for o in existing[:3])
        warnings.append(f"{company} already has {len(existing)} open: {names}")
    who = (f" · contact {contact['name']}" if contact else
           f" · adds {args['contact_name'].strip()} ({new_contact['emailAddress']}) as a new contact" if new_contact else "")
    summary = (f"New opportunity “{title}” for {company} · {directory.stage_label(body['stage'])} "
               f"({probability}%) · closes {close.strftime('%b %d, %Y').replace(' 0', ' ')} · owner "
               f"{directory.resource_name(owner)}" + who)
    summary += "".join(f" · ⚠ {w}" for w in warnings)
    payload: dict[str, Any] = {"body": body}
    if new_contact:
        payload["new_contact"] = new_contact
    return public_action(actions.propose(CREATE_KIND, summary, payload))


def propose_update(directory: Directory, actions: Actions, args: dict[str, Any], today: date | None = None) -> dict[str, Any]:
    today = today or datetime.now(local_zone()).date()
    oid = int(args.get("opportunity_id") or 0)
    rows = directory.at.query("Opportunities", [{"op": "eq", "field": "id", "value": oid}], FIELDS, 1) if oid else []
    if not rows:
        raise ValueError("No such opportunity. Look it up with find_opportunities first.")
    current = rows[0]
    patch: dict[str, Any] = {"id": oid}
    changes = []
    if args.get("stage"):
        key = args["stage"]
        if key not in STAGES or key not in directory.stage_ids():
            raise ValueError(f"stage must be one of {', '.join(STAGES)}")
        _, probability, status = STAGES[key]
        patch.update({"stage": directory.stage_ids()[key], "probability": probability, "status": status})
        changes.append(f"stage {directory.stage_label(current['stage'])} → {directory.stage_label(patch['stage'])} "
                       f"({current.get('probability')}% → {probability}%)")
    if args.get("close_date"):
        close = _close_date(args["close_date"], today)
        patch["projectedCloseDate"] = _at_date(close)
        changes.append(f"close date {_day(current.get('projectedCloseDate')) or 'none'} → "
                       f"{close.strftime('%b %d, %Y').replace(' 0', ' ')}")
    if args.get("title"):
        patch["title"] = args["title"].strip()
        changes.append(f"title → “{patch['title']}”")
    if args.get("amount") is not None:
        patch["amount"] = float(args["amount"])
        changes.append(f"amount {current.get('amount')} → {patch['amount']}")
    if not changes:
        raise ValueError("Nothing to change: give a stage, close_date, title or amount.")
    summary = f"Update “{current['title']}” ({directory.company_name(current['companyID'])}): " + "; ".join(changes)
    return public_action(actions.propose(UPDATE_KIND, summary, {"patch": patch}))


def opportunity_kinds(at: Any) -> list[ActionKind]:
    def create(payload: dict[str, Any], action_id: str) -> dict[str, Any]:
        body = dict(payload["body"])
        result: dict[str, Any] = {}
        if payload.get("new_contact"):
            body["contactID"] = result["contact_id"] = at.create("Contacts", payload["new_contact"])
        oid = at.create("Opportunities", body)
        return {**result, "opportunity_id": oid, "web_link": web_link("opportunity", oid)}

    def update(payload: dict[str, Any], action_id: str) -> dict[str, Any]:
        oid = at.update("Opportunities", payload["patch"])
        return {"opportunity_id": oid, "web_link": web_link("opportunity", oid)}

    return [ActionKind(CREATE_KIND, create), ActionKind(UPDATE_KIND, update)]


# ── chat tools ───────────────────────────────────────────────────────────────

def opportunity_tools(directory: Directory, actions: Actions) -> list[Tool]:
    def pending_note(result: dict[str, Any]) -> dict[str, Any]:
        if result["status"] == "pending":
            result["note"] = "Not saved yet. Dave sees an approval card; tell him it's ready for his approval."
        return result

    stage = {"type": "string", "enum": list(STAGES),
             "description": "identification, business_case, scoping, proposal (SOW presented), final_approval "
                            "(awaiting signature), won, on_hold, lost"}
    return [
        Tool(spec=ToolSpec(
            name="find_company",
            description="Find a client or prospect in Autotask by name, or by someone's email address (matches the domain).",
            input_schema={"type": "object", "properties": {
                "name": {"type": "string"}, "email": {"type": "string"}}}),
            handler=lambda a: {"companies": directory.find_companies(a.get("name", ""), a.get("email", ""))}),
        Tool(spec=ToolSpec(
            name="find_opportunities",
            description=("List Autotask opportunities: Dave's by default, or a company's (mine=false for everyone's). "
                         "Open ones only unless open_only=false; overdue_only for ones past their close date."),
            input_schema={"type": "object", "properties": {
                "company_id": {"type": "integer"}, "about": {"type": "string", "description": "Words in the title"},
                "mine": {"type": "boolean"}, "open_only": {"type": "boolean"}, "overdue_only": {"type": "boolean"}}}),
            handler=lambda a: {"opportunities": find_opportunities(
                directory, a.get("company_id"), a.get("about", ""), a.get("mine", not a.get("company_id")),
                a.get("open_only", True), a.get("overdue_only", False))}),
        Tool(spec=ToolSpec(
            name="create_opportunity",
            description=(
                "Propose a new Autotask opportunity (saved once Dave approves). Owner is Dave unless owner_name is given. "
                "Write the description in TAG's layout, from what the emails or meeting actually say:\n"
                "<one-sentence summary>\\n\\nCustomer:\\n<company>\\n\\nPrimary Contact:\\n<name>\\n<email>\\n\\n"
                "Scope:\\n- <item>\\n- <item>\\n\\nSales Contact:\\nDave Vener"),
            input_schema={"type": "object", "properties": {
                "company_id": {"type": "integer", "description": "From find_company"},
                "title": {"type": "string", "description": "Short, like 'Coles Collision - MSP Core'"},
                "stage": stage,
                "close_date": {"type": "string", "description": "Projected close, YYYY-MM-DD (default 30 days out)"},
                "contact_email": {"type": "string"}, "contact_name": {"type": "string", "description": "Full name"},
                "contact_title": {"type": "string", "description": "Their job title, if known"},
                "description": {"type": "string"},
                "amount": {"type": "number", "description": "Only if known; normally 0 until quoted"},
                "owner_name": {"type": "string"}},
                "required": ["company_id", "title"]}),
            handler=lambda a: pending_note(propose_create(directory, actions, a))),
        Tool(spec=ToolSpec(
            name="update_opportunity",
            description="Propose changing an opportunity's stage, close date, title or amount (saved once Dave approves).",
            input_schema={"type": "object", "properties": {
                "opportunity_id": {"type": "integer", "description": "From find_opportunities"},
                "stage": stage, "close_date": {"type": "string", "description": "YYYY-MM-DD"},
                "title": {"type": "string"}, "amount": {"type": "number"}},
                "required": ["opportunity_id"]}),
            handler=lambda a: pending_note(propose_update(directory, actions, a))),
    ]
