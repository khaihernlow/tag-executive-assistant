"""Folder filing: what Maria does to keep Dave's Inbox down to a dozen emails.

Dave's real filing system is ~45 folders under the Inbox (Quotes, Receipts/
Financial, HR/<person>, Boards/..., Legal, ...). Filed mail there is almost
all read: mail is filed after Dave has seen it, not on arrival.

  learn   once a day, code reads those folders and builds sender -> folder:
          a sender is learned when 3+ of their emails went to one folder, 90%+
          of their filed mail and 60%+ of all their mail (deleted and archived
          counted). TAG staff are never learned: their mail is about everything.
          (No model: it's counting.)
  file    each worker cycle, Inbox mail Dave has READ (1h+ old, not flagged,
          not part of a meeting request still in progress):
            - from a learned sender: that folder (free, no model);
            - otherwise (unless it's addressed to Dave and he hasn't replied yet,
              which means it may still need him) the fast model reads it, like
              Maria does, and picks one
              of Dave's folders using each folder's recent example subjects,
              or leaves it. Filed only when the model is sure AND that sender
              (or their company) was filed there before: on emails Maria had
              already filed, that was right 21 times in 22. Each email is judged
              once; anything else stays in the Inbox. Unread mail is never touched.
          Today lists everything filed, with Undo.
  rules   for high-volume automated senders whose mail goes unread in their
          folder (a newsletter Dave never opens), a real Outlook rule is
          suggested on a sign-off slip, so it's filed on arrival even when this
          app isn't running. Existing rules are respected.

Not here: Archive (mostly Mailprotector's own routing), action folders whose
names start with "!" (e.g. "! Dave", Maria's "needs Dave" pile: that's a
judgment, not a sender), and system folders.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from agent.actions import ActionKind, Actions, public_action
from agent.people import internal_domain
from agent.research import FREEMAIL
from llm.provider import ToolSpec

FILE_KIND = "file_email"
RULE_KIND = "create_rule"
LEARNED_AT = "filing_learned_at"
DECLINED_RULES = "filing_declined_rules"
MIN_COUNT, MIN_SHARE = 3, 0.9
MIN_OVERALL = 0.6  # of all their mail (filed + deleted + archived), at least this much went to the folder
RULE_MIN_COUNT, RULE_MIN_UNREAD = 10, 0.8
READ_FOR = timedelta(hours=1)
PROFILES = "filing_profiles"
PROFILE_EXAMPLES = 4
BATCH = 20
SYSTEM_FOLDERS = {"archive", "conversation history", "rss feeds", "snoozed", "sync issues", "clutter",
                  "outbox", "junk email", "deleted items", "sent items", "drafts", "scheduled"}


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sender(message: dict[str, Any]) -> tuple[str, str]:
    email = (message.get("from") or {}).get("emailAddress") or {}
    return (email.get("address") or "").lower(), email.get("name") or ""


def filing_folders(graph: Any, max_depth: int = 3) -> dict[str, str]:
    """{folder id: path} for Dave's own folders: everything under the Inbox plus his
    top-level folders, minus system/routing folders and "!" action folders."""
    found: dict[str, str] = {}
    # System folders by id as well as name: names can be localized or renamed.
    system_ids = set()
    for well_known in ("junkemail", "deleteditems", "sentitems", "drafts", "outbox", "archive",
                       "conversationhistory", "syncissues", "scheduled"):
        try:
            system_ids.add(graph.get(f"/users/{graph.mailbox}/mailFolders/{well_known}", {"$select": "id"})["id"])
        except Exception:  # noqa: BLE001 - not every mailbox has every well-known folder
            pass

    def walk(folder_id: str, prefix: str, depth: int) -> None:
        for child in graph.get_all(f"/users/{graph.mailbox}/mailFolders/{folder_id}/childFolders",
                                   {"$select": "id,displayName,childFolderCount", "$top": 100}, limit=500):
            name = child.get("displayName") or ""
            if child["id"] in system_ids or name.lower() in SYSTEM_FOLDERS or name.startswith("!"):
                continue
            path = f"{prefix}/{name}"
            found[child["id"]] = path
            if child.get("childFolderCount") and depth < max_depth:
                walk(child["id"], path, depth + 1)

    inbox = graph.get(f"/users/{graph.mailbox}/mailFolders/inbox", {"$select": "id"})["id"]
    walk(inbox, "Inbox", 1)
    for top in graph.get_all(f"/users/{graph.mailbox}/mailFolders",
                             {"$select": "id,displayName,childFolderCount", "$top": 100}, limit=200):
        name = top.get("displayName") or ""
        if top["id"] in system_ids | {inbox} or name.lower() in SYSTEM_FOLDERS or name.startswith("!"):
            continue
        found[top["id"]] = name
        if top.get("childFolderCount"):
            walk(top["id"], name, 2)
    return found


def _count_from(graph: Any, folder: str, address: str, since: str) -> int:
    """How many of a sender's emails ended up in a well-known folder (deleted, archive)."""
    try:
        return len(graph.get_all(f"/users/{graph.mailbox}/mailFolders/{folder}/messages", {
            "$select": "id", "$filter": f"from/emailAddress/address eq '{address.replace(chr(39), chr(39) * 2)}' "
                                        f"and receivedDateTime ge {since}", "$top": 200}, limit=1000))
    except Exception:  # noqa: BLE001 - unknown means "don't assume it's mostly filed"
        return 10**6


