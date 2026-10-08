import pytest

from agent.actions import Actions
from agent.filing import (
    file_kind, file_read_mail, filed_today, filing_folders, learn_filing, ready_to_file, rule_kind, suggest_rules,
    undo_filing,
)
from store.db import Store

DAVE = "dave@tag.example"
WELL_KNOWN = {"inbox": "inbox", "junkemail": "junk", "deleteditems": "deleted", "sentitems": "sent",
              "archive": "archive"}
CHILDREN = {
    "inbox": [{"id": "quotes", "displayName": "Quotes", "childFolderCount": 1},
              {"id": "bang", "displayName": "! Dave", "childFolderCount": 0},
              {"id": "hr", "displayName": "HR", "childFolderCount": 1}],
    "quotes": [{"id": "biz", "displayName": "Businessreview", "childFolderCount": 0}],
    "hr": [{"id": "hr-rj", "displayName": "RJ", "childFolderCount": 0}],
}
TOP = [{"id": "inbox", "displayName": "Inbox"}, {"id": "archive", "displayName": "Archive"},
       {"id": "junk", "displayName": "Junk Email"}, {"id": "board", "displayName": "Board", "childFolderCount": 0},
       {"id": "rss", "displayName": "RSS Feeds"}]


def mail(sender, read=True, mid=None):
    return {"id": mid or f"m-{sender}", "subject": f"From {sender}", "isRead": read,
            "receivedDateTime": "2026-10-08T10:00:00Z", "from": {"emailAddress": {"name": "", "address": sender}}}


class FakeGraph:
    mailbox = DAVE

    def __init__(self, folder_mail=None, inbox=(), rules=()):
        self.folder_mail = folder_mail or {}
        self.inbox = list(inbox)
        self.rules = list(rules)
        self.posts, self.inbox_filters = [], []

    def get(self, path, params=None, headers=None):
        name = path.rsplit("/", 1)[-1]
        if name not in WELL_KNOWN:
            raise RuntimeError("404")
        return {"id": WELL_KNOWN[name]}

    def get_all(self, path, params=None, limit=500, headers=None):
        tail = path.split("/mailFolders")[-1]
        if tail == "":
            return TOP
        if tail.endswith("/childFolders"):
            return CHILDREN.get(tail.split("/")[1], [])
        if tail == "/inbox/messageRules":
            return self.rules
        if tail == "/inbox/messages":
            self.inbox_filters.append(params["$filter"])
            return [m for m in self.inbox if m["isRead"]]
        if tail.endswith("/messages"):
            folder = tail.split("/")[1]
            if folder in ("deleteditems", "archive"):
                sender = params["$filter"].split("'")[1]
                return [m for m in self.folder_mail.get(folder, []) if m["from"]["emailAddress"]["address"] == sender]
            return self.folder_mail.get(folder, [])
        return []

    def post(self, path, body, headers=None):
        self.posts.append((path, body))
        return {"id": "new-" + path.split("/messages/")[1].split("/")[0]} if "/messages/" in path else {"id": "rule-1"}


def test_folder_walk_covers_inbox_subfolders_and_skips_system_archive_and_action_folders():
    assert filing_folders(FakeGraph()) == {
        "quotes": "Inbox/Quotes", "biz": "Inbox/Quotes/Businessreview", "hr": "Inbox/HR", "hr-rj": "Inbox/HR/RJ",
        "board": "Board"}


def test_learning_keeps_only_clear_patterns_and_marks_filed_senders_as_keep(monkeypatch):
    monkeypatch.setenv("GRAPH_MAILBOX", DAVE)
    graph = FakeGraph(folder_mail={
        "hr-rj": [mail("rj@tag.example")] * 3,                # staff: never learned
        "biz": [mail("publisher@news.example", read=False)] * 12,
        "quotes": [mail("joe@cpa.example")] * 3 + [mail("vendor@x.example")] * 2 + [mail("quotes@big.example")] * 4,
        "board": [mail("joe@cpa.example")],                  # 3 of 4 = 75%: not consistent enough
        "deleteditems": [mail("quotes@big.example")] * 10,   # mostly deleted: filing 4 doesn't mean "file all"
    })
    store = Store(":memory:")
    stats = learn_filing(graph, store)
    rules = {r["address"]: r for r in store.filing_rules()}
    assert set(rules) == {"publisher@news.example"}
    assert rules["publisher@news.example"]["folder_path"] == "Inbox/Quotes/Businessreview"
    assert rules["publisher@news.example"]["unread_share"] == 1.0
    assert store.sender_verdicts(["vendor@x.example", "rj@tag.example"]) == {
        "vendor@x.example": "keep", "rj@tag.example": "keep"}  # helps the junk sweep
    assert stats == {"folders": 5, "senders": 5, "learned": 1}

    # Relearning starts fresh but remembers senders Dave switched off with Undo.
    store.set_filing("stale@old.example", "biz", "Inbox/Quotes/Businessreview", 9, 1.0, 0.0)
    store.disable_filing("publisher@news.example")
    learn_filing(graph, store)
    assert [r["address"] for r in store.filing_rules()] == []
    assert store._execute("SELECT disabled FROM filing WHERE address = 'publisher@news.example'").fetchone()[0] == 1


