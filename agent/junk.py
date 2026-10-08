"""Junk sweep: what Maria does when spam slips past the filter, automatically.

Dave's mail passes through an upstream filter (Mailprotector) and Exchange
skips its own spam checks, so SCL/SPF/DKIM headers carry no signal here. The
decision is built from what does, in this order:

  1. Relationship (code): TAG staff and anyone Dave has emailed: never junk.
  2. Learned reputation (code): senders and domains from Dave's own history:
     his Junk folder means junk; mail Maria filed into a real folder means keep.
     Dave's taps (Undo, the slip) override anything learned.
  3. Content (fast model): only senders still unknown, shown examples of what
     Dave junks and keeps, returning junk / keep / unsure with a confidence.

Then:
  auto   known-junk senders, and junk the model is sure of (or bulk mail it
         calls junk): moved to Junk at once. Today lists them with Undo.
  ask    junk the model is less sure of: added to one rolling "Inbox clean-up"
         slip where each email can be unticked before approving.
  leave  everything else.

The worker runs it every cycle on mail that arrived since the last check, so
spam is gone within a few minutes of landing. Nothing is deleted: Junk Email
keeps everything and Undo moves it back.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from agent.actions import ActionKind, Actions, public_action
from agent.filing import filing_folders
from agent.people import internal_domain
from agent.research import FREEMAIL
from agent.tools import Tool
from llm.provider import ToolSpec

KIND = "move_to_junk"
MESSAGE_FIELDS = "id,subject,from,receivedDateTime,bodyPreview,internetMessageHeaders,parentFolderId"
BULK_HEADERS = ("list-unsubscribe", "x-campaign", "x-campaignid", "x-mailgun-tag", "x-sg-eid", "x-hs-cta-tracking")
CHECKPOINT = "junk_checkpoint"
LEARNED_AT = "junk_history_learned_at"
EXAMPLES = "junk_examples"
HISTORY_DAYS = 180
FIRST_RUN_DAYS = 3


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sender(message: dict[str, Any]) -> tuple[str, str]:
    email = (message.get("from") or {}).get("emailAddress") or {}
    return (email.get("address") or "").lower(), email.get("name") or ""


# ── relationship ─────────────────────────────────────────────────────────────

CORRESPONDENTS_TTL = 12 * 60 * 60  # who Dave writes to barely changes within a day
_correspondents: dict[str, tuple[float, set[str]]] = {}


def known_correspondents(graph: Any, days: int = 365) -> set[str]:
    """Everyone Dave has written to in the last year, plus his top contacts.

    Scanning a year of Sent Items takes ~20s, so the result is cached."""
    cached = _correspondents.get(graph.mailbox)
    if cached and time.monotonic() - cached[0] < CORRESPONDENTS_TTL:
        return cached[1]
    known = _scan_correspondents(graph, days)
    _correspondents[graph.mailbox] = (time.monotonic(), known)
    return known


def _scan_correspondents(graph: Any, days: int) -> set[str]:
    since = _iso(datetime.now(timezone.utc) - timedelta(days=days))
    known: set[str] = set()
    for m in graph.get_all(f"/users/{graph.mailbox}/mailFolders/sentitems/messages",
                           {"$select": "toRecipients,ccRecipients", "$filter": f"sentDateTime ge {since}",
                            "$top": 250}, limit=2000):
        for r in (m.get("toRecipients") or []) + (m.get("ccRecipients") or []):
            address = ((r.get("emailAddress") or {}).get("address") or "").lower()
            if address:
                known.add(address)
    for p in graph.get_all("/me/people", {"$top": 300, "$select": "scoredEmailAddresses"}, limit=300):
        for e in p.get("scoredEmailAddresses") or []:
            if e.get("address"):
                known.add(e["address"].lower())
    return known


def bulk_markers(headers: list[dict[str, str]]) -> list[str]:
    found = []
    for h in headers or []:
        name, value = h.get("name", "").lower(), (h.get("value") or "").lower()
        if name in BULK_HEADERS:
            found.append(name)
        elif name == "precedence" and value in ("bulk", "list", "junk"):
            found.append(f"precedence:{value}")
    return sorted(set(found))


# ── learning from Dave's history ─────────────────────────────────────────────

def folder_ids(graph: Any) -> dict[str, str]:
    ids = {}
    for name in ("inbox", "junkemail", "deleteditems", "sentitems", "drafts", "outbox"):
        try:
            ids[name] = graph.get(f"/users/{graph.mailbox}/mailFolders/{name}", {"$select": "id"})["id"]
        except Exception:  # noqa: BLE001 - a missing well-known folder just isn't used
            pass
    return ids


def learn_history(graph: Any, store: Any, days: int = HISTORY_DAYS) -> dict[str, int]:
    """Learn Dave's idea of junk from his own mailbox: senders in the Junk folder are
    junk; senders whose mail was filed into one of his folders are keep. Each is read
    directly (an all-folder scan drowns in Deleted Items). Keeps example subjects of
    each so the model learns from them too."""
    ids = folder_ids(graph)
    correspondents = known_correspondents(graph)
    since = _iso(datetime.now(timezone.utc) - timedelta(days=days))
    tally: dict[str, dict[str, int]] = {}
    examples: dict[str, list[str]] = {"junk": [], "keep": []}

    def count(messages: list[dict[str, Any]], verdict: str) -> None:
        for m in messages:
            address, _ = _sender(m)
            if not address or address == graph.mailbox.lower():
                continue
            tally.setdefault(address, {"junk": 0, "keep": 0})[verdict] += 1
            if address not in correspondents and len(examples[verdict]) < 15 and m.get("subject"):
                examples[verdict].append(f"{m['subject'][:90]} (from {address.split('@')[-1]})")

    query = {"$select": "from,subject", "$filter": f"receivedDateTime ge {since}", "$top": 500}
    if ids.get("junkemail"):
        count(graph.get_all(f"/users/{graph.mailbox}/mailFolders/junkemail/messages", query, limit=2000), "junk")
    for folder in filing_folders(graph):
        count(graph.get_all(f"/users/{graph.mailbox}/mailFolders/{folder}/messages",
                            {**query, "$top": 200}, limit=200), "keep")
    for address, counts in tally.items():
        if counts["junk"] > counts["keep"]:
            store.set_sender(address, "junk", "junk_folder")
        elif counts["keep"]:
            store.set_sender(address, "keep", "filed")
    store.set_state(EXAMPLES, json.dumps(examples))
    store.set_state(LEARNED_AT, _iso(datetime.now(timezone.utc)))
    return {"senders": len(tally), "junk_examples": len(examples["junk"]), "keep_examples": len(examples["keep"])}


def history_is_stale(store: Any, hours: int = 24) -> bool:
    learned = store.get_state(LEARNED_AT)
    return not learned or datetime.fromisoformat(learned.replace("Z", "+00:00")) < \
        datetime.now(timezone.utc) - timedelta(hours=hours)


def reputation(store: Any, address: str, correspondents: set[str], domain: str) -> tuple[str | None, str]:
    """('keep'|'junk'|None, why) from what code already knows about the sender."""
    if domain and address.endswith("@" + domain):
        return "keep", "TAG staff"
    if address in correspondents:
        return "keep", "someone you email"
    verdict = store.sender_verdicts([address]).get(address)
    if verdict:
        return verdict, "sender you've junked before" if verdict == "junk" else "sender you keep"
    sender_domain = address.split("@")[-1]
    if sender_domain not in FREEMAIL:
        counts = store.domain_counts(sender_domain)
        if counts.get("junk", 0) >= 2 and not counts.get("keep"):
            # Domain-wide guesses only ask: big platforms (hubspot.com, linkedin.com)
            # send both marketing Dave junks and real people he'd want.
            return "junk_domain", f"{sender_domain} is a domain you junk"
        if counts.get("keep") and not counts.get("junk"):
            return "keep", f"{sender_domain} is a domain you keep"
    return None, ""


# ── the model, for senders still unknown ─────────────────────────────────────

CLASSIFY = ToolSpec(
    name="classify_emails",
    description="Record a verdict for every email.",
    input_schema={"type": "object", "properties": {"verdicts": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "n": {"type": "integer", "description": "the email's number"},
            "verdict": {"type": "string", "enum": ["junk", "keep", "unsure"]},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "reason": {"type": "string", "description": "under 12 words"},
        },
        "required": ["n", "verdict", "confidence", "reason"],
    }}}, "required": ["verdicts"]},
)

CLASSIFY_PROMPT = """You triage email for Dave, CEO of TAG Solutions (an IT managed services provider).
These emails are from senders Dave has never written to. Decide for each:
- junk: unsolicited sales or marketing pitches, cold outreach selling services/leads/lists,
  newsletters and promotions he didn't ask for, scams and phishing.
