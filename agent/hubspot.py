"""Asking HubSpot questions in plain English.

The model turns Dave's question into a structured query: which records, which fields, what values,
in his words ("owner is RJ", "last contacted more than 6 months ago", "lifecycle is customer").
Code does everything else: finds the HubSpot field behind a word, turns names into owner ids and
labels into stage or option values, works out dates from phrases (agent.periods), runs the search,
and does every count, sum and grouping. The model then phrases the answer from those numbers.

What's in TAG's HubSpot (checked Oct 2026): 22,700 contacts and 22,000 companies, many enriched by
ZoomInfo; 155,800 calls, 58,000 tasks, 5,000 meetings (Dave's calendar syncs in), 600 notes.
Deals are NOT a working pipeline: 1,371 of 1,389 were created by Zapier from Autotask quotes and
never moved past "Appointment Scheduled"; only 18 have a close date. Pipeline questions (stages,
close dates, what's closing) belong to Autotask opportunities.
"""

from __future__ import annotations

import html
import os
import re
from collections import defaultdict
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any

from agent.calendar import local_zone
from agent.people import name_score
from agent.periods import period
from agent.tools import Tool
from llm.provider import ToolSpec

OBJECT_WORDS = {
    "companies": ("company", "companies", "account", "accounts", "organization", "organizations", "business", "businesses", "client", "clients", "prospect", "prospects"),
    "contacts": ("contact", "contacts", "person", "people", "lead", "leads"),
    "deals": ("deal", "deals", "opportunity", "opportunities", "quote", "quotes"),
    "calls": ("call", "calls", "phone call", "phone calls"),
    "meetings": ("meeting", "meetings"),
    "notes": ("note", "notes"),
    "tasks": ("task", "tasks", "to-do", "todo", "todos", "to-dos"),
    "emails": ("email", "emails", "logged email", "logged emails", "e-mail", "e-mails"),
}

# What each kind of record shows, and the words Dave uses for its fields
SHAPES: dict[str, dict[str, Any]] = {
    "companies": {
        "title": ["name"], "sub": ["domain", "city", "state"],
        "show": ["lifecyclestage", "hubspot_owner_id", "industry", "numberofemployees", "notes_last_contacted"],
        "fields": {"name": "name", "domain": "domain", "website": "domain", "owner": "hubspot_owner_id",
                   "lifecycle": "lifecyclestage", "lifecycle stage": "lifecyclestage", "stage": "lifecyclestage",
                   "type": "type", "industry": "industry", "employees": "numberofemployees", "size": "numberofemployees",
                   "revenue": "annualrevenue", "city": "city", "state": "state", "zip": "zip",
                   "last contacted": "notes_last_contacted", "last activity": "notes_last_updated",
                   "last meeting": "most_recent___meeting_start_time", "next activity": "notes_next_activity_date",
                   "created": "createdate", "modified": "hs_lastmodifieddate", "deals": "num_associated_deals",
                   "contacts": "num_associated_contacts", "lead status": "hs_lead_status",
                   "distance": "distance_from_albany_ny", "distance from albany": "distance_from_albany_ny"},
        "modified": "hs_lastmodifieddate"},
    "contacts": {
        "title": ["firstname", "lastname"], "sub": ["jobtitle", "company"],
        "show": ["email", "lifecyclestage", "hubspot_owner_id", "notes_last_contacted"],
        "fields": {"first name": "firstname", "last name": "lastname", "email": "email", "title": "jobtitle",
                   "job title": "jobtitle", "role": "jobtitle", "company": "company", "company name": "company",
                   "owner": "hubspot_owner_id", "lifecycle": "lifecyclestage", "lifecycle stage": "lifecyclestage",
                   "lead status": "hs_lead_status", "phone": "phone", "city": "city", "state": "state",
                   "last contacted": "notes_last_contacted", "last activity": "notes_last_updated",
                   "created": "createdate", "modified": "lastmodifieddate", "current client": "current_client",
                   # TAG's 2026 combined scoring first; older scores (Adjusted, Jun 2025, HubSpot's) only by name
                   "score": "lead_scoring_combined_2026", "lead score": "lead_scoring_combined_2026",
                   "combined score": "lead_scoring_combined_2026", "tier": "lead_scoring_combined_2026_threshold",
                   "lead tier": "lead_scoring_combined_2026_threshold", "score tier": "lead_scoring_combined_2026_threshold",
                   "engagement score": "lead_scoring_combined_2026_engagement", "engagement": "lead_scoring_combined_2026_engagement",
                   "fit score": "lead_scoring_combined_2026_fit", "fit": "lead_scoring_combined_2026_fit",
                   "adjusted lead score": "adjusted_lead_score", "hubspot score": "hubspotscore",
                   "likelihood to close": "hs_predictivecontactscore", "predictive score": "hs_predictivecontactscore",
                   # What they've done: marketing emails, the website
                   "email clicks": "hs_email_click", "emails clicked": "hs_email_click", "clicks": "hs_email_click",
                   "email opens": "hs_email_open", "emails opened": "hs_email_open", "opens": "hs_email_open",
                   "last email click": "hs_email_last_click_date", "last clicked": "hs_email_last_click_date",
                   "page views": "hs_analytics_num_page_views", "pageviews": "hs_analytics_num_page_views",
                   "website visits": "hs_analytics_num_visits", "visits": "hs_analytics_num_visits",
                   "sessions": "hs_analytics_num_visits", "last website visit": "hs_analytics_last_visit_timestamp",
                   "last visit": "hs_analytics_last_visit_timestamp", "last seen": "hs_analytics_last_timestamp"},
        "modified": "lastmodifieddate"},
    "deals": {
        "title": ["dealname"], "sub": [],
        "show": ["amount", "dealstage", "hubspot_owner_id", "createdate"],
        "fields": {"name": "dealname", "title": "dealname", "amount": "amount", "value": "amount", "stage": "dealstage",
                   "owner": "hubspot_owner_id", "close date": "closedate", "closes": "closedate", "created": "createdate",
                   "modified": "hs_lastmodifieddate", "pipeline": "pipeline", "closed": "hs_is_closed", "won": "hs_is_closed_won",
                   "last activity": "notes_last_updated", "last contacted": "notes_last_contacted"},
        "modified": "hs_lastmodifieddate"},
    "calls": {
        "title": ["hs_call_title"], "sub": ["hs_call_direction", "hs_call_disposition"],
        "show": ["hubspot_owner_id", "hs_timestamp", "hs_call_duration"],
        "fields": {"title": "hs_call_title", "owner": "hubspot_owner_id", "made by": "hubspot_owner_id", "when": "hs_timestamp",
                   "date": "hs_timestamp", "direction": "hs_call_direction", "outcome": "hs_call_disposition",
                   "duration": "hs_call_duration", "status": "hs_call_status", "notes": "hs_call_body",
                   "created": "hs_createdate", "logged": "hs_createdate"},
        "modified": "hs_lastmodifieddate", "when": "hs_timestamp"},
    "meetings": {
        "title": ["hs_meeting_title"], "sub": ["hs_meeting_outcome"],
        "show": ["hubspot_owner_id", "hs_timestamp"],
        # "when" is the meeting itself; "booked" is when it was set up (an appointment got "today" can be next week)
        "fields": {"title": "hs_meeting_title", "owner": "hubspot_owner_id", "when": "hs_timestamp", "date": "hs_timestamp",
                   "meeting date": "hs_timestamp", "held": "hs_timestamp", "outcome": "hs_meeting_outcome",
                   "notes": "hs_meeting_body", "created": "hs_createdate", "booked": "hs_createdate",
                   "booked on": "hs_createdate", "set": "hs_createdate", "scheduled on": "hs_createdate"},
        "modified": "hs_lastmodifieddate", "when": "hs_timestamp"},
    "notes": {
        "title": ["hs_note_body"], "sub": [],
        "show": ["hubspot_owner_id", "hs_timestamp"],
        "fields": {"text": "hs_note_body", "body": "hs_note_body", "owner": "hubspot_owner_id", "written by": "hubspot_owner_id",
                   "when": "hs_timestamp", "date": "hs_timestamp", "created": "hs_createdate"},
        "modified": "hs_lastmodifieddate", "when": "hs_timestamp"},
    "tasks": {
        "title": ["hs_task_subject"], "sub": ["hs_task_status", "hs_task_priority"],
        "show": ["hubspot_owner_id", "hs_timestamp"],
        "fields": {"subject": "hs_task_subject", "title": "hs_task_subject", "owner": "hubspot_owner_id",
                   "assigned to": "hubspot_owner_id", "due": "hs_timestamp", "due date": "hs_timestamp",
                   "status": "hs_task_status", "priority": "hs_task_priority", "type": "hs_task_type",
                   "created": "hs_createdate"},
        "modified": "hs_lastmodifieddate", "when": "hs_timestamp"},
    "emails": {
        "title": ["hs_email_subject"], "sub": ["hs_email_from_email", "hs_email_to_email"],
        "show": ["hubspot_owner_id", "hs_timestamp"],
        "fields": {"subject": "hs_email_subject", "title": "hs_email_subject", "owner": "hubspot_owner_id",
                   "sent by": "hubspot_owner_id", "when": "hs_timestamp", "date": "hs_timestamp", "from": "hs_email_from_email",
                   "to": "hs_email_to_email", "direction": "hs_email_direction", "text": "hs_email_text", "body": "hs_email_text",
                   "created": "hs_createdate"},
        "modified": "hs_lastmodifieddate", "when": "hs_timestamp"},
}

