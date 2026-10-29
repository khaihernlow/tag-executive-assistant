"""New Autotask accounts (companies), for a prospect who isn't in Autotask yet.

Follows what TAG did with the 94 companies added in the last year: almost all
are a Lead (37) or a Customer (37), every one has a phone number (Autotask
requires it), most have an address, and territory and market segment are never
set. A new account waits on Dave's sign-off like any other Autotask record.
"""

from __future__ import annotations

import re
from typing import Any

from agent.actions import ActionKind, Actions, public_action
from agent.opportunities import Directory, dave_resource_id, web_link
from agent.tools import Tool
from llm.provider import ToolSpec

CREATE_KIND = "create_company"
# Autotask's companyType picklist; Lead is TAG's usual start for someone not yet a client
TYPES = {"lead": (2, "Lead"), "prospect": (3, "Prospect"), "customer": (1, "Customer")}
FIELDS = ("company_type", "owner_id", "phone", "web", "address1", "city", "state", "postal_code")


SUFFIXES = re.compile(r"\b(inc|llc|corp|corporation|co|company|ltd|pllc|pc|the)\b\.?", re.I)


def _core(name: str) -> str:
    """'Northwind Dental, Inc.' -> 'Northwind Dental': what to search Autotask for."""
    return re.sub(r"\s+", " ", re.sub(r"[,&.]", " ", SUFFIXES.sub("", name))).strip()


def _same_name(a: str, b: str) -> bool:
    plain = lambda s: re.sub(r"[^a-z0-9]", "", _core(s).lower())  # noqa: E731
    return plain(a) == plain(b)


# Words too common in company names to say two of them are the same business
GENERIC = {"solutions", "services", "group", "associates", "consulting", "partners", "systems", "technologies",
           "technology", "management", "international", "holdings", "enterprises", "center", "office", "school",
           "district", "county", "city", "town", "village", "dental", "medical", "health", "law", "firm", "bank"}