def learn_filing(graph: Any, store: Any, days: int = 365) -> dict[str, int]:
    """Count where each sender's mail was filed; keep the clear patterns."""
    since = _iso(datetime.now(timezone.utc) - timedelta(days=days))
    per_sender: dict[str, dict[str, list[int]]] = {}  # address -> folder id -> [count, unread]
    folders = filing_folders(graph)
    profiles = []
    for folder_id in folders:
        messages = graph.get_all(f"/users/{graph.mailbox}/mailFolders/{folder_id}/messages",
                                 {"$select": "from,isRead,subject", "$filter": f"receivedDateTime ge {since}",
                                  "$orderby": "receivedDateTime desc", "$top": 250}, limit=500)
        if messages:
            # What this folder means to Dave, for the model: a few recent examples.
            examples: list[str] = []
            for m in messages:
                example = f"{m.get('subject', '')[:80]} (from {_sender(m)[0].split('@')[-1]})"
                if m.get("subject") and example not in examples:
                    examples.append(example)  # distinct examples only: repeats teach nothing
                if len(examples) == PROFILE_EXAMPLES:
                    break
            # Who has been filed here: the evidence a model pick must agree with.
            senders = {_sender(m)[0] for m in messages if _sender(m)[0]}
            domains = {a.split("@")[-1] for a in senders} - FREEMAIL
            profiles.append({"id": folder_id, "path": folders[folder_id], "count": len(messages), "examples": examples,
                             "senders": sorted(senders | domains)[:400]})
        for m in messages:
            address, _ = _sender(m)
            if not address or address == graph.mailbox.lower():
                continue
            tally = per_sender.setdefault(address, {}).setdefault(folder_id, [0, 0])
            tally[0] += 1
            tally[1] += 0 if m.get("isRead") else 1
    domain = internal_domain()
    store.clear_learned_filing()
    learned = 0
    for address, by_folder in per_sender.items():
        # Anyone whose mail Dave files is someone he keeps: never junk.
        store.set_sender(address, "keep", "filed")
        if domain and address.endswith("@" + domain):
            continue  # staff email about everything: where it goes depends on content, not sender
        folder_id, (count, unread) = max(by_folder.items(), key=lambda kv: kv[1][0])
        total = sum(c for c, _ in by_folder.values())
        if count < MIN_COUNT or count / total < MIN_SHARE:
            continue
        # The folder must hold most of their mail overall, not just most of the filed part:
        # 7 quotes filed out of 200 emails mostly deleted is not "file everything here".
        elsewhere = _count_from(graph, "deleteditems", address, since) + _count_from(graph, "archive", address, since)
        if count / (count + elsewhere) < MIN_OVERALL:
            continue
        store.set_filing(address, folder_id, folders[folder_id], count, round(count / total, 3), round(unread / count, 3))
        learned += 1
    store.set_state(PROFILES, json.dumps(profiles))
    store.set_state(LEARNED_AT, _iso(datetime.now(timezone.utc)))
    return {"folders": len(folders), "senders": len(per_sender), "learned": learned}