OPS = ("is", "is_not", "any_of", "none_of", "contains", "not_contains", "more_than", "less_than", "at_least",
       "at_most", "between", "during", "before", "after", "has_value", "is_empty")
FUTURE_FIELDS = {"closedate", "notes_next_activity_date"}     # bare months/dates mean the coming ones
STATE_FIELDS = {"state", "company_state"}
# HubSpot holds states both ways ("NY" from forms, "New York" from ZoomInfo): a state matches either
US_STATES = {"AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
             "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
             "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
             "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
             "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
             "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
             "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
             "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
             "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
             "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming"}


def state_forms(value: str) -> list[str]:
    v = str(value).strip()
    if v.upper() in US_STATES:
        return [v.upper(), US_STATES[v.upper()]]
    for abbr, name in US_STATES.items():
        if v.lower() == name.lower():
            return [abbr, name]
    return [v]
MAX_AGGREGATE_ROWS = 3000


def object_type(word: str) -> str:
    w = (word or "").strip().lower()
    for obj, words in OBJECT_WORDS.items():
        if w == obj or w in words:
            return obj
    raise ValueError(f"Unknown kind of record '{word}'. Use one of: {', '.join(OBJECT_WORDS)}.")


def _clean_label(text: str) -> str:
    return re.sub(r"[\s:]+$", "", (text or "").strip().lower())


def _plain(text: str, limit: int = 300) -> str:
    """A note or call body (HTML) as one line of text."""
    text = re.sub(r"(?is)<(style|script).*?</\1>", " ", text or "")
    text = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</div>|</h\d>", " · ", text)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"(\s*·\s*)+", " · ", " ".join(text.split())).strip(" ·")
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