def _words(name: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", _core(name).lower()) if len(w) >= 4 and w not in GENERIC]


def _lookalikes(directory: Directory, name: str, email: str) -> list[dict[str, Any]]:
    """Companies that may be this one: its name without 'Inc.', the same email domain, or the same
    leading word, which is usually the brand ('Northwind' Dental), as a whole word: 'Zz' is inside
    'BuzzFeed' and 'Orthodontics' is half the practices in Albany."""
    core, words = _core(name), _words(name)
    found: dict[int, dict[str, Any]] = {c["company_id"]: c for c in directory.find_companies(core, email)}
    if words:
        for c in directory.find_companies(words[0]):
            if words[0] in _words(c["name"]):
                found.setdefault(c["company_id"], c)
    return list(found.values())


def propose_company(directory: Directory, actions: Actions, args: dict[str, Any]) -> dict[str, Any]:
    name = (args.get("name") or "").strip()
    if not name:
        raise ValueError("name is required")
    phone = (args.get("phone") or "").strip()
    if not phone:
        raise ValueError("Autotask needs a phone number for a new company. Find it in their email signature or "
                         "website, or ask Dave for it.")
    # Already there? By name, and by the domain of anyone's email, so 'Acme' isn't added twice as 'Acme Inc.'
    found = _lookalikes(directory, name, args.get("contact_email", ""))
    same = [c for c in found if _same_name(c["name"], name)]
    if same:
        c = same[0]
        raise ValueError(f"{c['name']} is already in Autotask (company_id {c['company_id']}"
                         f"{', inactive' if not c['active'] else ''}). Use it instead of adding another.")
    warnings = []
    if found:
        warnings.append("similar names already in Autotask: " + ", ".join(c["name"] for c in found[:3]))

    kind = (args.get("company_type") or "lead").lower()
    if kind not in TYPES:
        raise ValueError(f"company_type must be one of {', '.join(TYPES)}")
    owner = dave_resource_id()
    if args.get("owner_name"):
        owner = directory.resource_by_name(args["owner_name"]) or 0
        if not owner:
            raise ValueError(f"No single Autotask user matches '{args['owner_name']}'.")
    values = {"title": name, "company_type": kind, "owner_id": str(owner), "phone": phone,
              "web": (args.get("website") or "").strip(), "address1": (args.get("address") or "").strip(),
              "city": (args.get("city") or "").strip(), "state": (args.get("state") or "").strip(),
              "postal_code": (args.get("postal_code") or "").strip()}
    # A row's 4th item names the field Dave can change on the slip.
    rows = [["Type", TYPES[kind][1], "", "company_type"], ["Owner", directory.resource_name(owner), "", "owner_id"],
            ["Phone", phone, "", "phone"], ["Website", values["web"], "", "web"],
            ["Address", values["address1"], "", "address1"], ["City", values["city"], "", "city"],
            ["State", values["state"], "", "state"], ["Zip", values["postal_code"], "", "postal_code"]]
    edit = {"company_type": {"options": [[k, label] for k, (_, label) in TYPES.items()]},
            "owner_id": {"options": directory.owner_choices()},
            **{f: {"placeholder": p} for f, p in (("phone", "(518) 555-0100"), ("web", "example.com"),
                                                   ("address1", "Street"), ("city", ""), ("state", "NY"),
                                                   ("postal_code", ""))}}
    where = ", ".join(v for v in (values["city"], values["state"]) if v)
    summary = (f"New Autotask account “{name}” · {TYPES[kind][1]} · owner {directory.resource_name(owner)}"
               + (f" · {where}" if where else "") + "".join(f" · ⚠ {w}" for w in warnings))
    payload = {**values, "display": {"rows": rows, "edit": edit, "warnings": warnings}}
    return public_action(actions.propose(CREATE_KIND, summary, payload))


def company_body(payload: dict[str, Any]) -> dict[str, Any]:
    """The Autotask record, from the slip as Dave left it. Only values the slip offered are accepted."""
    edit = (payload.get("display") or {}).get("edit") or {}
    kind = payload.get("company_type") if payload.get("company_type") in TYPES else "lead"
    owners = {o[0] for o in edit.get("owner_id", {}).get("options", [])}
    owner = payload.get("owner_id") if payload.get("owner_id") in owners else str(dave_resource_id())
    body = {"companyName": payload["title"].strip(), "companyType": TYPES[kind][0], "ownerResourceID": int(owner),
            "phone": payload["phone"].strip(), "isActive": True}
    for field, key in (("webAddress", "web"), ("address1", "address1"), ("city", "city"), ("state", "state"),
                       ("postalCode", "postal_code")):
        if (payload.get(key) or "").strip():
            body[field] = payload[key].strip()
    return body


def company_kinds(at: Any) -> list[ActionKind]:
    def create(payload: dict[str, Any], action_id: str) -> dict[str, Any]:
        cid = at.create("Companies", company_body(payload))
        return {"company_id": cid, "web_link": web_link("company", cid)}

    return [ActionKind(CREATE_KIND, create, editable=("title", *FIELDS))]


def company_tools(directory: Directory, actions: Actions) -> list[Tool]:
    def pending_note(result: dict[str, Any]) -> dict[str, Any]:
        if result["status"] == "pending":
            result["note"] = ("Not saved yet. Dave sees an approval card; tell him it's ready for his approval. "
                              "Once it's saved, its company_id is in the result for a new opportunity.")
        return result

    return [Tool(spec=ToolSpec(
        name="create_company",
        description=("Propose a new account (company) in Autotask for someone not in it yet (saved once Dave approves). "
                     "Check find_company first. Take the phone, website and address from their email signature, "
                     "website or what Dave said; Autotask requires a phone. Type is lead unless they're already a client."),
        input_schema={"type": "object", "properties": {
            "name": {"type": "string", "description": "The company's name as they write it"},
            "phone": {"type": "string"}, "website": {"type": "string"},
            "address": {"type": "string", "description": "Street address"},
            "city": {"type": "string"}, "state": {"type": "string", "description": "Two-letter, e.g. NY"},
            "postal_code": {"type": "string"},
            "company_type": {"type": "string", "enum": list(TYPES)},
            "contact_email": {"type": "string", "description": "Someone's email there, to catch an existing account by domain"},
            "owner_name": {"type": "string", "description": "Account owner if not Dave"}},
            "required": ["name"]}),
        handler=lambda a: pending_note(propose_company(directory, actions, a)))]