def learning_is_stale(store: Any, hours: int = 24) -> bool:
    learned = store.get_state(LEARNED_AT)
    return not learned or datetime.fromisoformat(learned.replace("Z", "+00:00")) < \
        datetime.now(timezone.utc) - timedelta(hours=hours)


# ── filing read mail ─────────────────────────────────────────────────────────

def read_inbox(graph: Any, store: Any, now: datetime | None = None) -> list[dict[str, Any]]:
    """Inbox mail Dave has read, 1h+ old, minus flagged mail and meeting requests still in progress."""
    cutoff = _iso((now or datetime.now(timezone.utc)) - READ_FOR)
    inbox = graph.get_all(f"/users/{graph.mailbox}/mailFolders/inbox/messages", {
        "$select": "id,subject,from,receivedDateTime,isRead,bodyPreview,flag,conversationId,toRecipients",
        "$filter": f"isRead eq true and receivedDateTime le {cutoff}", "$top": 100}, limit=300)
    active = {r["thread_id"] for r in store.requests_in(("new", "waiting"))} if hasattr(store, "requests_in") else set()
    return [m for m in inbox
            if (m.get("flag") or {}).get("flagStatus") != "flagged" and m.get("conversationId") not in active]


def ready_to_file(graph: Any, store: Any, now: datetime | None = None,
                  inbox: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Read Inbox mail from senders with a learned folder (no model needed)."""
    inbox = read_inbox(graph, store, now) if inbox is None else inbox
    rules = store.filing_for([_sender(m)[0] for m in inbox])
    items = []
    for m in inbox:
        address, name = _sender(m)
        rule = rules.get(address)
        if rule:
            items.append({"id": m["id"], "from": name or address, "address": address,
                          "subject": m.get("subject") or "(no subject)",
                          "folder_id": rule["folder_id"], "folder": rule["folder_path"], "by": "sender"})
    return items


CHOOSE_FOLDER = ToolSpec(
    name="choose_folders",
    description="Record where each email should be filed.",
    input_schema={"type": "object", "properties": {"emails": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "n": {"type": "integer", "description": "the email's number"},
            "folder": {"type": "integer", "description": "the folder's number, or 0 to leave it in the Inbox"},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "reason": {"type": "string", "description": "under 10 words"},
        },
        "required": ["n", "folder", "confidence"],
    }}}, "required": ["emails"]},
)

CHOOSE_PROMPT = """You file email for Dave, CEO of TAG Solutions, the way his assistant Maria does: after he has
read it, each email goes into the folder where it belongs, or stays in the Inbox.
Dave's folders (with recent examples of what's in each):
{folders}

Rules:
- Pick a folder only when the email clearly belongs with that folder's examples. Otherwise choose 0.
- Choose 0 for anything that may still need Dave's action, personal mail, and conversations in progress.
- Confidence high only when it's unmistakable."""


def _profiles_text(profiles: list[dict[str, Any]]) -> str:
    return "\n".join(f"{i + 1}. {p['path']}: " + "; ".join(p["examples"]) for i, p in enumerate(profiles))


def choose_folders(llm: Any, profiles: list[dict[str, Any]], messages: list[dict[str, Any]]) -> dict[str, dict]:
    """{message id: {folder (profile) | None, confidence, reason}} from one batched model call."""
    if not messages or not profiles:
        return {}
    listing = "\n\n".join(
        f"#{i}\nFrom: {_sender(m)[1]} <{_sender(m)[0]}>\nSubject: {m.get('subject', '')}\n"
        f"Preview: {(m.get('bodyPreview') or '')[:300]}" for i, m in enumerate(messages))
    response = llm.complete([{"role": "user", "content": listing}],
                            system=CHOOSE_PROMPT.format(folders=_profiles_text(profiles)),
                            tools=[CHOOSE_FOLDER], tool_choice=CHOOSE_FOLDER.name, max_tokens=3000)
    out: dict[str, dict] = {}
    for call in response.tool_calls:
        for item in call.input.get("emails") or []:
            n, folder = item.get("n"), item.get("folder")
            if not (isinstance(n, int) and 0 <= n < len(messages)):
                continue
            chosen = profiles[folder - 1] if isinstance(folder, int) and 1 <= folder <= len(profiles) else None
            out[messages[n]["id"]] = {"folder": chosen, "confidence": item.get("confidence") or "low",
                                      "reason": str(item.get("reason") or "")[:100]}
    return out