def learned(store):
    store.set_filing("rj@tag.example", "hr-rj", "Inbox/HR/RJ", 5, 1.0, 0.0)  # stored directly to test filing


def test_only_read_mail_older_than_an_hour_from_learned_senders_is_filed():
    graph = FakeGraph(inbox=[mail("rj@tag.example", mid="read"), mail("rj@tag.example", read=False, mid="unread"),
                             mail("stranger@x.example", mid="other")])
    store = Store(":memory:")
    learned(store)
    items = ready_to_file(graph, store)
    assert [i["id"] for i in items] == ["read"]
    assert "isRead eq true and receivedDateTime le" in graph.inbox_filters[0]


def test_filing_runs_automatically_and_undo_puts_it_back_and_stops_that_sender():
    graph = FakeGraph(inbox=[mail("rj@tag.example", mid="read")])
    store = Store(":memory:")
    actions = Actions(store, [file_kind(graph, store)], auto=set())
    learned(store)

    assert file_read_mail(graph, store, actions) == 1
    assert graph.posts[0] == (f"/users/{DAVE}/messages/read/move", {"destinationId": "hr-rj"})
    [today] = filed_today(store)
    assert today["folder"] == "Inbox/HR/RJ" and not today["undone"]

    undo_filing(graph, store, today["action_id"], "read")
    assert graph.posts[-1] == (f"/users/{DAVE}/messages/new-read/move", {"destinationId": "inbox"})
    assert filed_today(store)[0]["undone"] is True
    assert ready_to_file(graph, store) == []   # sender no longer filed automatically
    with pytest.raises(ValueError):
        undo_filing(graph, store, today["action_id"], "read")


def test_rule_suggestions_only_for_unread_high_volume_senders_not_already_covered_or_declined():
    graph = FakeGraph(rules=[{"conditions": {"fromAddresses": [{"emailAddress": {"address": "Covered@news.example"}}]}}])
    store = Store(":memory:")
    actions = Actions(store, [rule_kind(graph, store)], auto=set())
    store.set_filing("publisher@news.example", "biz", "Inbox/Quotes/Businessreview", 40, 1.0, 0.95)
    store.set_filing("covered@news.example", "biz", "Inbox/Quotes/Businessreview", 40, 1.0, 0.95)
    store.set_filing("joe@cpa.example", "quotes", "Inbox/Quotes", 40, 1.0, 0.1)      # Dave reads these
    store.set_filing("rare@x.example", "biz", "Inbox/Quotes/Businessreview", 4, 1.0, 1.0)

    assert suggest_rules(graph, store, actions) == 1
    [slip] = store.list_actions(status="pending")
    assert slip["payload"]["address"] == "publisher@news.example"
    assert suggest_rules(graph, store, actions) == 0          # already pending: not proposed twice

    done = actions.approve(slip["id"], "dave")
    path, body = graph.posts[-1]
    assert path.endswith("/mailFolders/inbox/messageRules") and done["status"] == "executed"
    assert body["actions"] == {"moveToFolder": "biz", "stopProcessingRules": True}
    assert body["conditions"]["fromAddresses"][0]["emailAddress"]["address"] == "publisher@news.example"


def test_declined_rule_is_not_suggested_again():
    graph, store = FakeGraph(), Store(":memory:")
    actions = Actions(store, [rule_kind(graph, store)], auto=set())
    store.set_filing("publisher@news.example", "biz", "Inbox/Quotes/Businessreview", 40, 1.0, 0.95)
    suggest_rules(graph, store, actions)
    actions.reject(store.list_actions(status="pending")[0]["id"], "dave")
    assert suggest_rules(graph, store, actions) == 0


# ── filing by content ────────────────────────────────────────────────────────

import json

from agent.filing import PROFILES, learn_filing as _learn
from llm.provider import LLMResponse


class ChooseLLM:
    def __init__(self, picks):
        self.picks, self.calls = picks, []

    def complete(self, messages, system=None, tools=None, max_tokens=2048, temperature=None, tool_choice=None):
        self.calls.append({"content": messages[0]["content"], "system": system, "tool_choice": tool_choice})
        return LLMResponse(content=[{"type": "tool_use", "id": "x", "name": "choose_folders",
                                     "input": {"emails": self.picks}}], stop_reason="tool_use")


PROFILE_LIST = [{"id": "fin", "path": "Inbox/Receipts/Financial", "count": 7,
                 "examples": ["Monthly financials (from cpa.example)"], "senders": ["cpa.example"]},
                {"id": "hr-rj", "path": "Inbox/HR/RJ", "count": 3,
                 "examples": ["PTO request (from tag.example)"], "senders": ["rj@tag.example", "tag.example"]}]


