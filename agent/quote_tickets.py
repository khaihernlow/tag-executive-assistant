"""The quote request ticket that goes with a new opportunity.

At TAG an opportunity is two steps (Dave walked through it, Oct 2026): describe the deal (the
opportunity), then ask pre-sales to quote it, as a ticket linked to the opportunity in the
Pre-Sales / Design Desk queue. The ticket has to be classified right (category, work type,
issue type, priority); Dave: "it only did that because I knew to pick the right code."

How the classification is chosen:
- Code reads the last year of quote tickets: which values are actually in use for each field,
  and which past tickets look most like this request (word overlap on title and description).
- The model picks from those values only, with the similar tickets in front of it, and says why.
- Code checks every pick is a real option (else falls back to what the similar tickets used),
  and works out the due date from how long pre-sales is usually given at that priority.
- The approval card shows each pick as a dropdown with its reason; Dave can change any of them.
"""

from __future__ import annotations

import re
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from statistics import median
from typing import Any

from llm.provider import ToolSpec

HISTORY_DAYS = 365
CACHE_SECONDS = 12 * 3600
MAX_EXAMPLES = 8
TEMPLATE = ("PROJECT STAKEHOLDERS (CLIENT AND TAG RESOURCES):\n- Client: <name> / <title>\n- TAG: <name> / <role>\n\n"
            "PROJECT OBJECTIVE:\n- <what the client needs and why>\n\nPROJECT GOALS:\n- <the outcome>\n\n"
            "PROJECT BUDGET:\n- <amount or Unknown>\n\n"
            "IF RENEWAL OF MONTHLY SERVICE, AMOUNT CLIENT IS CURRENTLY PAYING FOR THIS SERVICE:\n- <amount or N/A>")
FIELDS = ["id", "title", "description", "queueID", "issueType", "subIssueType", "ticketCategory", "billingCodeID",
          "priority", "ticketType", "source", "createDate", "dueDateTime", "opportunityID"]
_WORD = re.compile(r"[a-z0-9][a-z0-9+-]{2,}")
_STOP = {"the", "and", "for", "with", "quote", "project", "client", "tag", "resources", "stakeholders", "objective",
         "goals", "budget", "renewal", "monthly", "service", "amount", "currently", "paying", "this", "unknown", "n/a",
         "please", "prepare", "need", "needs", "new", "from", "that", "will", "are", "vcio", "approved", "budgetary"}


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall((text or "").lower()) if w not in _STOP}