def awaiting_dave(graph: Any, message: dict[str, Any]) -> bool:
    """Addressed TO Dave (not just copied) and he hasn't replied in the thread since: it
    may still need him, so it stays in the Inbox. ("Attached are my targets. Let's
    discuss Monday" was filed by content alone; Maria would have left it.)"""
    to = {((r.get("emailAddress") or {}).get("address") or "").lower() for r in message.get("toRecipients") or []}
    if graph.mailbox.lower() not in to:
        return False
    replies = graph.get_all(f"/users/{graph.mailbox}/mailFolders/sentitems/messages", {
        "$select": "id,sentDateTime",
        "$filter": f"conversationId eq '{message.get('conversationId')}' and sentDateTime gt {message['receivedDateTime']}",
        "$top": 5}, limit=5)
    return not replies


def has_history(folder: dict[str, Any], address: str) -> bool:
    """Has this sender, or someone at their company (not a free-mail domain), been filed in this folder?"""
    known = set(folder.get("senders") or [])
    domain = address.split("@")[-1]
    return address in known or (domain not in FREEMAIL and domain in known)


def content_to_file(graph: Any, store: Any, llm: Any, inbox: list[dict[str, Any]],
                    skip: set[str]) -> list[dict[str, Any]]:
    """Ask the model about read mail no sender rule covered; each email is judged once."""
    profiles = json.loads(store.get_state(PROFILES) or "[]")
    pending = [m for m in inbox if m["id"] not in skip]
    pending = [m for m in pending if m["id"] not in store.filing_seen_ids([p["id"] for p in pending])]
    # Not marked as judged: once Dave replies, it becomes eligible again.
    pending = [m for m in pending if not awaiting_dave(graph, m)][:BATCH]
    if not pending or not profiles:
        return []
    verdicts = choose_folders(llm, profiles, pending)
    store.mark_filing_seen([m["id"] for m in pending if m["id"] in verdicts])
    items = []
    for m in pending:
        v = verdicts.get(m["id"])
        address, name = _sender(m)
        # File only when the model is sure AND this sender (or their company) has been filed
        # there before. Tested on emails Maria had filed: 95% right, vs 83% on confidence alone.
        if v and v["folder"] and v["confidence"] == "high" and has_history(v["folder"], address):
            items.append({"id": m["id"], "from": name or address, "address": address,
                          "subject": m.get("subject") or "(no subject)", "folder_id": v["folder"]["id"],
                          "folder": v["folder"]["path"], "by": "content", "reason": v["reason"]})
    return items


def file_kind(graph: Any, store: Any) -> ActionKind:
    def execute(payload: dict[str, Any], action_id: str) -> dict[str, Any]:
        moved, records = 0, []
        for item in payload["items"]:
            try:
                result = graph.post(f"/users/{graph.mailbox}/messages/{item['id']}/move",
                                    {"destinationId": item["folder_id"]})
                records.append({"id": item["id"], "new_id": result.get("id"), "moved": True})
                moved += 1
            except Exception as e:  # noqa: BLE001 - e.g. Dave moved it himself in the meantime
                records.append({"id": item["id"], "failed": str(e)[:200]})
        return {"moved": moved, "items": records}

    return ActionKind(FILE_KIND, execute)


def file_read_mail(graph: Any, store: Any, actions: Actions, now: datetime | None = None, llm: Any = None) -> int:
    inbox = read_inbox(graph, store, now)
    items = ready_to_file(graph, store, now, inbox=inbox)
    if llm is not None:
        items += content_to_file(graph, store, llm, inbox, skip={i["id"] for i in items})
    if not items:
        return 0
    folders = sorted({i["folder"].split("/")[-1] for i in items})
    summary = f"Filed {len(items)} read email{'s' if len(items) != 1 else ''} into {', '.join(folders[:4])}"
    action = actions.propose(FILE_KIND, summary, {"items": items, "auto": True})
    done = actions.approve(action["id"], decided_by="auto: filing")
    return (done.get("result") or {}).get("moved", 0)