def content_inbox():
    flagged = {**mail("joe@cpa.example", mid="flagged"), "flag": {"flagStatus": "flagged"}}
    in_request = {**mail("pat@client.example", mid="thread"), "conversationId": "t-open"}
    return [mail("joe@cpa.example", mid="fin-mail"), mail("rj@tag.example", mid="rj-mail"),
            mail("someone@x.example", mid="unclear"), flagged, in_request]


def test_learning_builds_folder_profiles_from_recent_examples():
    graph = FakeGraph(folder_mail={"hr-rj": [mail("rj@tag.example")] * 2, "board": []})
    store = Store(":memory:")
    _learn(graph, store)
    profiles = json.loads(store.get_state(PROFILES))
    assert [p["path"] for p in profiles] == ["Inbox/HR/RJ"]            # empty folders aren't offered
    assert profiles[0]["examples"] == ["From rj@tag.example (from tag.example)"]
    assert profiles[0]["senders"] == ["rj@tag.example", "tag.example"]


def test_a_confident_pick_without_sender_history_stays_in_the_inbox():
    graph = FakeGraph(inbox=[mail("newcomer@other.example", mid="new")])
    store = Store(":memory:")
    store.set_state(PROFILES, json.dumps(PROFILE_LIST))
    actions = Actions(store, [file_kind(graph, store)], auto=set())
    llm = ChooseLLM([{"n": 0, "folder": 1, "confidence": "high", "reason": "looks financial"}])
    assert file_read_mail(graph, store, actions, llm=llm) == 0 and graph.posts == []


def test_content_filing_files_only_confident_picks_and_asks_once():
    graph = FakeGraph(inbox=content_inbox())
    store = Store(":memory:")
    store.set_state(PROFILES, json.dumps(PROFILE_LIST))
    store.save_request("thread", "t-open", "2026-10-08T10:00:00Z", "new", {"from_email": "pat@client.example"})
    actions = Actions(store, [file_kind(graph, store)], auto=set())
    llm = ChooseLLM([
        {"n": 0, "folder": 1, "confidence": "high", "reason": "monthly financials"},
        {"n": 1, "folder": 2, "confidence": "medium", "reason": "maybe RJ's"},
        {"n": 2, "folder": 0, "confidence": "high", "reason": "needs Dave"},
    ])
    assert file_read_mail(graph, store, actions, llm=llm) == 1
    assert graph.posts == [(f"/users/{DAVE}/messages/fin-mail/move", {"destinationId": "fin"})]
    sent = llm.calls[0]["content"]
    assert "flagged" not in sent and "pat@client.example" not in sent   # flagged and live requests are skipped
    assert "1. Inbox/Receipts/Financial: Monthly financials" in llm.calls[0]["system"]
    [filed] = filed_today(store)
    assert filed["reason"] == "monthly financials"

    file_read_mail(graph, store, actions, llm=llm)
    assert len(llm.calls) == 1   # every email judged once: no second model call


def test_undoing_a_content_filing_keeps_the_sender_rule_and_the_email_stays_put():
    graph = FakeGraph(inbox=[mail("joe@cpa.example", mid="fin-mail")])
    store = Store(":memory:")
    store.set_state(PROFILES, json.dumps(PROFILE_LIST))
    store.set_filing("other@cpa.example", "fin", "Inbox/Receipts/Financial", 5, 1.0, 0.0)
    actions = Actions(store, [file_kind(graph, store)], auto=set())
    file_read_mail(graph, store, actions, llm=ChooseLLM([{"n": 0, "folder": 1, "confidence": "high"}]))
    [filed] = filed_today(store)
    undo_filing(graph, store, filed["action_id"], "fin-mail")
    assert store.filing_seen_ids(["new-fin-mail"]) == {"new-fin-mail"}   # won't be refiled
    assert [r["address"] for r in store.filing_rules()] == ["other@cpa.example"]


def test_mail_addressed_to_dave_waits_until_he_replies():
    asked = {**mail("kelly@tag.example", mid="ask"), "conversationId": "t-ask",
             "toRecipients": [{"emailAddress": {"address": DAVE}}]}
    copied = {**mail("joe@cpa.example", mid="cc"), "toRecipients": [{"emailAddress": {"address": "ap@cpa.example"}}]}
    graph = FakeGraph(inbox=[asked, copied])
    store = Store(":memory:")
    store.set_state(PROFILES, json.dumps(PROFILE_LIST))
    actions = Actions(store, [file_kind(graph, store)], auto=set())
    llm = ChooseLLM([{"n": 0, "folder": 1, "confidence": "high"}])

    file_read_mail(graph, store, actions, llm=llm)
    assert "kelly@tag.example" not in llm.calls[0]["content"]        # held back: Dave hasn't answered
    assert store.filing_seen_ids(["ask"]) == set()                   # and not written off

    graph.folder_mail["sentitems"] = [{"id": "reply", "sentDateTime": "2026-10-08T11:00:00Z"}]
    llm2 = ChooseLLM([{"n": 0, "folder": 2, "confidence": "high"}])
    file_read_mail(graph, store, actions, llm=llm2)
    assert "kelly@tag.example" in llm2.calls[0]["content"]           # replied: now it can be filed
