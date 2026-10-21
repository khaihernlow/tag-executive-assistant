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

    def category_id(self, word: str) -> int:
        """TAG's opportunity category whose label contains `word` ('renewal' -> 'Renewals')."""
        info = self.at._request_json("GET", self.at._api_url("Opportunities/entityInformation/fields"))
        field = next((f for f in info["fields"] if f["name"] == "opportunityCategoryID"), {})
        for v in field.get("picklistValues") or []:
            if word.lower() in v["label"].lower() and v.get("isActive", True):
                return int(v["value"])
        raise ValueError(f"Autotask has no opportunity category like '{word}'.")

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

    def prefetch_companies(self, ids: list[int]) -> None:
        missing = sorted({i for i in ids if i and i not in self._companies})
        for start in range(0, len(missing), 200):
            for r in self.at.query("Companies", [{"op": "in", "field": "id", "value": missing[start:start + 200]}],
                                   ["id", "companyName"]):
                self._companies[r["id"]] = r["companyName"]

    def resource_name(self, resource_id: int) -> str:
        if not self._resources:
            for r in self.at.query("Resources", [{"op": "exist", "field": "id"}], ["id", "firstName", "lastName"]):
                self._resources[r["id"]] = f"{r['firstName']} {r['lastName']}".strip()
        return self._resources.get(resource_id, str(resource_id))

    def owner_choices(self) -> list[list[str]]:
        """[[id, name]] of everyone who owns an open opportunity, Dave first."""
        if not hasattr(self, "_owners"):
            ids = {o["ownerResourceID"] for o in self.at.query(
                "Opportunities", [{"op": "in", "field": "status", "value": list(OPEN_STATUSES)}], ["id", "ownerResourceID"])}
            ids.add(dave_resource_id())
            names = sorted((self.resource_name(i), i) for i in ids if i)
            dave = dave_resource_id()
            self._owners = [[str(dave), self.resource_name(dave)]] + [[str(i), n] for n, i in names if i != dave]
        return self._owners

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
    code = {"opportunity": "OpenOpportunity&OpportunityID", "company": "OpenAccount&AccountID",
            "ticket": "OpenTicketDetail&TicketID"}[record]
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
        "close_iso": close,
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
    directory.prefetch_companies([o.get("companyID") for o in rows])
    return [describe_opportunity(directory, o, today) for o in rows]