- keep: anything that could be a real person or business Dave deals with: prospects or clients
  asking about IT services, partners, vendors he buys from (invoices, renewals, account notices),
  recruiting he is engaged in, security or system alerts, banks, government, personal mail.
- unsure: anything else.
Confidence high only when it's unmistakable. A prospect asking TAG for IT help is never junk.
{examples}"""


def _examples_text(store: Any) -> str:
    try:
        examples = json.loads(store.get_state(EXAMPLES) or "{}")
    except ValueError:
        return ""
    parts = []
    if examples.get("junk"):
        parts.append("Mail Dave has put in Junk before:\n" + "\n".join(f"- {e}" for e in examples["junk"]))
    if examples.get("keep"):
        parts.append("Mail from unknown senders Dave kept (filed):\n" + "\n".join(f"- {e}" for e in examples["keep"]))
    return "\n\n".join(parts)


def classify(llm: Any, candidates: list[dict[str, Any]], store: Any = None) -> dict[str, dict[str, str]]:
    """{message id: {verdict, confidence, reason}}. Missing or malformed verdicts count as unsure."""
    if not candidates:
        return {}
    listing = "\n\n".join(
        f"#{i}\nFrom: {c['from_name']} <{c['from']}>\nSubject: {c['subject']}\n"
        f"Bulk-mail headers: {', '.join(c['bulk']) or 'none'}\nPreview: {c['preview']}"
        for i, c in enumerate(candidates))
    system = CLASSIFY_PROMPT.format(examples=_examples_text(store) if store else "")
    response = llm.complete([{"role": "user", "content": listing}], system=system,
                            tools=[CLASSIFY], tool_choice=CLASSIFY.name, max_tokens=4096)
    verdicts: dict[str, dict[str, str]] = {}
    for call in response.tool_calls:
        for v in call.input.get("verdicts") or []:
            n = v.get("n")
            if isinstance(n, int) and 0 <= n < len(candidates) and v.get("verdict") in ("junk", "keep", "unsure"):
                verdicts[candidates[n]["id"]] = {"verdict": v["verdict"],
                                                 "confidence": v.get("confidence") or "low",
                                                 "reason": str(v.get("reason", ""))[:120]}
    return verdicts


# ── one pass ─────────────────────────────────────────────────────────────────

_owner: dict[str, str] = {}


def owner_name(graph: Any) -> str:
    """Dave's display name, lowercased (looked up once)."""
    if graph.mailbox not in _owner:
        try:
            _owner[graph.mailbox] = (graph.get("/me", {"$select": "displayName"}).get("displayName") or "").strip().lower()
        except Exception:  # noqa: BLE001 - without it, only the address checks apply
            _owner[graph.mailbox] = ""
    return _owner[graph.mailbox]