class QuoteHistory:
    """TAG's recent quote tickets and the picklists behind them, cached for half a day."""

    def __init__(self, at: Any):
        self.at = at
        self._loaded = 0.0
        self.tickets: list[dict[str, Any]] = []
        self.labels: dict[str, dict[str, str]] = {}
        self.parents: dict[str, str] = {}        # sub-issue type -> its issue type
        self.work_types: dict[str, str] = {}

    def load(self) -> "QuoteHistory":
        if time.time() - self._loaded < CACHE_SECONDS and self.tickets:
            return self
        fields = self.at._request_json("GET", self.at._api_url("Tickets/entityInformation/fields"))["fields"]
        for f in fields:
            if f.get("isPickList"):
                self.labels[f["name"]] = {str(v["value"]): v["label"] for v in f.get("picklistValues") or []
                                          if v.get("isActive", True)}
                if f["name"] == "subIssueType":
                    self.parents = {str(v["value"]): str(v.get("parentValue")) for v in f.get("picklistValues") or []}
        since = (datetime.now(timezone.utc) - timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%dT00:00:00Z")
        self.tickets = self.at.query("Tickets", [{"op": "exist", "field": "opportunityID"},
                                                 {"op": "gte", "field": "createDate", "value": since}], FIELDS)
        # Work types (billing codes): the ones quote tickets use, plus every "... Pre-Sales" one
        ids = sorted({t["billingCodeID"] for t in self.tickets if t.get("billingCodeID")})
        codes = self.at.query("BillingCodes", [{"op": "in", "field": "id", "value": ids}], ["id", "name", "isActive"]) if ids else []
        codes += self.at.query("BillingCodes", [{"op": "contains", "field": "name", "value": "Pre-Sales"}], ["id", "name", "isActive"])
        self.work_types = {str(c["id"]): c["name"] for c in codes if c.get("isActive", True) and "DO NOT USE" not in c["name"]}
        self._loaded = time.time()
        return self

    def quote_tickets(self, days: int = HISTORY_DAYS) -> list[dict[str, Any]]:
        """Tickets in the pre-sales queue (the quote requests), newest first."""
        presales = self.majority("queueID", self.tickets) or ""
        cut = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        rows = [t for t in self.tickets if str(t.get("queueID")) == presales and (t.get("createDate") or "") >= cut[:19]]
        return sorted(rows, key=lambda t: t.get("createDate") or "", reverse=True)

    @staticmethod
    def majority(field: str, rows: list[dict[str, Any]]) -> str | None:
        counts = Counter(str(t.get(field)) for t in rows if t.get(field) not in (None, ""))
        return counts.most_common(1)[0][0] if counts else None

    def label(self, field: str, value: Any) -> str:
        if value in (None, "", "none"):
            return "None"
        if field == "billingCodeID":
            return self.work_types.get(str(value), str(value))
        if field == "issue":
            issue, _, sub = str(value).partition(":")
            return f"{self.labels.get('issueType', {}).get(issue, issue)} / {self.labels.get('subIssueType', {}).get(sub, sub)}"
        return self.labels.get(field, {}).get(str(value), str(value))

    def options(self) -> dict[str, list[list[str]]]:
        """[[value, label]] for each field Dave can change, most used first. 'Recent' is the last 3 months:
        conventions shift (issue types stopped being set on quote tickets in Sep 2026)."""
        recent = self.quote_tickets(days=90) or self.quote_tickets()
        year = self.quote_tickets()

        def ranked(field: str, extra: list[str] = ()) -> list[str]:
            counts = Counter(str(t.get(field)) for t in recent if t.get(field) not in (None, ""))
            older = Counter(str(t.get(field)) for t in year if t.get(field) not in (None, ""))
            seen = [v for v, _ in counts.most_common()] + [v for v, _ in older.most_common() if v not in counts]
            return seen + [v for v in extra if v not in seen]

        presales_codes = [k for k, v in self.work_types.items() if v.lower().endswith("pre-sales")]
        issues = Counter(f"{t['issueType']}:{t.get('subIssueType') or ''}" for t in year if t.get("issueType"))
        voice = next((k for k, v in self.labels.get("issueType", {}).items() if v == "Voice"), None)
        voice_quote = next((k for k, p in self.parents.items() if p == voice
                            and self.labels.get("subIssueType", {}).get(k) == "Sales Quote"), None) if voice else None
        issue_values = ["none"] + [v for v, _ in issues.most_common()] + ([f"{voice}:{voice_quote}"] if voice_quote else [])
        return {
            "category": [[v, self.label("ticketCategory", v)] for v in ranked("ticketCategory")
                         if "pre-sales" in self.label("ticketCategory", v).lower()] or
                        [[v, self.label("ticketCategory", v)] for v in ranked("ticketCategory")],
            "work_type": [[v, self.label("billingCodeID", v)] for v in ranked("billingCodeID", presales_codes)],
            "issue": [[v, self.label("issue", v)] for v in dict.fromkeys(issue_values)],
            "priority": [[v, label] for v, label in self.labels.get("priority", {}).items()],
        }

    def similar(self, text: str, k: int = MAX_EXAMPLES) -> list[dict[str, Any]]:
        want = _words(text)
        scored = []
        for t in self.quote_tickets():
            have = _words(f"{t.get('title')} {(t.get('description') or '')[:600]}")
            overlap = len(want & have)
            if overlap:
                scored.append((overlap / (len(want | have) ** 0.5), t))
        scored.sort(key=lambda x: -x[0])
        return [t for _, t in scored[:k]]

    def due_days(self, priority: str) -> int:
        """How long pre-sales is usually given at this priority (median days, recent quote tickets)."""
        gaps = []
        for t in self.quote_tickets(days=180):
            if str(t.get("priority")) == str(priority) and t.get("dueDateTime") and t.get("createDate"):
                try:
                    gap = (datetime.fromisoformat(t["dueDateTime"][:19]) - datetime.fromisoformat(t["createDate"][:19])).days
                except ValueError:
                    continue
                if 0 <= gap <= 60:
                    gaps.append(gap)
        return int(median(gaps)) if gaps else 7

    def fixed(self) -> dict[str, Any]:
        """What every quote ticket has in common (queue, type, source): the recent majority, not guessed."""
        recent = self.quote_tickets(days=90) or self.quote_tickets()
        statuses = {v: k for k, v in self.labels.get("status", {}).items()}
        return {k: int(v) for k, v in (("queueID", self.majority("queueID", self.tickets)),
                                        ("ticketType", self.majority("ticketType", recent)),
                                        ("source", self.majority("source", recent)),
                                        ("status", statuses.get("New"))) if v}


PICK = ToolSpec(
    name="pick_quote_ticket",
    description="Fill in the quote request ticket for this opportunity, choosing each code from the options given.",
    input_schema={"type": "object", "properties": {
        "title": {"type": "string", "description": "'Quote - <what>'. Start with 'Budgetary Quote - ' if Dave wants ballpark "
                                                   "numbers, or 'Quote - [APPROVED] – ' if the client already approved it"},
        "category": {"type": "string", "description": "One of the category option values"},
        "work_type": {"type": "string", "description": "One of the work type option values"},
        "issue": {"type": "string", "description": "One of the issue option values ('none' unless the similar tickets use one)"},
        "priority": {"type": "string", "description": "One of the priority option values"},
        "description": {"type": "string", "description": "The ticket text in TAG's template, from what Dave said only"},
        "why": {"type": "string", "description": "One short sentence: why this category and work type, e.g. "
                                                 "'A phone system, so the Voice pre-sales work type; a new quote, not a renewal'"},
    }, "required": ["title", "category", "work_type", "issue", "priority", "description", "why"]},
)

PICK_PROMPT = """You fill in the ticket that asks TAG's pre-sales team to quote a new opportunity. TAG is an IT managed
services provider (IT and voice/phone systems). Choose each code ONLY from the options listed, by value. Go by how
the most similar past tickets were classified, adjusted for what this request is: a phone system or phones is
voice work; a renewal of something the client already pays for is a renewal, otherwise a new quote. Priority:
match similar tickets unless Dave says it's urgent. Write the description in the template, using only what's in
the request; write "Unknown" or "N/A" where it says nothing. No em dashes."""


def suggest(llm: Any, history: QuoteHistory, request: dict[str, Any]) -> dict[str, Any]:
    """The ticket for this opportunity: the model's picks, checked against the real options."""
    history.load()
    options = history.options()
    text = f"{request.get('title', '')} {request.get('notes', '')} {request.get('description', '')}"
    examples = history.similar(text)
    shown = [{"title": t.get("title"), "category": history.label("ticketCategory", t.get("ticketCategory")),
              "work_type": history.label("billingCodeID", t.get("billingCodeID")),
              "issue": history.label("issue", f"{t['issueType']}:{t.get('subIssueType') or ''}") if t.get("issueType") else "None",
              "priority": history.label("priority", t.get("priority")), "created": (t.get("createDate") or "")[:10]}
             for t in examples]
    material = (f"Opportunity: {request.get('title')} for {request.get('company')}\n"
                f"Client contact: {request.get('contact') or 'unknown'}\n"
                f"TAG people on it: {request.get('tag_people') or 'Dave Vener / President'}\n"
                f"What Dave said: {request.get('notes') or request.get('description') or ''}\n"
                f"Opportunity description: {request.get('description') or ''}\n\n"
                f"Options (value: label):\n" + "\n".join(
                    f"{field}: " + "; ".join(f"{v}: {label}" for v, label in opts) for field, opts in options.items()) +
                f"\n\nMost similar past quote tickets:\n" + "\n".join(map(str, shown)) +
                f"\n\nTemplate:\n{TEMPLATE}")
    response = llm.complete([{"role": "user", "content": material}], system=PICK_PROMPT, tools=[PICK],
                            tool_choice=PICK.name, max_tokens=1500)
    picked = response.tool_calls[0].input if response.tool_calls else {}

    def check(field: str, source_field: str) -> str:
        allowed = [v for v, _ in options[field]]
        value = str(picked.get(field) or "")
        if value in allowed:
            return value
        by_label = next((v for v, label in options[field] if label.lower() == value.lower()), None)
        if by_label:
            return by_label
        # Not a real option: what the similar tickets used, else the most common
        def key(t: dict[str, Any]) -> str:
            if field == "issue":
                return f"{t['issueType']}:{t.get('subIssueType') or ''}" if t.get("issueType") else "none"
            return str(t.get(source_field))
        votes = Counter(key(t) for t in examples if key(t) in allowed)
        return votes.most_common(1)[0][0] if votes else allowed[0]

    # A field the team has stopped setting stays empty: no issue type on 90%+ of recent quote tickets
    recent = history.quote_tickets(days=90)
    if recent and sum(1 for t in recent if not t.get("issueType")) >= 0.9 * len(recent):
        picked["issue"] = "none"
    out = {
        "category": check("category", "ticketCategory"),
        "work_type": check("work_type", "billingCodeID"),
        "issue": check("issue", "issue"),
        "priority": check("priority", "priority"),
        "why": re.sub(r"\s*[—–]\s*", ", ", (picked.get("why") or "").strip()),
        "similar": len(examples),
    }
    title = re.sub(r"\s*[—]\s*", " – ", (picked.get("title") or "").strip()) or f"Quote - {request.get('title')}"
    if not re.match(r"^(Budgetary )?Quote\b", title, re.I):
        title = f"Quote - {title}"
    out["title"] = title[:250]
    description = re.sub(r"\s*—\s*", ", ", (picked.get("description") or "").strip())
    out["description"] = description or TEMPLATE
    out["due"] = (date.today() + timedelta(days=history.due_days(out["priority"]))).isoformat()
    return out


def usage_note(history: QuoteHistory, field: str, source_field: str, value: str) -> str:
    """'used on 89 of 101 recent quote tickets': how common a pick is, counted in code."""
    recent = history.quote_tickets(days=90) or history.quote_tickets()
    if field == "issue":
        n = sum(1 for t in recent if (f"{t['issueType']}:{t.get('subIssueType') or ''}" if t.get("issueType") else "none") == value)
    else:
        n = sum(1 for t in recent if str(t.get(source_field)) == value)
    return f"{n} of {len(recent)} recent quote tickets"


def ticket_body(payload: dict[str, Any], opportunity_id: int, company_id: int, contact_id: int | None) -> dict[str, Any]:
    """The Autotask ticket from the slip's (possibly edited) values."""
    body: dict[str, Any] = {
        **payload["ticket_fixed"],
        "companyID": company_id, "opportunityID": opportunity_id,
        "title": payload["ticket_title"].strip()[:255],
        "description": payload["ticket_description"].strip()[:8000],
        "ticketCategory": int(payload["ticket_category"]),
        "billingCodeID": int(payload["ticket_work_type"]),
        "priority": int(payload["ticket_priority"]),
        "dueDateTime": f"{payload['ticket_due'][:10]}T17:00:00Z",
    }
    if contact_id:
        body["contactID"] = contact_id
    issue, _, sub = (payload.get("ticket_issue") or "none").partition(":")
    if issue and issue != "none":
        body["issueType"] = int(issue)
        if sub:
            body["subIssueType"] = int(sub)
    return body