def _plural(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def _number(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().lower().replace(",", "").replace("$", "")
    m = re.fullmatch(r"(-?\d+(?:\.\d+)?)\s*(k|m|thousand|million)?", text)
    if not m:
        raise ValueError(f"'{value}' isn't a number.")
    scale = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6}.get(m.group(2) or "", 1)
    return float(m.group(1)) * scale


class HubSpot:
    """The account's fields, owners and stages, and the translation from Dave's words to HubSpot's."""

    def __init__(self, client: Any, graph: Any = None, today: date | None = None):
        self.hs = client
        self.graph = graph       # TAG's directory, for nicknames ("RJ" is Richard Gray)
        self._today = today

    @property
    def today(self) -> date:
        return self._today or datetime.now(local_zone()).date()

    # ── fields ──

    def props(self, obj: str) -> dict[str, dict[str, Any]]:
        return {p["name"]: p for p in self.hs.properties(obj)}

    def field(self, obj: str, word: str) -> dict[str, Any]:
        """Dave's word for a field -> the HubSpot property. Raises with suggestions if unsure."""
        props = self.props(obj)
        w = _clean_label(word)
        name = SHAPES[obj]["fields"].get(w)
        if name and name in props:
            return props[name]
        if word in props:
            return props[word]
        by_label = [p for p in props.values() if _clean_label(p.get("label", "")) == w]
        if len(by_label) == 1:
            return by_label[0]
        if by_label:
            return sorted(by_label, key=lambda p: (not p.get("hubspotDefined"), p["name"]))[0]
        close = self.find_fields(obj, word, limit=6)
        if len(close) == 1:
            return props[close[0]["name"]]
        hint = f" Close matches: {', '.join(c['label'] + ' (' + c['name'] + ')' for c in close)}." if close else ""
        if obj in ("calls", "meetings", "notes", "tasks") and _clean_label(word) in ("company", "contact", "account"):
            hint += f" {obj.title()} don't carry a company field: pass company to look at one company's {obj}."
        raise ValueError(f"No {obj} field called '{word}'.{hint} Use hubspot_fields to look for others.")

    def find_fields(self, obj: str, words: str, limit: int = 15) -> list[dict[str, Any]]:
        terms = [t for t in re.split(r"\W+", (words or "").lower()) if t]
        scored = []
        for p in self.props(obj).values():
            if p.get("hidden"):
                continue
            hay = f"{p.get('label', '')} {p['name']} {p.get('description', '')} {p.get('groupName', '')}".lower()
            hits = sum(1 for t in terms if t in hay)
            if hits:
                in_label = sum(1 for t in terms if t in p.get("label", "").lower())
                scored.append((hits, in_label, p))
        scored.sort(key=lambda x: (-x[0], -x[1], x[2].get("label", "")))
        enough = max(1, (len(terms) + 1) // 2)      # most of the words, not just one stray hit
        return [{"name": p["name"], "label": p.get("label"), "type": p.get("type"),
                 "options": [o["label"] for o in p.get("options") or []][:20] or None,
                 "description": (p.get("description") or "")[:160] or None}
                for hits, _, p in scored if hits >= enough][:limit]

    # ── owners and stages ──

    def owners(self) -> list[dict[str, Any]]:
        return [o for o in self.hs.owners() if o.get("email")]

    def owner_name(self, owner_id: Any) -> str:
        for o in self.owners():
            if str(o["id"]) == str(owner_id):
                name = " ".join(filter(None, [o.get("firstName"), o.get("lastName")])) or o["email"]
                return f"{name} (former)" if o.get("archived") else name
        return ""

    def owner_id(self, who: str) -> str:
        w = (who or "").strip().lower()
        me = os.environ.get("GRAPH_MAILBOX", "").lower()
        if w in ("me", "my", "mine", "i", "dave", "david", "dave vener", "david vener", "myself") or (me and w == me):
            match = [o for o in self.owners() if o["email"].lower() == me]
            if match:
                return str(match[0]["id"])
        if w.isdigit():
            return w
        scored = []
        for o in self.owners():
            name = " ".join(filter(None, [o.get("firstName"), o.get("lastName")]))
            score = name_score(who, name, o["email"])
            if o["email"].lower() == w:
                score = 1.0
            scored.append((score - (0.01 if o.get("archived") else 0), o))   # people still at TAG first
        scored.sort(key=lambda x: -x[0])
        if scored and scored[0][0] >= 0.85 and (len(scored) == 1 or scored[1][0] < scored[0][0]):
            return str(scored[0][1]["id"])
        if self.graph is not None:
            # Nicknames: TAG's directory knows "RJ" is RGray@
            from agent.people import find_people
            try:
                people = [p for p in find_people(self.graph, who, limit=3) if p.internal and p.score >= 0.9]
            except Exception:  # noqa: BLE001
                people = []
            for p in people[:1]:
                for o in self.owners():
                    if o["email"].lower() == p.email.lower():
                        return str(o["id"])
        names = ", ".join(" ".join(filter(None, [o.get("firstName"), o.get("lastName")])) for o in self.owners()
                          if not o.get("archived"))
        raise ValueError(f"No HubSpot user matches '{who}'. HubSpot users now: {names}.")

    def stages(self) -> dict[str, dict[str, Any]]:
        out = {}
        for p in self.hs.pipelines("deals"):
            for s in p["stages"]:
                out[s["id"]] = {"label": s["label"], "pipeline": p["label"]}
        return out

    # ── values ──

    def _option(self, prop: dict[str, Any], value: Any) -> str:
        if prop["name"] == "dealstage":
            want = str(value).strip().lower()
            for sid, s in self.stages().items():
                if want in (sid.lower(), s["label"].lower()):
                    return sid
            raise ValueError(f"No deal stage '{value}'. Stages: {', '.join(sorted({s['label'] for s in self.stages().values()}))}.")
        if prop["name"] == "pipeline":
            for p in self.hs.pipelines("deals"):
                if str(value).strip().lower() in (p["id"].lower(), p["label"].lower()):
                    return p["id"]
        if prop["name"] == "hs_call_disposition":
            outcomes = self.hs.call_outcomes()
            want = str(value).strip().lower()
            exact = [i for i, label in outcomes.items() if want in (i.lower(), label.lower())]
            close = exact or [i for i, label in outcomes.items() if want in label.lower()]
            if len(close) == 1:
                return close[0]
            raise ValueError(f"'{value}' isn't a call outcome. Outcomes: {', '.join(outcomes.values())}.")
        options = prop.get("options") or []
        want = str(value).strip().lower()
        for o in options:
            if want in (str(o["value"]).lower(), o["label"].lower()):
                return o["value"]
        contains = [o for o in options if want in o["label"].lower()]
        if len(contains) == 1:
            return contains[0]["value"]
        raise ValueError(f"'{value}' isn't an option for {prop.get('label')}. Options: "
                         f"{', '.join(o['label'] for o in options[:30])}.")

    def _value(self, prop: dict[str, Any], value: Any) -> str:
        """One value in Dave's words -> what HubSpot's filter expects."""
        if prop["name"] == "hubspot_owner_id" or prop.get("referencedObjectType") == "OWNER":
            return self.owner_id(str(value))
        kind = prop.get("type")
        if prop["name"] == "hs_call_disposition" or (kind == "enumeration" and prop.get("fieldType") != "booleancheckbox"):
            return self._option(prop, value)
        if kind == "bool" or prop.get("fieldType") == "booleancheckbox":
            return "true" if str(value).strip().lower() in ("true", "yes", "1", "y") else "false"
        if kind == "number":
            return str(_number(value))
        if kind in ("date", "datetime"):
            return self._millis(prop, value, "start")
        return str(value)

    def _span(self, prop: dict[str, Any], phrase: Any) -> tuple[date | None, date | None, str]:
        lean = "future" if prop["name"] in FUTURE_FIELDS else "past"
        p = period(str(phrase), self.today, lean)
        if not p:
            raise ValueError(f"Couldn't read '{phrase}' as a date or period. Try 'last month', 'Q3', "
                             f"'since September', 'more than 6 months ago', or YYYY-MM-DD.")
        return p["from"], p["to"], p["label"]

    def _millis(self, prop: dict[str, Any], value: Any, end: str) -> str:
        """A date (or phrase) -> epoch ms: midnight UTC for date fields, local day bounds for datetimes."""
        start, stop, _ = self._span(prop, value)
        day = start if end == "start" else stop
        if day is None:
            day = stop if end == "start" else start
        if prop.get("type") == "date":
            moment = datetime.combine(day, dtime.min, timezone.utc)
        else:
            moment = datetime.combine(day, dtime.min if end == "start" else dtime.max, local_zone())
        return str(int(moment.timestamp() * 1000))

    # ── filters ──

    def build_filter(self, obj: str, f: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
        """{"field", "op", "value"} -> HubSpot filters (a date span can take two) and words saying what it did."""
        prop = self.field(obj, f.get("field", ""))
        op = (f.get("op") or "is").strip().lower()
        if op not in OPS:
            raise ValueError(f"Unknown op '{op}'. Use one of: {', '.join(OPS)}.")
        name, label, value = prop["name"], prop.get("label", prop["name"]), f.get("value")
        is_date = prop.get("type") in ("date", "datetime")
        if op == "has_value":
            return [{"propertyName": name, "operator": "HAS_PROPERTY"}], f"{label} is filled in"
        if op == "is_empty":
            return [{"propertyName": name, "operator": "NOT_HAS_PROPERTY"}], f"{label} is empty"
        if name in STATE_FIELDS and op in ("is", "is_not", "any_of", "none_of"):
            given = value if isinstance(value, list) else [value]
            forms = [f for v in given for f in state_forms(v)]
            positive = op in ("is", "any_of")
            return ([{"propertyName": name, "operator": "IN" if positive else "NOT_IN", "values": forms}],
                    f"{label} {'is' if positive else 'is not'} {' or '.join(map(str, given))}")
        if is_date and op in ("is", "during", "between"):
            phrase = " and ".join(map(str, value)) if isinstance(value, list) else value
            start, stop, words = self._span(prop, phrase)
            out = []
            if start:
                out.append({"propertyName": name, "operator": "GTE", "value": self._millis(prop, start.isoformat(), "start")})
            if stop:
                out.append({"propertyName": name, "operator": "LTE", "value": self._millis(prop, stop.isoformat(), "end")})
            return out, f"{label}: {words}"
        if is_date and op in ("before", "after", "less_than", "more_than", "at_least", "at_most"):
            start, stop, words = self._span(prop, value)
            if op in ("before", "less_than"):
                edge = start or stop
                return ([{"propertyName": name, "operator": "LT", "value": self._millis(prop, edge.isoformat(), "start")}],
                        f"{label} before {edge:%b %d, %Y}".replace(" 0", " "))
            if op == "at_most":
                edge = stop or start
                return ([{"propertyName": name, "operator": "LTE", "value": self._millis(prop, edge.isoformat(), "end")}],
                        f"{label} on or before {edge:%b %d, %Y}".replace(" 0", " "))
            edge = stop or start
            if op == "at_least":
                edge = start or stop
                return ([{"propertyName": name, "operator": "GTE", "value": self._millis(prop, edge.isoformat(), "start")}],
                        f"{label} on or after {edge:%b %d, %Y}".replace(" 0", " "))
            return ([{"propertyName": name, "operator": "GT", "value": self._millis(prop, edge.isoformat(), "end")}],
                    f"{label} after {edge:%b %d, %Y}".replace(" 0", " "))
        if op in ("any_of", "none_of"):
            values = value if isinstance(value, list) else [v.strip() for v in str(value).split(",")]
            converted = [self._value(prop, v) for v in values]
            return ([{"propertyName": name, "operator": "IN" if op == "any_of" else "NOT_IN", "values": converted}],
                    f"{label} {'is one of' if op == 'any_of' else 'is none of'} {', '.join(map(str, values))}")
        if op in ("contains", "not_contains"):
            return ([{"propertyName": name, "operator": "CONTAINS_TOKEN" if op == "contains" else "NOT_CONTAINS_TOKEN",
                      "value": f"*{str(value).strip('*')}*"}], f"{label} {'contains' if op == 'contains' else 'does not contain'} '{value}'")
        if op == "between":
            low, high = (value if isinstance(value, list) else str(value).split(" and "))[:2]
            return ([{"propertyName": name, "operator": "BETWEEN", "value": self._value(prop, low), "highValue": self._value(prop, high)}],
                    f"{label} between {low} and {high}")
        operator = {"is": "EQ", "is_not": "NEQ", "more_than": "GT", "less_than": "LT", "at_least": "GTE", "at_most": "LTE"}[op]
        shown = value
        if prop["name"] == "hubspot_owner_id":
            shown = self.owner_name(self._value(prop, value)) or value
        return ([{"propertyName": name, "operator": operator, "value": self._value(prop, value)}],
                f"{label} {op.replace('_', ' ')} {shown}")

    def company_id(self, words: str) -> tuple[str, str, list[str]]:
        """'Equinox' or 'equinoxinc.org' -> (id, name, other records that also matched).

        TAG's HubSpot has duplicates ("Sawyer Savings Bank" twice, an "Equinox" next to "Equinox, Inc."),
        so among the records whose names match, the one with real history wins: people on it, deals,
        lifecycle, how recently anyone was in touch. Only a genuine toss-up goes back to Dave."""
        w = (words or "").strip()
        if w.isdigit():
            return w, w, []
        fields = ["name", "domain", "num_associated_contacts", "num_associated_deals", "lifecyclestage",
                  "notes_last_contacted", "hubspot_owner_id"]
        if "." in w and " " not in w:
            rows = self.hs.search("companies", filters=[{"propertyName": "domain", "operator": "EQ", "value": w.lower()}],
                                  properties=fields, limit=5).get("results", [])
        else:
            rows = self.hs.search("companies", query=w, properties=fields, limit=10).get("results", [])
        if not rows:
            raise ValueError(f"No HubSpot company matches '{words}'.")

        def named(r: dict) -> float:
            return name_score(w, r["properties"].get("name") or "", r["properties"].get("domain") or "")

        def history(r: dict) -> float:
            p = r["properties"]
            weight = float(p.get("num_associated_contacts") or 0) + 3 * float(p.get("num_associated_deals") or 0)
            weight += {"customer": 20, "opportunity": 10, "salesqualifiedlead": 5}.get(p.get("lifecyclestage") or "", 0)
            if p.get("hubspot_owner_id"):
                weight += 2
            touched = p.get("notes_last_contacted") or ""
            if touched:
                age = (self.today - date.fromisoformat(touched[:10])).days
                weight += 10 if age < 90 else 4 if age < 365 else 1
            return weight

        top = max(named(r) for r in rows)
        close = [r for r in rows if named(r) >= max(0.8, top - 0.12)]
        close.sort(key=lambda r: (-history(r), -named(r)))
        best = close[0]
        describe = lambda r: f"{r['properties'].get('name')} ({r['properties'].get('domain') or 'no domain'})"  # noqa: E731
        same = lambda a, b: re.sub(r"\W+|\b(inc|llc|corp|co|ltd)\b", "", (a["properties"].get("name") or "").lower()) == \
            re.sub(r"\W+|\b(inc|llc|corp|co|ltd)\b", "", (b["properties"].get("name") or "").lower())  # noqa: E731
        rival = next((r for r in close[1:] if not same(r, best)), None)
        # The same name twice is a duplicate record of one company: take the fuller one, mention the other.
        # Two different names that both have history is a real question for Dave.
        if rival is not None and history(rival) >= 0.6 * history(best) and history(best) > 0:
            raise ValueError("Several companies match, each with history: " + "; ".join(
                f"{describe(r)}, id {r['id']}" for r in close[:4]) + ". Ask Dave which, or pass the domain or id.")
        return best["id"], best["properties"].get("name") or w, [describe(r) for r in close[1:4]]

    # ── shaping results ──

    def show(self, obj: str, prop_name: str, raw: Any) -> Any:
        if raw in (None, ""):
            return None
        if prop_name == "hubspot_owner_id":
            return self.owner_name(raw) or raw
        if prop_name == "dealstage":
            return self.stages().get(raw, {}).get("label", raw)
        if prop_name == "pipeline":
            return next((p["label"] for p in self.hs.pipelines("deals") if p["id"] == raw), raw)
        if prop_name in ("hs_note_body", "hs_call_body", "hs_meeting_body", "hs_email_text"):
            return _plain(str(raw))
        if prop_name == "hs_call_disposition":
            return self.hs.call_outcomes().get(str(raw), raw)
        prop = self.props(obj).get(prop_name) or {}
        kind = prop.get("type")
        if kind == "enumeration" and prop.get("options"):
            labels = {str(o["value"]): o["label"] for o in prop["options"]}
            return "; ".join(labels.get(v, v) for v in str(raw).split(";"))
        if kind in ("date", "datetime") or re.fullmatch(r"\d{4}-\d{2}-\d{2}(T.*)?", str(raw)):
            try:
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(raw)):
                    d = date.fromisoformat(str(raw))
                else:
                    d = datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(local_zone()).date()
                return f"{d:%b %d, %Y}".replace(" 0", " ")
            except (ValueError, OSError, OverflowError):   # junk dates (year 1) can't be converted on Windows
                return str(raw)[:10]
        if prop_name == "hs_call_duration":
            seconds = int(float(raw)) // 1000
            return f"{seconds // 60}m {seconds % 60:02d}s"
        if kind == "number":
            n = float(raw)
            return round(n, 2) if n % 1 else int(n)
        return raw

    def row(self, obj: str, record: dict[str, Any], extra: list[str] = ()) -> dict[str, Any]:
        p, shape = record.get("properties") or {}, SHAPES[obj]
        title = " ".join(str(self.show(obj, k, p.get(k)) or "") for k in shape["title"]).strip() or f"({obj[:-1]} {record['id']})"
        out: dict[str, Any] = {"id": record["id"], "title": title[:140]}
        sub = [str(self.show(obj, k, p.get(k))) for k in shape["sub"] if p.get(k)]
        if sub:
            out["sub"] = " · ".join(sub)
        for k in [*shape["show"], *extra]:
            value = self.show(obj, k, p.get(k))
            if value not in (None, ""):
                label = (self.props(obj).get(k) or {}).get("label", k)
                out[_clean_label(label).replace(" ", "_") if k not in ("hubspot_owner_id",) else "owner"] = value
        try:
            out["url"] = self.hs.record_url(obj, record["id"])
        except Exception:  # noqa: BLE001 - a row without a link is still a row
            pass
        return out

    def wanted_props(self, obj: str, extra: list[str] = ()) -> list[str]:
        shape = SHAPES[obj]
        return list(dict.fromkeys([*shape["title"], *shape["sub"], *shape["show"], *extra]))

    # ── the query ──

    def query(self, args: dict[str, Any]) -> dict[str, Any]:
        obj, common, extra, said, sort_prop, direction = self._search(args)
        group_word, sum_word = args.get("group_by"), args.get("sum")
        return self._answer(obj, args, common, extra, said, sort_prop, direction, group_word, sum_word)

    def export(self, args: dict[str, Any], max_rows: int = 10000) -> tuple[list[str], list[list[str]], int]:
        """Every record a query matches (not just the card's page), as spreadsheet columns and rows."""
        obj, common, extra, _, _, _ = self._search(args)
        records, total = self.hs.search_all(obj, max_rows=max_rows, **common)
        rows = [self.row(obj, r, extra) for r in records]
        keys = list(dict.fromkeys(k for r in rows for k in r if k not in ("id", "url")))
        heads = ["Name" if k == "title" else "Details" if k == "sub" else k.replace("_", " ").capitalize() for k in keys]
        return [*heads, "HubSpot link"], [[str(r.get(k, "")) for k in keys] + [r.get("url", "")] for r in rows], total

    def _search(self, args: dict[str, Any]) -> tuple[str, dict[str, Any], list[str], list[str], str, str]:
        """Dave's question as a HubSpot search: the filters, columns and order, plus the conditions in words."""
        obj = object_type(args.get("object", ""))
        filters, said = [], []
        for f in args.get("filters") or []:
            built, words = self.build_filter(obj, f)
            filters += built
            said.append(words)
        if args.get("company"):
            if obj == "companies":
                raise ValueError("To look at one company, use hubspot_record.")
            cid, cname, others = self.company_id(str(args["company"]))
            filters.append({"propertyName": "associations.company", "operator": "EQ", "value": cid})
            said.append(f"at {cname}")
            if others:
                said.append(f"(other HubSpot records with a similar name, not included: {'; '.join(others)})")
        groups = None
        either = args.get("either") or []
        if either:
            groups = []
            for f in either[:5]:
                built, words = self.build_filter(obj, f)
                groups.append({"filters": filters + built})
            said.append("either " + " or ".join(self.build_filter(obj, f)[1] for f in either[:5]))
        if len(filters) > 6 or (groups and any(len(g["filters"]) > 6 for g in groups)):
            raise ValueError("Too many conditions for one HubSpot search (6 at most). Drop the least important.")
        text = (args.get("text") or "").strip()
        if text:
            said.append(f"matching '{text}'")

        extra = [self.field(obj, w)["name"] for w in args.get("show") or []]
        sort_word = args.get("sort_by")
        sort_prop = self.field(obj, sort_word)["name"] if sort_word else SHAPES[obj].get("when") or SHAPES[obj]["modified"]
        direction = "ASCENDING" if (args.get("order") or ("ascending" if sort_prop in FUTURE_FIELDS else "descending")).lower().startswith("asc") else "DESCENDING"
        common = {"properties": self.wanted_props(obj, [*extra, sort_prop]), "query": text,
                  "sorts": [{"propertyName": sort_prop, "direction": direction}]}
        if groups is not None:
            common["filter_groups"] = groups
        else:
            common["filters"] = filters
        return obj, common, extra, said, sort_prop, direction

    def _answer(self, obj: str, args: dict[str, Any], common: dict[str, Any], extra: list[str], said: list[str],
                sort_prop: str, direction: str, group_word: Any, sum_word: Any) -> dict[str, Any]:
        limit = max(1, min(int(args.get("limit") or 15), 50))
        out: dict[str, Any] = {"object": obj, "conditions": said or ["all " + obj]}
        if group_word or sum_word:
            gprop = self.field(obj, group_word) if group_word else None
            sprop = self.field(obj, sum_word) if sum_word else None
            if sprop and sprop.get("type") != "number":
                raise ValueError(f"{sprop.get('label')} isn't a number, so it can't be added up.")
            needed = [p["name"] for p in (gprop, sprop) if p]
            categorical = bool(gprop) and gprop.get("type") not in ("date", "datetime", "number") and not sprop
            # Counting by a category over a big set: a sample shows which values occur, then HubSpot
            # counts each one exactly. Sums need every record, so those page through.
            cap = 1000 if categorical else MAX_AGGREGATE_ROWS
            rows, total = self.hs.search_all(obj, max_rows=cap, **{**common, "properties": self.wanted_props(obj, [*extra, *needed, sort_prop])})
            out["matching"] = total
            buckets: dict[Any, list[float]] = defaultdict(list)
            raw_of: dict[str, Any] = {}
            for r in rows:
                p = r.get("properties") or {}
                key = "all"
                if gprop:
                    raw = p.get(gprop["name"])
                    if gprop.get("type") in ("date", "datetime") and raw:
                        d = raw[:10]
                        key = f"{date.fromisoformat(d):%Y-%m} ({date.fromisoformat(d):%b %Y})"
                    else:
                        key = self.show(obj, gprop["name"], raw) or "(none)"
                    raw_of.setdefault(str(key), raw)
                buckets[str(key)].append(_number(p.get(sprop["name"])) if sprop and p.get(sprop["name"]) not in (None, "") else 0.0)
            exact: dict[str, int] = {}
            if categorical and total > len(rows):
                def count(extra_filter: dict[str, Any]) -> int:
                    base = common.get("filter_groups") or [{"filters": common.get("filters") or []}]
                    found = self.hs.search(obj, filter_groups=[{"filters": g["filters"] + [extra_filter]} for g in base],
                                           query=common.get("query") or "", properties=["hs_object_id"], limit=1)
                    return found.get("total", 0)
                for key, raw in list(raw_of.items())[:25]:
                    exact[key] = count({"propertyName": gprop["name"], "operator": "NOT_HAS_PROPERTY"} if raw in (None, "")
                                       else {"propertyName": gprop["name"], "operator": "EQ", "value": raw})
                rest = total - sum(exact.values())
                if rest > 0:
                    exact["(other values)"] = rest
            elif total > len(rows):
                out["counted"] = len(rows)
                out["note"] = f"Only the first {len(rows)} of {total} were counted; say so."
            if sprop:
                out["total"] = round(sum(sum(v) for v in buckets.values()), 2)
                out["with_a_value"] = sum(1 for r in rows if (r.get("properties") or {}).get(sprop["name"]) not in (None, "", "0"))
            if gprop:
                if exact:
                    ordered_counts = sorted(exact.items(), key=lambda kv: (kv[0] == "(other values)", -kv[1], kv[0]))
                    out["groups"] = [{"group": k, "count": n} for k, n in ordered_counts]
                else:
                    if gprop.get("type") in ("date", "datetime"):
                        ordered = sorted(buckets.items())                       # by month, in order
                    else:
                        ordered = sorted(buckets.items(), key=lambda kv: (-(sum(kv[1]) if sprop else len(kv[1])), kv[0]))
                    out["groups"] = [{"group": k, "count": len(v), **({"total": round(sum(v), 2)} if sprop else {})}
                                     for k, v in ordered[:30]]
                    if len(ordered) > 30:
                        out["groups_not_shown"] = len(ordered) - 30
            out["rows"] = [self.row(obj, r, extra) for r in rows[:min(limit, 10)]]
        else:
            page = self.hs.search(obj, limit=limit, **common)
            out["matching"] = page.get("total", 0)
            out["rows"] = [self.row(obj, r, extra) for r in page.get("results", [])]
            if out["matching"] > len(out["rows"]):
                out["showing"] = f"the {'latest' if direction == 'DESCENDING' else 'earliest'} {len(out['rows'])} by {sort_prop}"
        if obj == "deals":
            out["about_deals"] = DEALS_CAVEAT
        return out

    # ── who's most likely to buy: ranked in code, each with its reasons ──

    def prospects(self, args: dict[str, Any], everyone: bool = False) -> dict[str, Any]:
        """Sales plays from Khai's 'Sales Lead Prospector' agent in Hatz, run in code. Scores are TAG's
        2026 combined lead scoring (fit + engagement, tiers A1-C3).
        - engaged (default): not yet clients; on TAG's website in the last 30 days first, then the 2026 score.
          Email clicks alone are weak (security scanners click every link), so clicks with no visit are flagged.
        - neglected: Lead/MQL/SQL/Opportunity with no activity in 180+ days, oldest first; unassigned or
          former-rep owners called out.
        - tier: one scoring tier ("A3" is RJ's Tier 1 MQL list), MQLs unless a stage is given, best score first."""
        play = (args.get("play") or ("tier" if args.get("tier") else "engaged")).lower()
        if play not in PLAYS:
            raise ValueError(f"play must be one of {', '.join(PLAYS)}")
        limit = EXPORT_MAX if everyone else max(5, min(int(args.get("limit") or 15), 40))   # everyone: the export
        tier = str(args.get("tier") or "").strip().upper()
        if play == "tier" and tier not in TIERS:
            raise ValueError(f"tier must be one of {', '.join(TIERS)} (A3 is RJ's Tier 1 MQL list)")
        stages = {"engaged": PROSPECT_STAGES, "neglected": OPEN_STAGES, "tier": ["marketingqualifiedlead"]}[play]
        base = [{"propertyName": "lifecyclestage", "operator": "IN", "values": stages},
                {"propertyName": "email", "operator": "NOT_CONTAINS_TOKEN", "value": "*@" + (os.environ.get("INTERNAL_DOMAIN") or "tagsolutions.com")}]
        said = [{"engaged": "not yet clients (subscriber, lead, MQL, SQL, opportunity, former customer)",
                 "neglected": "Lead, MQL, SQL or Opportunity with no activity in 180+ days (or none ever)",
                 "tier": f"MQLs in scoring tier {tier}" + (" (RJ's Tier 1 list)" if tier == "A3" else "")}[play]]
        if play == "tier":
            base.append({"propertyName": "lead_scoring_combined_2026_threshold", "operator": "EQ", "value": tier})
        if args.get("owner"):
            base.append({"propertyName": "hubspot_owner_id", "operator": "EQ", "value": self.owner_id(str(args["owner"]))})
            said.append(f"owned by {self.owner_name(self.owner_id(str(args['owner'])))}")
        if args.get("company"):
            cid, cname, _ = self.company_id(str(args["company"]))
            base.append({"propertyName": "associations.company", "operator": "EQ", "value": cid})
            said.append(f"at {cname}")
        props = list(dict.fromkeys([*self.wanted_props("contacts"), *PROSPECT_PROPS]))
        pool = EXPORT_MAX if everyone else 200

        def num(p: dict[str, Any], k: str) -> float:
            try:
                return float(p.get(k) or 0)
            except ValueError:
                return 0.0

        recent: set[str] = set()
        if play == "engaged":
            since = int((datetime.combine(self.today - timedelta(days=30), dtime.min, local_zone())).timestamp() * 1000)
            visitors, _ = self.hs.search_all("contacts", max_rows=pool, properties=props,
                                             filters=base + [{"propertyName": "hs_analytics_last_visit_timestamp", "operator": "GTE", "value": since}],
                                             sorts=[{"propertyName": "hs_analytics_last_visit_timestamp", "direction": "DESCENDING"}])
            scored, total = self.hs.search_all("contacts", max_rows=pool, properties=props,
                                               filters=base + [{"propertyName": SCORE, "operator": "GT", "value": 0}],
                                               sorts=[{"propertyName": SCORE, "direction": "DESCENDING"}])
            people = {r["id"]: r for r in [*visitors, *scored]}
            recent = {r["id"] for r in visitors}
            order = lambda r: (r["id"] not in recent, -num(r["properties"], SCORE), -num(r["properties"], SCORE + "_engagement"))  # noqa: E731
        elif play == "neglected":
            cutoff = int((datetime.combine(self.today - timedelta(days=180), dtime.min, local_zone())).timestamp() * 1000)
            found, total = self.hs.search_all("contacts", max_rows=pool, properties=props, filter_groups=[
                {"filters": base + [{"propertyName": "notes_last_updated", "operator": "LT", "value": cutoff}]},
                {"filters": base + [{"propertyName": "notes_last_updated", "operator": "NOT_HAS_PROPERTY"}]}],
                sorts=[{"propertyName": SCORE, "direction": "DESCENDING"}])     # the best fits of all of them, not the oldest few
            people = {r["id"]: r for r in found}
            # Best fit first, then the longest forgotten: oldest-first alone led with never-touched poor fits
            order = lambda r: (-num(r["properties"], SCORE), r["properties"].get("notes_last_updated") or "")  # noqa: E731
        else:
            found, total = self.hs.search_all("contacts", max_rows=pool, properties=props, filters=base,
                                              sorts=[{"propertyName": SCORE, "direction": "DESCENDING"}])
            people = {r["id"]: r for r in found}
            order = lambda r: -num(r["properties"], SCORE)  # noqa: E731
        # Vendors' people (KnowBe4, Dell, VMware) aren't prospects
        people = {k: r for k, r in people.items()
                  if (r["properties"].get("email") or "").rsplit("@", 1)[-1].lower() not in VENDOR_DOMAINS}
        ranked = sorted(people.values(), key=order)

        rows = []
        for r in ranked[:limit]:
            p = r["properties"]
            row = self.row("contacts", r)
            why = []
            views = int(num(p, "hs_analytics_num_page_views"))
            if r["id"] in recent:
                why.append(f"on the website {self.show('contacts', 'hs_analytics_last_visit_timestamp', p.get('hs_analytics_last_visit_timestamp'))}"
                           + (f" ({_plural(views, 'page view')} in all)" if views else ""))
            elif views and play == "engaged":
                why.append(f"{_plural(views, 'website page view')} in all")
            if play == "neglected":
                last = p.get("notes_last_updated")
                why.append(f"last activity {self.show('contacts', 'notes_last_updated', last)}" if last else "no activity ever logged")
                owner = row.get("owner") or ""
                if not p.get("hubspot_owner_id"):
                    row["caution"] = "Unassigned: no owner"
                elif "(former)" in owner:
                    row["caution"] = f"Owner is a former rep: {owner.replace(' (former)', '')}"
            if p.get(SCORE + "_threshold") or num(p, SCORE):
                # The 2026 engagement part is 0 for every contact (checked Oct 2026): show it only once it's real
                engaged = int(num(p, SCORE + "_engagement"))
                why.append(f"tier {p.get(SCORE + '_threshold') or '?'}, fit score {int(num(p, SCORE + '_fit'))}"
                           + (f", engagement {engaged}" if engaged else ""))
            clicks = int(num(p, "hs_email_click"))
            if clicks and play != "neglected":
                why.append(f"clicked {_plural(clicks, 'marketing email')}, last "
                           f"{self.show('contacts', 'hs_email_last_click_date', p.get('hs_email_last_click_date'))}")
            if play == "engaged" and clicks >= 5 and not views:
                row["caution"] = "Email clicks but never on the website: could be an email security scanner, not a person"
            row["why"] = "; ".join(why)
            rows.append(row)
        # Several people at one company says more than any one of them. The same company is often typed two
        # ways ("Hudson Headwaters", "Hudson Headwaters Health Network"): the shorter name wins.
        names = sorted({(r["properties"].get("company") or "").strip() for r in ranked[:120]} - {""}, key=len)
        canon = {n: next((m for m in names if n.lower().startswith(m.lower())), n) for n in names}
        by_company: dict[str, list[str]] = defaultdict(list)
        for r in ranked[:120]:
            company = canon.get((r["properties"].get("company") or "").strip())
            person = " ".join(filter(None, [r["properties"].get("firstname"), r["properties"].get("lastname")]))
            if company and person and person not in by_company[company]:
                by_company[company].append(person)
        accounts = [{"company": c, "people": len(n), "names": n[:8]} for c, n in
                    sorted(by_company.items(), key=lambda kv: -len(kv[1])) if len(n) >= 2][:6]
        how = {"engaged": ("People on TAG's website in the last 30 days first, then by TAG's 2026 lead score. Email clicks "
                           "alone don't rank anyone: security scanners click links too."),
               "neglected": ("Best 2026 fit score first, then the longest without activity. Unassigned contacts and "
                             "former reps' contacts are flagged."),
               "tier": "Highest 2026 lead score first."}[play]
        return {"object": "contacts", "play": play, "conditions": said, "considered": len(people), "in_hubspot": total,
                "rows": rows, "accounts": accounts, "how_ranked": how,
                "scoring_note": ("TAG's 2026 lead score is fit only for now: its engagement part is 0 for every contact, so "
                                 "the tiers are only A3/B3/C3 and a high score means a good fit, not interest. Website "
                                 "visits are the interest signal."),
                "export": (f"The card shows the top {len(rows)}; its 'Export the ranked list' button downloads "
                           f"{f'all {total:,}' if total <= EXPORT_MAX else f'the top {EXPORT_MAX:,} of {total:,}'} ranked, "
                           "with each one's reasons, as a spreadsheet. Point Dave to it for a list; say that number.")}

    def export_prospects(self, args: dict[str, Any]) -> tuple[list[str], list[list[str]], int]:
        """The whole ranked list behind a prospects card, as spreadsheet rows, best first."""
        out = self.prospects(args, everyone=True)
        heads = ["Rank", "Name", "Title and company", "Owner", "Email", "Lifecycle stage", "Why", "Caution", "HubSpot link"]
        rows = [[str(i), r.get("title", ""), r.get("sub", ""), r.get("owner", ""), r.get("email", ""),
                 r.get("lifecycle_stage", ""), r.get("why", ""), r.get("caution", ""), r.get("url", "")]
                for i, r in enumerate(out["rows"], 1)]
        return heads, rows, out["considered"]

    # ── one rep's calling: calls, conversations, appointments ──

    def rep_activity(self, args: dict[str, Any]) -> dict[str, Any]:
        """'How many calls did Garrett make today, and how many appointments did he get?', worked out
        in code. Appointments booked are meetings *made* in the period; the meetings on the calendar
        in the period are a different number (asked once, it gave 0 booked when there was 1)."""
        who, when = args.get("person") or "me", args.get("period") or "today"
        owner = {"field": "owner", "op": "is", "value": who}
        calls = self.query({"object": "calls", "filters": [owner, {"field": "when", "op": "during", "value": when}],
                            "group_by": "outcome"})
        outcomes = calls.get("groups") or []
        talked = [g for g in outcomes if g["group"].lower() in CONVERSATION]
        booked = self.query({"object": "meetings", "filters": [owner, {"field": "booked", "op": "during", "value": when}],
                             "sort_by": "when", "order": "ascending", "limit": 25})
        held = self.query({"object": "meetings", "filters": [owner, {"field": "when", "op": "during", "value": when}],
                           "sort_by": "when", "order": "ascending", "limit": 25})
        span = next((c.split(": ", 1)[1] for c in calls["conditions"] if c.startswith("Activity date")), str(when))
        return {"person": self.owner_name(self.owner_id(who)), "period": span,
                "calls": calls.get("matching", 0), "outcomes": outcomes,
                "conversations": sum(g["count"] for g in talked),
                "counted_as_conversation": [g["group"] for g in talked],
                "meetings_set_on_calls": sum(g["count"] for g in outcomes if g["group"].lower() in MEETING_OUTCOMES),
                "appointments_booked": booked.get("matching", 0), "booked": booked.get("rows", []),
                "meetings_in_period": held.get("matching", 0), "meetings": held.get("rows", []),
                "note": ("appointments_booked are meetings made in the period, whatever day they're for; "
                         "meetings_in_period are the ones held or scheduled in it. Say which you mean.")}

    # ── one record, the whole picture ──

    def record(self, args: dict[str, Any]) -> dict[str, Any]:
        obj = object_type(args.get("object") or "company")
        words = str(args.get("name") or args.get("id") or "").strip()
        if not words:
            raise ValueError("Say which record: a name, domain, email or id.")
        others: list[str] = []
        if obj == "companies":
            rid, _, others = self.company_id(words)
        elif obj == "contacts":
            if "@" in words:
                rows = self.hs.search("contacts", filters=[{"propertyName": "email", "operator": "EQ", "value": words.lower()}],
                                      properties=["firstname"], limit=2)["results"]
            else:
                rows = self.hs.search("contacts", query=words, properties=["firstname", "lastname", "company", "jobtitle", "email"], limit=6)["results"]
            if not rows:
                raise ValueError(f"No HubSpot contact matches '{words}'.")
            if len(rows) > 1 and "@" not in words:
                scored = sorted(rows, key=lambda r: -name_score(words, f"{r['properties'].get('firstname') or ''} {r['properties'].get('lastname') or ''}", r["properties"].get("email") or ""))
                top = [r for r in scored if name_score(words, f"{r['properties'].get('firstname') or ''} {r['properties'].get('lastname') or ''}", r["properties"].get("email") or "") >= 0.85]
                if len(top) != 1:
                    return {"choose": [self.row("contacts", r) for r in scored[:6]],
                            "note": "Several contacts match; ask Dave which (or search by email)."}
                rows = top
            rid = rows[0]["id"]
        else:
            if words.isdigit():
                rid = words
            else:
                rows = self.hs.search("deals", query=words, properties=["dealname"], limit=5)["results"]
                if not rows:
                    raise ValueError(f"No HubSpot deal matches '{words}'.")
                rid = rows[0]["id"]

        keep = [k for k in self.props(obj) if not k.startswith("hs_") or k in ("hs_lead_status",)]
        found = self.hs.search(obj, filters=[{"propertyName": "hs_object_id", "operator": "EQ", "value": rid}],
                               properties=keep[:300], limit=1).get("results") or [{}]
        props = {k: v for k, v in (found[0].get("properties") or {}).items() if v not in (None, "", "false")}
        labels = {k: (self.props(obj).get(k) or {}).get("label", k) for k in props}
        out: dict[str, Any] = {"object": obj, **self.row(obj, {"id": rid, "properties": props})}
        skip = {"hs_object_id", "createdate", "lastmodifieddate", "hs_lastmodifieddate", "hubspot_owner_assigneddate"}
        out["details"] = {_clean_label(labels[k]): self.show(obj, k, v) for k, v in sorted(props.items())
                          if k not in skip and not k.startswith(("zoominfo_company_id", "company_zoominfo_url"))
                          and len(str(v)) < 400}
        out["created"] = self.show(obj, "createdate", props.get("createdate"))
        if others:
            out["also_in_hubspot"] = {"records": others, "note": "Other records with a similar name (duplicates or "
                                      "different companies); this one has the most history. Mention them if it matters."}

        assoc_key = {"companies": "associations.company", "contacts": "associations.contact", "deals": "associations.deal"}[obj]
        link = [{"propertyName": assoc_key, "operator": "EQ", "value": rid}]
        if obj != "contacts":
            people = self.hs.search("contacts", filters=link, properties=self.wanted_props("contacts"),
                                    sorts=[{"propertyName": "notes_last_contacted", "direction": "DESCENDING"}], limit=10)
            out["contacts"] = {"count": people.get("total", 0), "rows": [self.row("contacts", r) for r in people.get("results", [])]}
        if obj != "deals":
            deals = self.hs.search("deals", filters=link, properties=self.wanted_props("deals"),
                                   sorts=[{"propertyName": "createdate", "direction": "DESCENDING"}], limit=10)
            out["deals"] = {"count": deals.get("total", 0), "rows": [self.row("deals", r) for r in deals.get("results", [])],
                            "about_deals": DEALS_CAVEAT}
        recent = []
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        for kind in ("meetings", "calls", "notes", "emails"):
            found = self.hs.search(kind, filters=link + [{"propertyName": "hs_timestamp", "operator": "LTE", "value": str(now_ms)}],
                                   properties=self.wanted_props(kind, {"notes": ["hs_note_body"], "calls": ["hs_call_body"],
                                                                       "emails": ["hs_email_text"]}.get(kind, [])),
                                   sorts=[{"propertyName": "hs_timestamp", "direction": "DESCENDING"}], limit=5)
            out[f"{kind}_count"] = found.get("total", 0)
            for r in found.get("results", []):
                row = self.row(kind, r)
                p = r.get("properties") or {}
                if kind == "calls" and p.get("hs_call_body"):
                    row["notes"] = _plain(p["hs_call_body"], 200)
                if kind == "emails" and p.get("hs_email_text"):
                    row["text"] = _plain(p["hs_email_text"], 200)
                row["kind"] = kind[:-1]
                row["_when"] = (r.get("properties") or {}).get("hs_timestamp") or ""
                recent.append(row)
        upcoming = self.hs.search("meetings", filters=link + [{"propertyName": "hs_timestamp", "operator": "GT", "value": str(now_ms)}],
                                  properties=self.wanted_props("meetings"),
                                  sorts=[{"propertyName": "hs_timestamp", "direction": "ASCENDING"}], limit=3)
        if upcoming.get("results"):
            out["upcoming_meetings"] = [self.row("meetings", r) for r in upcoming["results"]]
        tasks = self.hs.search("tasks", filters=link + [{"propertyName": "hs_task_status", "operator": "NEQ", "value": "COMPLETED"}],
                               properties=self.wanted_props("tasks"), sorts=[{"propertyName": "hs_timestamp", "direction": "ASCENDING"}], limit=5)
        if tasks.get("results"):
            out["open_tasks"] = {"count": tasks.get("total", 0), "rows": [self.row("tasks", r) for r in tasks["results"]]}
        recent.sort(key=lambda r: r.pop("_when", ""), reverse=True)
        out["recent_activity"] = recent[:10]
        # Every count about this record in one place, named for what it counts: a reply once called the
        # company's 257 employees "257 contacts"
        out["in_hubspot"] = {k: v for k, v in {
            "people (contacts) in HubSpot": (out.get("contacts") or {}).get("count"),
            "deals in HubSpot": (out.get("deals") or {}).get("count"),
            "meetings logged": out.get("meetings_count"), "calls logged": out.get("calls_count"),
            "notes": out.get("notes_count"), "emails logged": out.get("emails_count"),
            "open tasks": (out.get("open_tasks") or {}).get("count", 0),
        }.items() if v is not None}
        for key in ("number of employees", "employees"):
            if key in out["details"]:
                out["details"]["employees at the company (not contacts)"] = out["details"].pop(key)
        return out


# Call outcomes where the rep actually talked with someone (TAG's 34 outcomes, checked Oct 2026). Voicemail, no
# answer, wrong number, gatekeeper, gone, plain busy and the admin ones (bad info, do not call) are not.
CONVERSATION = {"connected", "interest, call back later", "interest, send info", "not interested, follow up",
                "busy call back", "busy, call back later", "busy, send information", "meeting scheduled", "meeting set",
                "meeting rescheduled", "rescheduled meeting", "referral", "not decision maker",
                "disqualified, reason in notes", "disqualified", "nurture", "no pitch", "send email"}
MEETING_OUTCOMES = {"meeting scheduled", "meeting set"}

# The sales plays of Khai's "Sales Lead Prospector" agent in Hatz (read Oct 2026), run in code
PLAYS = ("engaged", "neglected", "tier")
EXPORT_MAX = 3000          # a prospects export: all of RJ's A3 list (2,368) in one spreadsheet
# Not yet clients: TAG's lifecycle stages before Customer, plus former customers (win-backs)
PROSPECT_STAGES = ["subscriber", "lead", "marketingqualifiedlead", "salesqualifiedlead", "opportunity", "222236330"]
OPEN_STAGES = ["lead", "marketingqualifiedlead", "salesqualifiedlead", "opportunity"]
# TAG's 2026 combined lead scoring: a total, its fit and engagement parts, and a tier (letter = fit, number = engagement)
SCORE = "lead_scoring_combined_2026"
TIERS = ("A1", "A2", "A3", "B1", "B2", "B3", "C1", "C2", "C3")
VENDOR_DOMAINS = {"knowbe4.com", "dell.com", "vmware.com"}
PROSPECT_PROPS = ["company", SCORE, SCORE + "_fit", SCORE + "_engagement", SCORE + "_threshold", "notes_last_updated",
                  "hs_analytics_num_page_views", "hs_analytics_num_visits", "hs_analytics_last_visit_timestamp",
                  "hs_email_click", "hs_email_open", "hs_email_last_click_date"]

DEALS_CAVEAT = ("TAG's HubSpot deals are mostly copies of Autotask quotes made by Zapier: nearly all sit at "
                "'Appointment Scheduled' with no close date. Don't treat their stage or close date as a pipeline; "
                "for pipeline questions use Autotask opportunities (find_opportunities).")

QUERY_DESCRIPTION = """Ask TAG's HubSpot a question: find, count, add up or group companies, contacts, deals, calls,
meetings, notes, tasks or logged emails (sales emails, sequences, replies). Code finds the fields, works out dates and does all the arithmetic; quote its numbers.
How to ask:
- object: companies | contacts | deals | calls | meetings | notes | tasks | emails
- filters: [{field, op, value}] in plain words. field: e.g. owner, lifecycle, industry, employees, state, last contacted,
  created, title, outcome, status, due, amount, or any field label (hubspot_fields finds others).
  op: is, is_not, any_of, none_of, contains, more_than, less_than, at_least, at_most, between, during, before, after,
  has_value, is_empty.
  value: owner as a name or "me" (Dave); options by their label ("Customer"); dates as phrases ("last month", "Q3",
  "since September", "more than 6 months ago", "2026-10-01").
- either: [{field, op, value}] for OR (e.g. last contacted before X OR last contacted is_empty).
- company: a company name or domain, to look only at its contacts/deals/calls/meetings/notes/tasks.
- text: free-text match on names, emails, domains.
- group_by: a field to count by (owner, lifecycle, industry, outcome, or a date field = by month). sum: a number field.
- sort_by + order, limit (default 15), show: extra fields for each row.
Examples:
 "who hasn't been contacted in 6 months among customers" -> companies, filters [lifecycle is Customer],
   either [last contacted before "6 months ago", last contacted is_empty]
 "how many calls did RJ make last week" -> calls, filters [owner is RJ, when during "last week"]
 "appointments Garrett set today" -> meetings, filters [owner is Garrett, booked during "today"] ("when" is the
   meeting's own date: a meeting booked today is often next week). For a rep's day or week, hubspot_rep_activity.
 "most engaged leads" -> contacts, sort_by "lead score" (TAG's own score), show [engagement score, page views,
   website visits, email clicks]; or hubspot_prospects, which ranks them with reasons.
 "calls by person this month" -> calls, filters [when during "this month"], group_by owner
 "my open tasks" -> tasks, filters [owner is me, status is_not Completed], sort_by due, order ascending
 "contacts at Equinox" -> contacts, company "Equinox"
For one company/contact/deal's whole picture, use hubspot_record instead."""


def dave_links(hub: HubSpot) -> list[dict[str, str]]:
    """Dave's HubSpot scheduling links, his main one first (the others belong to marketing campaigns)."""
    user = os.environ.get("GRAPH_MAILBOX", "").lower().split("@")[0]
    mine = [l for l in hub.hs.meeting_links() if user and user[:6] in (l.get("link") or "")]
    mine.sort(key=lambda l: (len(l.get("link") or ""), l.get("name") or ""))
    return [{"name": l.get("name") or "", "url": l.get("link") or ""} for l in mine]


def booking_link(hub: HubSpot | None) -> str | None:
    """Dave's main booking link, or None (no HubSpot, or it can't be reached)."""
    if hub is None:
        return None
    try:
        links = dave_links(hub)
    except Exception:  # noqa: BLE001 - a reply without the link is still a good reply
        return None
    return links[0]["url"] if links else None


def hubspot_tools(hub: HubSpot) -> list[Tool]:
    def fields(args: dict[str, Any]) -> dict[str, Any]:
        obj = object_type(args.get("object", "companies"))
        return {"object": obj, "fields": hub.find_fields(obj, args.get("words") or "")}

    def links(args: dict[str, Any]) -> dict[str, Any]:
        return {"links": dave_links(hub), "note": "The first is Dave's main link; the others are for marketing campaigns."}

    return [
        Tool(ToolSpec("hubspot_query", QUERY_DESCRIPTION, {"type": "object", "properties": {
            "object": {"type": "string", "enum": list(OBJECT_WORDS)},
            "filters": {"type": "array", "items": {"type": "object", "properties": {
                "field": {"type": "string"}, "op": {"type": "string", "enum": list(OPS)},
                "value": {"description": "A word, number, date phrase, or a list for any_of/none_of/between"}},
                "required": ["field", "op"]}},
            "either": {"type": "array", "items": {"type": "object", "properties": {
                "field": {"type": "string"}, "op": {"type": "string", "enum": list(OPS)}, "value": {}},
                "required": ["field", "op"]}},
            "company": {"type": "string"},
            "text": {"type": "string"},
            "group_by": {"type": "string"}, "sum": {"type": "string"},
            "sort_by": {"type": "string"}, "order": {"type": "string", "enum": ["ascending", "descending"]},
            "limit": {"type": "integer"},
            "show": {"type": "array", "items": {"type": "string"}},
        }, "required": ["object"]}), handler=hub.query),
        Tool(ToolSpec("hubspot_record", "One company, contact or deal in HubSpot, the whole picture: owner, lifecycle, "
                      "details (ZoomInfo industry, size, revenue...), its contacts and deals, recent meetings, calls and "
                      "notes, upcoming meetings, open tasks. Use for 'what's going on with X', 'who do we know at X', "
                      "'when did we last talk to X'.", {"type": "object", "properties": {
                          "object": {"type": "string", "enum": ["companies", "contacts", "deals"]},
                          "name": {"type": "string", "description": "Company name or domain, contact name or email, deal name, or an id"}},
                          "required": ["object", "name"]}), handler=hub.record),
        Tool(ToolSpec("hubspot_fields", "Look up HubSpot fields by words (e.g. 'zoominfo industry', 'cyber score', "
                      "'distance albany'), with their options. Use when hubspot_query doesn't know a field.",
                      {"type": "object", "properties": {"object": {"type": "string", "enum": list(OBJECT_WORDS)},
                                                       "words": {"type": "string"}}, "required": ["object", "words"]}),
             handler=fields),
        Tool(ToolSpec("hubspot_prospects", "TAG's sales plays on HubSpot contacts, ranked in code with each person's "
                      "reasons, using TAG's 2026 lead scoring (score, fit, engagement, tier A1-C3). Plays: "
                      "'engaged' (default): who's most likely to buy now (on TAG's website lately, then score; email-only "
                      "clickers flagged as possible security scanners); 'neglected': leads/MQLs/SQLs/opportunities with no "
                      "activity in 180+ days, unassigned and former reps' contacts flagged; 'tier': one scoring tier, e.g. "
                      "'RJ's list', 'Ximena's list', 'top marketing leads' or 'List 3481' = tier A3 MQLs. Use for "
                      "'most likely prospects', 'most engaged', 'who should we call', 'neglected leads', 'RJ's list'.",
                      {"type": "object", "properties": {
                          "play": {"type": "string", "enum": list(PLAYS)},
                          "tier": {"type": "string", "enum": list(TIERS), "description": "For play 'tier' (A3 = RJ's Tier 1 MQL list)"},
                          "owner": {"type": "string", "description": "Only this owner's contacts: a name, or 'me' for Dave. Leave out for everyone's"},
                          "company": {"type": "string"},
                          "limit": {"type": "integer", "description": "How many people (default 15)"}}}), handler=hub.prospects),
        Tool(ToolSpec("hubspot_rep_activity", "One person's calling in HubSpot over a period, counted in code: calls made, "
                      "their outcomes, how many were real conversations (and which outcomes count as one), appointments "
                      "booked (meetings made in the period, whatever day they're for) and meetings held in the period. "
                      "Use for 'how many calls did Garrett make today and how many appointments did he get', "
                      "'how was RJ's week'.", {"type": "object", "properties": {
                          "person": {"type": "string", "description": "A name, or 'me' for Dave"},
                          "period": {"type": "string", "description": "A phrase: today, yesterday, this week, last week, October..."}},
                          "required": ["person"]}), handler=hub.rep_activity),
        Tool(ToolSpec("hubspot_meeting_link", "Dave's HubSpot scheduling link(s), for someone outside TAG to book time "
                      "with him themselves.", {"type": "object", "properties": {}}), handler=links),
    ]


# ── for meeting briefs: what TAG's CRM knows about the outside people ──

MAX_BRIEF_ITEMS = 8
BRIEF_EXCERPT = 700
BRIEF_LOOKBACK_DAYS = 540          # older activity rarely helps prepare for tomorrow's meeting
_INVITE_NOISE = re.compile(r"_{5,}|Microsoft Teams (?:meeting|Need help)|Join(?: the meeting now|:)\s*\S+|Meeting ID:?\s*[\d ]+"
                           r"|Passcode:?\s*\S+|Dial in by phone.*|For organizers:.*|Join on a video conferencing device.*", re.I)
# Logged automatically with nothing a person wrote: dialer attempts, call-recording links, booking confirmations
_DIALER = re.compile(r"ConnectAndSell|Agent Disposition|Agent Attempt|^Called \(?\d{3}\)?[\d\s-]+ from \d+\.? Recording:"
                     r"|Thank you for booking a meeting|Need to make changes\? \S* Reschedule", re.I)


def _said(kind: str, raw: str, subject: str = "") -> str:
    """The human words in an activity: no invite boilerplate, no signatures or quoted history."""
    from agent.emailtext import readable
    text = readable(raw or "", subject) if kind in ("emails", "meetings") else raw or ""
    text = _plain(_INVITE_NOISE.sub(" ", text), BRIEF_EXCERPT)
    text = re.sub(r"\[cid:[^\]]*\]", "", text).strip(" ·")
    return "" if _DIALER.search(text) else text


def brief_material(hub: HubSpot, externals: list[str], mailbox: str, first_label: int = 1) -> list[dict[str, Any]]:
    """The companies behind a meeting's outside attendees, as citable sources ("HubSpot n"):
    an account overview (owner, stage, last contact, who's known there), then what TAG people
    did with them lately: notes, calls, meeting recaps, emails. Emails to or from Dave are left
    out (the brief reads his own mailbox already); his staff's are the point."""
    from agent.research import FREEMAIL

    me = (mailbox or "").lower()
    companies: dict[str, str] = {}             # id -> name
    attendees: dict[str, dict[str, Any]] = {}  # email -> their contact record, if any
    for address in externals[:6]:
        email = address.lower()
        found = hub.hs.search("contacts", filters=[{"propertyName": "email", "operator": "EQ", "value": email}],
                              properties=hub.wanted_props("contacts", ["associatedcompanyid"]), limit=1).get("results", [])
        if found:
            attendees[email] = found[0]
            cid = (found[0].get("properties") or {}).get("associatedcompanyid")
            if cid and cid not in companies:
                companies[cid] = ""
        domain = email.rsplit("@", 1)[-1]
        if domain not in FREEMAIL and not any(domain in n for n in companies.values()):
            try:
                cid, name, _ = hub.company_id(domain)
                companies.setdefault(cid, name)
            except ValueError:
                pass
    items: list[dict[str, Any]] = []

    def add(item: dict[str, Any]) -> None:
        if len(items) < MAX_BRIEF_ITEMS:
            items.append({"label": f"HubSpot {first_label + len(items)}", **item})

    for cid in list(companies)[:2]:
        rec = hub.record({"object": "companies", "name": cid})
        name = rec.get("title") or companies[cid]
        lines = [f"{name} in TAG's HubSpot."]
        if rec.get("lifecycle_stage"):
            lines.append(f"Lifecycle stage: {rec['lifecycle_stage']}.")
        if rec.get("owner"):
            lines.append(f"Account owner at TAG: {rec['owner']}.")
        if rec.get("last_contacted"):
            lines.append(f"Last contacted: {rec['last_contacted']}.")
        people = (rec.get("contacts") or {}).get("rows") or []
        if people:
            lines.append("People known there: " + "; ".join(
                p["title"] + (f" ({p['sub'].split(' · ')[0]})" if p.get("sub") else "") for p in people[:6]) + ".")
        for email, contact in attendees.items():
            row = hub.row("contacts", contact)
            bits = [b for b in (row.get("lifecycle_stage"), row.get("owner") and f"owned by {row['owner']}",
                                row.get("last_contacted") and f"last contacted {row['last_contacted']}") if b]
            if bits:
                lines.append(f"{row['title']}: " + ", ".join(bits) + ".")
        counts = rec.get("in_hubspot") or {}
        lines.append("Logged: " + ", ".join(f"{v} {k}" for k, v in counts.items() if v and "people" not in k and "deals" not in k) + ".")
        add({"kind": "account", "title": name, "url": rec.get("url"), "text": " ".join(lines)})

        link = [{"propertyName": "associations.company", "operator": "EQ", "value": cid}]
        now_ms = str(int(datetime.now(timezone.utc).timestamp() * 1000))
        since = datetime.now(timezone.utc) - timedelta(days=BRIEF_LOOKBACK_DAYS)
        past = link + [{"propertyName": "hs_timestamp", "operator": "LTE", "value": now_ms},
                       {"propertyName": "hs_timestamp", "operator": "GTE", "value": str(int(since.timestamp() * 1000))}]
        activity, seen = [], set()
        dave_owner = None
        try:
            dave_owner = hub.owner_id("me")
        except ValueError:
            pass
        extras = {"emails": ["hs_email_direction", "hs_email_from_email", "hs_email_to_email", "hs_sequence_id", "hs_template_id"],
                  "meetings": ["hs_internal_meeting_notes"]}
        for kind, body, limit in (("notes", "hs_note_body", 3), ("calls", "hs_call_body", 3), ("meetings", "hs_meeting_body", 4),
                                  ("emails", "hs_email_text", 8)):
            for r in hub.hs.search(kind, filters=past, properties=hub.wanted_props(kind, [body, *extras.get(kind, [])]),
                                   sorts=[{"propertyName": "hs_timestamp", "direction": "DESCENDING"}], limit=limit).get("results", []):
                p = r.get("properties") or {}
                if kind == "emails":
                    if p.get("hs_sequence_id") or p.get("hs_template_id"):
                        continue  # sequence and template sends (RJ's "TAG - Connect" went to 1,830 people)
                    parties = f"{p.get('hs_email_from_email') or ''} {p.get('hs_email_to_email') or ''}".lower()
                    if me and me in parties:
                        continue  # Dave's own mail: the brief already reads it from Outlook
                if kind == "meetings":
                    recap = p.get("hs_internal_meeting_notes")      # where Fathom puts its summary
                    if recap:
                        text = _plain(re.sub(r"(?is)<h1>.*?</h1>|Generated by.*?</p>", " ", recap), BRIEF_EXCERPT * 2)
                    elif str(p.get("hubspot_owner_id")) == str(dave_owner):
                        continue  # Dave's own calendar, synced in: he knows his events
                    else:
                        text = _said(kind, p.get(body) or "")
                else:
                    text = _said(kind, p.get(body) or "", p.get("hs_email_subject") or "")
                if not text or (kind == "meetings" and len(text) < 40) or text[:120] in seen:
                    continue      # nothing written down (or just the invite, or a copy) says nothing new
                seen.add(text[:120])
                if kind == "emails" and (p.get("hs_email_from_email") or p.get("hs_email_to_email")):
                    # Who wrote it, so a reply isn't credited to the wrong person
                    text = f"From {p.get('hs_email_from_email') or 'unknown'} to {p.get('hs_email_to_email') or 'unknown'}: {text}"
                row = hub.row(kind, r)
                if row["title"].startswith("(meeting"):
                    row["title"] = "Meeting"
                activity.append((p.get("hs_timestamp") or "", {
                    "kind": kind[:-1], "title": row["title"] if kind != "notes" else f"Note by {row.get('owner') or 'TAG'}",
                    "when": row.get("activity_date") or "", "by": row.get("owner") or "",
                    "url": row.get("url"), "text": text}))
        for _, item in sorted(activity, key=lambda x: x[0], reverse=True):
            add(item)
    return items