def with_highlights(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The list plus the facts a summary needs, counted and sorted in code."""
    label = lambda o: f"{o['title']} ({o['company']}, {o['stage']}, closes {o['close_date']})"  # noqa: E731
    overdue = [o for o in rows if o["overdue"]]
    return {
        "highlights": {
            "count": len(rows), "past_close_date": len(overdue),
            "oldest_past_close": [label(o) for o in overdue[:3]],
            "biggest_amount": [label(o) + f", ${o['amount']:,.0f}" for o in
                               sorted(rows, key=lambda o: -(o.get("amount") or 0))[:3] if o.get("amount")],
        },
        "opportunities": rows,
    }


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


def ticket_section(history: Any, llm: Any, args: dict[str, Any], company: str, contact_name: str) -> tuple[dict, dict]:
    """The quote request ticket for the slip: best-guess codes (from TAG's past quote tickets), each
    a dropdown Dave can change, with how often it's used and why it was picked."""
    from agent.quote_tickets import suggest, usage_note
    s = suggest(llm, history, {"title": args.get("title"), "company": company, "contact": contact_name,
                               "notes": args.get("quote_notes") or "", "description": args.get("description") or "",
                               "tag_people": args.get("tag_people") or ""})
    options = history.options()
    values = {"ticket": "yes", "ticket_title": s["title"], "ticket_description": s["description"],
              "ticket_category": s["category"], "ticket_work_type": s["work_type"], "ticket_issue": s["issue"],
              "ticket_priority": s["priority"], "ticket_due": s["due"], "ticket_fixed": history.fixed()}
    rows = [["Create it", "", "", "ticket"],
            ["Category", history.label("ticketCategory", s["category"]),
             usage_note(history, "category", "ticketCategory", s["category"]), "ticket_category"],
            ["Work type", history.label("billingCodeID", s["work_type"]),
             usage_note(history, "work_type", "billingCodeID", s["work_type"]), "ticket_work_type"],
            ["Issue type", history.label("issue", s["issue"]), usage_note(history, "issue", "", s["issue"]), "ticket_issue"],
            ["Priority", history.label("priority", s["priority"]), "", "ticket_priority"],
            ["Due", s["due"], "", "ticket_due"],
            ["Queue", history.label("queueID", values["ticket_fixed"].get("queueID"))]]
    from agent.quote_tickets import usage

    def with_usage(field: str, source_field: str) -> dict[str, Any]:
        # Every option's count goes to the slip, so the note follows whatever Dave picks
        counts, total = usage(history, field, source_field)
        return {"options": options[field], "usage": {v: counts.get(v, 0) for v, _ in options[field]}, "usage_total": total}

    section = {"heading": "Quote request ticket", "rows": rows, "why": s["why"],
               "edit": {"ticket": {"options": [["yes", "Yes, send to pre-sales"], ["no", "No, just the opportunity"]]},
                        "ticket_category": with_usage("category", "ticketCategory"),
                        "ticket_work_type": with_usage("work_type", "billingCodeID"),
                        "ticket_issue": with_usage("issue", ""),
                        # The due date follows the priority (how long pre-sales usually gets at each one)
                        "ticket_priority": {"options": options["priority"],
                                            "due_days": {v: history.due_days(v) for v, _ in options["priority"]}},
                        "ticket_due": {"type": "date"}}}
    return values, section


def propose_create(directory: Directory, actions: Actions, args: dict[str, Any], today: date | None = None,
                   llm: Any = None, history: Any = None) -> dict[str, Any]:
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
        "amount": float(args.get("amount") or 0), "cost": 0.0, "useQuoteTotals": True,
        "opportunityCategoryID": directory.category_id(args["category"]) if args.get("category") else 1,
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
    contact_row = (contact["name"] if contact else
                   f"{args['contact_name'].strip()} \u00b7 {new_contact['emailAddress']}" if new_contact else "None")
    # A row's 4th item names the field Dave can change on the slip.
    rows = [["Company", company], ["Stage", f"{directory.stage_label(body['stage'])} \u00b7 {probability}%", "", "stage"],
            ["Closes", close.strftime("%b %d, %Y").replace(" 0", " "), "", "close_date"],
            ["Owner", directory.resource_name(owner), "", "owner_id"],
            ["Contact", contact_row, "New contact" if new_contact else ""]]
    if args.get("category"):
        rows.insert(1, ["Category", args["category"].title() + "s"])
    stages = directory.stage_ids()
    edit_fields = {
        "stage": {"options": [[k, f"{directory.stage_label(stages[k])} \u00b7 {STAGES[k][1]}%"]
                              for k in STAGES if k in stages and k not in ("won", "lost")]},
        "close_date": {"type": "date"},
        "owner_id": {"options": directory.owner_choices()},
    }
    payload: dict[str, Any] = {
        "body": body, "title": title, "description": body["description"],
        "stage": stage_key, "close_date": close.isoformat(), "owner_id": str(owner),
        # What each stage means in Autotask terms, so an edited stage saves correctly.
        "stage_map": {k: [stages[k], STAGES[k][1], STAGES[k][2]] for k in STAGES if k in stages},
        "display": {"rows": rows, "edit": edit_fields, "warnings": warnings, "details_label": "Description"},
    }
    if new_contact:
        payload["new_contact"] = new_contact
    if llm is not None and history is not None and args.get("quote_ticket", True):
        try:
            values, section = ticket_section(history, llm, args, company, contact["name"] if contact else
                                             (args.get("contact_name") or ""))
        except Exception as e:  # noqa: BLE001 - the opportunity still stands; say the ticket couldn't be prepared
            warnings.append(f"couldn't prepare the quote ticket ({type(e).__name__}); create it in Autotask")
        else:
            payload.update(values)
            payload["display"]["ticket"] = section
            summary += f" · then a quote request ticket ({section['rows'][1][1]}, {section['rows'][2][1]})"
    return public_action(actions.propose(CREATE_KIND, summary, payload))


def propose_update(directory: Directory, actions: Actions, args: dict[str, Any], today: date | None = None) -> dict[str, Any]:
    today = today or datetime.now(local_zone()).date()
    oid = int(args.get("opportunity_id") or 0)
    rows = directory.at.query("Opportunities", [{"op": "eq", "field": "id", "value": oid}], FIELDS, 1) if oid else []
    if not rows:
        raise ValueError("No such opportunity. Look it up with find_opportunities first.")
    current = rows[0]
    patch: dict[str, Any] = {"id": oid}
    rows: list[list[str]] = []
    if args.get("stage"):
        key = args["stage"]
        if key not in STAGES or key not in directory.stage_ids():
            raise ValueError(f"stage must be one of {', '.join(STAGES)}")
        _, probability, status = STAGES[key]
        patch.update({"stage": directory.stage_ids()[key], "probability": probability, "status": status})
        rows.append(["Stage", f"{directory.stage_label(current['stage'])} \u2192 {directory.stage_label(patch['stage'])}",
                     f"{current.get('probability')}% \u2192 {probability}%"])
    if args.get("close_date"):
        close = _close_date(args["close_date"], today)
        patch["projectedCloseDate"] = _at_date(close)
        rows.append(["Closes", f"{_day(current.get('projectedCloseDate')) or 'none'} \u2192 "
                               f"{close.strftime('%b %d, %Y').replace(' 0', ' ')}"])
    if args.get("title"):
        patch["title"] = args["title"].strip()
        rows.append(["Title", f"\u2192 \u201c{patch['title']}\u201d"])
    if args.get("amount") is not None:
        patch["amount"] = float(args["amount"])
        rows.append(["Amount", f"{current.get('amount')} \u2192 {patch['amount']}"])
    if not rows:
        raise ValueError("Nothing to change: give a stage, close_date, title or amount.")
    company = directory.company_name(current["companyID"])
    summary = f"Update \u201c{current['title']}\u201d ({company}): " + "; ".join(
        f"{r[0].lower()} {r[1]}" + (f" ({r[2]})" if len(r) > 2 else "") for r in rows)
    display = {"heading": current["title"], "rows": [["Company", company], *rows], "warnings": []}
    return public_action(actions.propose(UPDATE_KIND, summary, {"patch": patch, "display": display}))


def apply_slip_edits(payload: dict[str, Any]) -> dict[str, Any]:
    """The record to save, with whatever Dave changed on the slip (title, description,
    stage, close date, owner). Only values the slip offered are accepted."""
    body = {**payload["body"], "title": (payload.get("title") or payload["body"]["title"]).strip(),
            "description": payload.get("description", payload["body"]["description"])}
    stage = (payload.get("stage_map") or {}).get(payload.get("stage"))
    if stage:
        body["stage"], body["probability"], body["status"] = stage
    if payload.get("close_date"):
        try:
            body["projectedCloseDate"] = _at_date(date.fromisoformat(payload["close_date"][:10]))
        except ValueError as e:
            raise ValueError("The close date isn't a valid date.") from e
    owners = {o[0] for o in ((payload.get("display") or {}).get("edit") or {}).get("owner_id", {}).get("options", [])}
    if payload.get("owner_id") in owners:
        body["ownerResourceID"] = int(payload["owner_id"])
    return body


def opportunity_kinds(at: Any) -> list[ActionKind]:
    def create(payload: dict[str, Any], action_id: str) -> dict[str, Any]:
        body = apply_slip_edits(payload)
        result: dict[str, Any] = {}
        if payload.get("new_contact"):
            body["contactID"] = result["contact_id"] = at.create("Contacts", payload["new_contact"])
        oid = at.create("Opportunities", body)
        result.update({"opportunity_id": oid, "web_link": web_link("opportunity", oid)})
        if payload.get("ticket") == "yes" and payload.get("ticket_fixed"):
            from agent.quote_tickets import ticket_body
            try:
                tid = at.create("Tickets", ticket_body(payload, oid, body["companyID"], body.get("contactID")))
                result.update({"ticket_id": tid, "ticket_link": web_link("ticket", tid)})
            except Exception as e:  # noqa: BLE001 - the opportunity is saved; say the ticket wasn't
                result["ticket_error"] = f"{type(e).__name__}: {e}"[:300]
        return result

    def update(payload: dict[str, Any], action_id: str) -> dict[str, Any]:
        oid = at.update("Opportunities", payload["patch"])
        return {"opportunity_id": oid, "web_link": web_link("opportunity", oid)}

    ticket_fields = ("ticket", "ticket_title", "ticket_description", "ticket_category", "ticket_work_type",
                     "ticket_issue", "ticket_priority", "ticket_due")
    return [ActionKind(CREATE_KIND, create, editable=("title", "description", "stage", "close_date", "owner_id", *ticket_fields)),
            ActionKind(UPDATE_KIND, update)]


# ── chat tools ───────────────────────────────────────────────────────────────

def opportunity_tools(directory: Directory, actions: Actions, llm: Any = None) -> list[Tool]:
    from agent.quote_tickets import QuoteHistory
    history = QuoteHistory(directory.at) if llm is not None else None
    if history is not None:
        # A year of quote tickets takes ~12s to read: do it now, not while Dave waits for his card
        import threading

        def warm() -> None:
            try:
                history.load()
            except Exception:  # noqa: BLE001 - it loads again on first use
                pass
        threading.Thread(target=warm, daemon=True).start()

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
            handler=lambda a: with_highlights(find_opportunities(
                directory, a.get("company_id"), a.get("about", ""), a.get("mine", not a.get("company_id")),
                a.get("open_only", True), a.get("overdue_only", False)))),
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
                "owner_name": {"type": "string"},
                "quote_ticket": {"type": "boolean", "description": "Also ask pre-sales to quote it (a ticket linked to the "
                                 "opportunity). Default true; false only if Dave says not to, or it's already quoted"},
                "quote_notes": {"type": "string", "description": "What to quote, in Dave's words: what, how many, budget, "
                                "what they pay now if it's a renewal, whether he wants ballpark numbers or the client "
                                "already approved, any urgency"},
                "tag_people": {"type": "string", "description": "TAG people involved besides Dave, if any ('RJ Gray / Sales')"}},
                "required": ["company_id", "title"]}),
            handler=lambda a: pending_note(propose_create(directory, actions, a, llm=llm, history=history))),
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