def undo_filing(graph: Any, store: Any, action_id: str, message_id: str) -> dict[str, Any]:
    """Back to the Inbox, and stop filing that sender automatically."""
    action = store.get_action(action_id)
    if action is None or action["kind"] != FILE_KIND or action["status"] != "executed":
        raise ValueError("Nothing to undo there.")
    item = next((i for i in action["payload"]["items"] if i["id"] == message_id), None)
    record = next((r for r in (action["result"] or {}).get("items", []) if r["id"] == message_id), None)
    if item is None or record is None or not record.get("moved") or record.get("undone"):
        raise ValueError("That email wasn't filed, or was already put back.")
    graph.post(f"/users/{graph.mailbox}/messages/{record['new_id']}/move", {"destinationId": "inbox"})
    record["undone"] = True
    store.update_action_result(action_id, action["result"])
    if item.get("by", "sender") == "sender":
        store.disable_filing(item["address"])
    store.mark_filing_seen([record["new_id"]])  # back in the Inbox with a new id: don't refile it
    return {"undone": message_id}


def filed_today(store: Any) -> list[dict[str, Any]]:
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    out = []
    for action in store.actions_since(FILE_KIND, since):
        if action["status"] != "executed":
            continue
        records = {r["id"]: r for r in (action["result"] or {}).get("items", [])}
        for item in action["payload"]["items"]:
            r = records.get(item["id"], {})
            if r.get("moved"):
                out.append({"action_id": action["id"], "id": item["id"], "from": item["from"],
                            "subject": item["subject"], "folder": item["folder"], "reason": item.get("reason", ""),
                            "undone": bool(r.get("undone"))})
    return out


# ── Outlook rule suggestions ─────────────────────────────────────────────────

def existing_rule_senders(graph: Any) -> set[str]:
    senders: set[str] = set()
    for rule in graph.get_all(f"/users/{graph.mailbox}/mailFolders/inbox/messageRules", {}, limit=200):
        for a in (rule.get("conditions") or {}).get("fromAddresses") or []:
            address = ((a.get("emailAddress") or {}).get("address") or "").lower()
            if address:
                senders.add(address)
    return senders


def rule_kind(graph: Any, store: Any) -> ActionKind:
    def execute(payload: dict[str, Any], action_id: str) -> dict[str, Any]:
        rule = graph.post(f"/users/{graph.mailbox}/mailFolders/inbox/messageRules", {
            "displayName": f"Assistant: {payload['address']} → {payload['folder']}",
            "sequence": 50,
            "isEnabled": True,
            "conditions": {"fromAddresses": [{"emailAddress": {"address": payload["address"]}}]},
            "actions": {"moveToFolder": payload["folder_id"], "stopProcessingRules": True},
        })
        return {"rule_id": rule.get("id")}

    def declined(payload: dict[str, Any]) -> None:
        seen = set(json.loads(store.get_state(DECLINED_RULES) or "[]"))
        seen.add(payload["address"])
        store.set_state(DECLINED_RULES, json.dumps(sorted(seen)))

    return ActionKind(RULE_KIND, execute, on_rejected=declined)


def suggest_rules(graph: Any, store: Any, actions: Actions, limit: int = 3) -> int:
    """Propose Outlook rules for automated senders Dave files but doesn't read."""
    pending = {a["payload"].get("address") for a in store.list_actions(status="pending", limit=50)
               if a["kind"] == RULE_KIND}
    declined = set(json.loads(store.get_state(DECLINED_RULES) or "[]"))
    covered = existing_rule_senders(graph)
    proposed = 0
    for rule in store.filing_rules(min_count=RULE_MIN_COUNT):
        if proposed + len(pending) >= limit:
            break
        address = rule["address"]
        if rule["unread_share"] < RULE_MIN_UNREAD or address in covered | declined | pending:
            continue
        actions.propose(RULE_KIND,
                        f"Outlook rule: file mail from {address} into {rule['folder_path']} as it arrives "
                        f"({rule['filed_count']} filed there, mostly unread)",
                        {"address": address, "folder_id": rule["folder_id"], "folder": rule["folder_path"]})
        proposed += 1
    return proposed