def triage(graph: Any, llm: Any, store: Any, messages: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Split messages into auto (move now), ask (slip) and unsure (left alone)."""
    correspondents = known_correspondents(graph)
    domain = internal_domain()
    owner = owner_name(graph)
    auto, ask, unknown = [], [], []
    for m in messages:
        address, name = _sender(m)
        if not address or address == graph.mailbox.lower():
            continue
        if owner and (name.strip().lower() == owner or address.split("@")[0].replace(".", "") == owner.replace(" ", "")):
            continue  # Dave writing from another of his addresses (e.g. personal iCloud)
        item = {"id": m["id"], "from": name or address, "address": address,
                "subject": m.get("subject") or "(no subject)", "folder": m.get("parentFolderId")}
        verdict, why = reputation(store, address, correspondents, domain)
        if verdict == "junk":
            auto.append({**item, "reason": why})
        elif verdict == "junk_domain":
            ask.append({**item, "reason": why})
        elif verdict is None:
            unknown.append({**item, "from_name": name, "from": address, "display": name or address,
                            "preview": (m.get("bodyPreview") or "")[:300],
                            "bulk": bulk_markers(m.get("internetMessageHeaders"))})
    unsure = []
    verdicts = classify(llm, unknown, store)
    for c in unknown:
        v = verdicts.get(c["id"], {"verdict": "unsure", "confidence": "low", "reason": "no verdict"})
        item = {"id": c["id"], "from": c["display"], "address": c["from"], "subject": c["subject"],
                "folder": c["folder"], "reason": v["reason"]}
        if v["verdict"] == "junk" and (v["confidence"] == "high" or c["bulk"]):
            auto.append(item)
        elif v["verdict"] == "junk":
            ask.append(item)
        elif v["verdict"] == "unsure":
            unsure.append(item)
    return {"auto": auto, "ask": ask, "unsure": unsure}


def _summary(items: list[dict[str, Any]], verb: str = "Move") -> str:
    senders = sorted({i["from"] for i in items})
    shown = ", ".join(senders[:4]) + (f" and {len(senders) - 4} more" if len(senders) > 4 else "")
    return f"{verb} {len(items)} email{'s' if len(items) != 1 else ''} to Junk · from {shown}"


def act(store: Any, actions: Actions, result: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Move the sure ones now; add the rest to the rolling clean-up slip."""
    out: dict[str, Any] = {"moved": 0, "asked": 0}
    if result["auto"]:
        action = actions.propose(KIND, _summary(result["auto"], "Moved"), {"items": result["auto"], "auto": True})
        done = actions.approve(action["id"], decided_by="auto: junk sweep")
        out["moved"] = (done.get("result") or {}).get("moved", 0)
        out["auto_action"] = public_action(done)
    if result["ask"]:
        pending = [a for a in store.list_actions(status="pending", limit=20)
                   if a["kind"] == KIND and not a["payload"].get("auto")]
        if pending:
            slip = pending[0]
            known = {i["id"] for i in slip["payload"]["items"]}
            items = slip["payload"]["items"] + [i for i in result["ask"] if i["id"] not in known]
            store.extend_pending_action(slip["id"], _summary(items), {**slip["payload"], "items": items})
            out["ask_action"] = public_action(store.get_action(slip["id"]))
        else:
            out["ask_action"] = public_action(actions.propose(KIND, _summary(result["ask"]), {"items": result["ask"]}))
        out["asked"] = len(result["ask"])
    return out


def sweep_new(graph: Any, llm: Any, store: Any, actions: Actions, now: datetime | None = None) -> dict[str, Any]:
    """Inbox mail that arrived since the last check (first run: the last few days)."""
    now = now or datetime.now(timezone.utc)
    since = store.get_state(CHECKPOINT) or _iso(now - timedelta(days=FIRST_RUN_DAYS))
    messages = graph.get_all(f"/users/{graph.mailbox}/mailFolders/inbox/messages", {
        "$select": MESSAGE_FIELDS, "$filter": f"receivedDateTime gt {since}",
        "$orderby": "receivedDateTime desc", "$top": 50}, limit=200)
    result = triage(graph, llm, store, messages)
    outcome = act(store, actions, result)
    newest = max([m.get("receivedDateTime", "") for m in messages] + [since])
    store.set_state(CHECKPOINT, newest)
    return {"checked": len(messages), **outcome, "unsure": result["unsure"]}


# ── the action: move, remember what was learned, allow undo ──────────────────

def junk_kind(graph: Any, store: Any = None) -> ActionKind:
    def execute(payload: dict[str, Any], action_id: str) -> dict[str, Any]:
        keep = {k for k in (payload.get("keep_ids") or "").split(",") if k}
        moved, failed, items = 0, [], []
        for item in payload["items"]:
            if item["id"] in keep:
                items.append({"id": item["id"], "kept": True})
                continue
            try:
                moved_msg = graph.post(f"/users/{graph.mailbox}/messages/{item['id']}/move",
                                       {"destinationId": "junkemail"})
                # Moving gives the message a new id; Undo needs it.
                items.append({"id": item["id"], "new_id": moved_msg.get("id"), "moved": True})
                moved += 1
            except Exception as e:  # noqa: BLE001 - one bad message shouldn't stop the rest
                failed.append({"subject": item["subject"], "error": str(e)[:200]})
                items.append({"id": item["id"], "failed": True})
        return {"moved": moved, "failed": failed, "items": items}

    def learn(payload: dict[str, Any], result: dict[str, Any]) -> None:
        if store is None:
            return
        keep = {k for k in (payload.get("keep_ids") or "").split(",") if k}
        source = "auto" if payload.get("auto") else "dave"
        for item in payload["items"]:
            store.set_sender(item["address"], "keep" if item["id"] in keep else "junk",
                             "dave" if item["id"] in keep else source)

    def declined(payload: dict[str, Any]) -> None:
        if store is not None:
            for item in payload["items"]:
                store.set_sender(item["address"], "keep", "dave")

    return ActionKind(KIND, execute, editable=("keep_ids",), on_done=learn, on_rejected=declined)


def undo(graph: Any, store: Any, action_id: str, message_id: str) -> dict[str, Any]:
    """Move one email back to where it came from and remember to keep that sender."""
    action = store.get_action(action_id)
    if action is None or action["kind"] != KIND or action["status"] != "executed":
        raise ValueError("Nothing to undo there.")
    item = next((i for i in action["payload"]["items"] if i["id"] == message_id), None)
    record = next((r for r in (action["result"] or {}).get("items", []) if r["id"] == message_id), None)
    if item is None or record is None or not record.get("moved") or record.get("undone"):
        raise ValueError("That email wasn't moved, or was already put back.")
    graph.post(f"/users/{graph.mailbox}/messages/{record['new_id']}/move",
               {"destinationId": item.get("folder") or "inbox"})
    record["undone"] = True
    store.update_action_result(action_id, action["result"])
    store.set_sender(item["address"], "keep", "undo")
    return {"undone": message_id}


def moved_today(store: Any) -> list[dict[str, Any]]:
    """Everything moved to Junk in the last 24 hours, newest first, for Today (with Undo)."""
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    out = []
    for action in store.actions_since(KIND, since):
        if action["status"] != "executed":
            continue
        records = {r["id"]: r for r in (action["result"] or {}).get("items", [])}
        for item in action["payload"]["items"]:
            r = records.get(item["id"], {})
            if r.get("moved"):
                out.append({"action_id": action["id"], "id": item["id"], "from": item["from"],
                            "subject": item["subject"], "reason": item.get("reason", ""),
                            "auto": bool(action["payload"].get("auto")), "undone": bool(r.get("undone"))})
    return out


# ── chat tool: "any junk?" ───────────────────────────────────────────────────

def junk_tools(graph: Any, llm: Any, actions: Actions, store: Any = None) -> list[Tool]:
    def run(args: dict[str, Any]) -> dict[str, Any]:
        since = _iso(datetime.now(timezone.utc) - timedelta(days=int(args.get("days", 3))))
        messages = graph.get_all(f"/users/{graph.mailbox}/mailFolders/inbox/messages", {
            "$select": MESSAGE_FIELDS, "$filter": f"receivedDateTime ge {since}",
            "$orderby": "receivedDateTime desc", "$top": 50}, limit=200)
        result = triage(graph, llm, store, messages)
        outcome = act(store, actions, result)
        out: dict[str, Any] = {
            "checked": len(messages),
            "moved_automatically": outcome["moved"],
            "awaiting_approval": outcome["asked"],
            "unsure": [{k: i[k] for k in ("from", "subject", "reason")} for i in result["unsure"]],
        }
        if "ask_action" in outcome:
            out["proposal"] = outcome["ask_action"]
        out["note"] = ("Sure junk was moved already (Dave can undo on Today); less certain ones are on the "
                       "clean-up slip. Mention the unsure ones briefly." if outcome["moved"] or outcome["asked"]
                       else "No junk found.")
        return out

    return [Tool(
        spec=ToolSpec(
            name="sweep_junk",
            description=(
                "Check Dave's inbox for spam/junk that got past the filter (like Maria does). Clear junk is "
                "moved at once (undoable); less certain junk goes on an approval slip. Senders Dave emails "
                "or keeps are never touched. This also runs automatically every few minutes."
            ),
            input_schema={"type": "object", "properties": {
                "days": {"type": "integer", "description": "How far back to check, default 3"}}},
        ),
        handler=run,
    )]
