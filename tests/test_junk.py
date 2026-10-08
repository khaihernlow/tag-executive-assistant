import json

import pytest

from agent.actions import Actions
from agent.cards import build_cards
from agent.junk import (
    act, bulk_markers, junk_kind, junk_tools, learn_history, moved_today, reputation, sweep_new, triage, undo,
)
from agent.loop import ToolTrace
from agent.tools import ToolRegistry
from llm.provider import LLMResponse, ToolCall
from store.db import Store

DAVE = "dave@tag.example"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    from agent import junk

    monkeypatch.setenv("GRAPH_MAILBOX", DAVE)
    junk._correspondents.clear()
    junk._owner.clear()


def msg(mid, address, subject="Hi", headers=(), name="", folder="inbox-id", received="2026-10-08T12:00:00Z"):
    return {"id": mid, "subject": subject, "receivedDateTime": received, "bodyPreview": "...",
            "parentFolderId": folder, "from": {"emailAddress": {"name": name, "address": address}},
            "internetMessageHeaders": [{"name": n, "value": v} for n, v in headers]}


FOLDERS = {"inbox": "inbox-id", "junkemail": "junk-id", "deleteditems": "deleted-id", "sentitems": "sent-id",
           "drafts": "drafts-id", "outbox": "outbox-id"}


class FakeGraph:
    mailbox = DAVE

    def __init__(self, inbox=(), history=()):
        self.inbox, self.history = list(inbox), list(history)
        self.posts, self.inbox_filters = [], []

    def get(self, path, params=None, headers=None):
        if path == "/me":
            return {"displayName": "Dave Vener"}
        return {"id": FOLDERS[path.rsplit("/", 1)[-1]]}

    def get_all(self, path, params=None, limit=500, headers=None):
        if path.endswith("/sentitems/messages"):
            return [{"toRecipients": [{"emailAddress": {"address": "Client@client.example"}}]}]
        if path == "/me/people":
            return []
        if path.endswith("/inbox/messages"):
            self.inbox_filters.append(params["$filter"])
            return self.inbox
        if path.endswith("/junkemail/messages"):
            return [m for m in self.history if m["parentFolderId"] == "junk-id"]
        if path == f"/users/{DAVE}/mailFolders":
            return [{"id": fid, "childFolderCount": 0} for fid in ("inbox-id", "junk-id", "filed-id", "finance-folder-id")]
        if path.startswith(f"/users/{DAVE}/mailFolders/") and path.endswith("/messages"):
            folder = path.split("/mailFolders/")[1].split("/")[0]
            return [m for m in self.history if m["parentFolderId"] == folder]
        return []

    def post(self, path, body, headers=None):
        self.posts.append((path, body))
        if "bad" in path:
            raise RuntimeError("404 not found")
        return {"id": "moved-" + path.split("/messages/")[1].split("/")[0]}


class FakeLLM:
    """Answers the forced classify_emails call by email number."""

    def __init__(self, verdicts=()):
        self.verdicts, self.calls = list(verdicts), []

    def complete(self, messages, system=None, tools=None, max_tokens=2048, temperature=None, tool_choice=None):
        self.calls.append({"content": messages[0]["content"], "system": system})
        return LLMResponse(content=[{"type": "tool_use", "id": "c", "name": "classify_emails",
                                     "input": {"verdicts": self.verdicts}}], stop_reason="tool_use")


def setup(inbox=(), history=(), verdicts=()):
    graph, store = FakeGraph(inbox, history), Store(":memory:")
    actions = Actions(store, [junk_kind(graph, store)], auto=set())
    return graph, store, actions, FakeLLM(verdicts)


def test_bulk_markers():
    assert bulk_markers([{"name": "List-Unsubscribe", "value": "x"}, {"name": "Precedence", "value": "bulk"},
                         {"name": "Subject", "value": "y"}]) == ["list-unsubscribe", "precedence:bulk"]


# ── learning ─────────────────────────────────────────────────────────────────

def test_learns_junk_from_the_junk_folder_and_keep_from_filed_mail():
    graph, store, _, _ = setup(history=[
        msg("1", "spam@leads.example", "Buy leads", folder="junk-id"),
        msg("2", "spam@leads.example", "Buy more leads", folder="junk-id"),
        msg("3", "billing@vendor.example", "Invoice 42", folder="finance-folder-id"),
        msg("4", "news@mixed.example", "Hello", folder="junk-id"),
        msg("5", "news@mixed.example", "Hello", folder="filed-id"),
        msg("6", "news@mixed.example", "Hello", folder="filed-id"),
        msg("7", "someone@else.example", "Still in inbox", folder="inbox-id"),
        msg("8", "client@client.example", "Contract", folder="filed-id"),
    ])
    stats = learn_history(graph, store)
    assert store.sender_verdicts(["spam@leads.example", "billing@vendor.example", "news@mixed.example",
                                  "someone@else.example"]) == {
        "spam@leads.example": "junk", "billing@vendor.example": "keep", "news@mixed.example": "keep"}
    examples = json.loads(store.get_state("junk_examples"))
    assert "Buy leads (from leads.example)" in examples["junk"]
    assert not any("client.example" in e for e in examples["keep"])  # correspondents aren't examples
    assert stats["senders"] == 4  # the one still in the Inbox teaches nothing


def test_reputation_order_and_domain_rules():
    store = Store(":memory:")
    store.set_sender("a@spammy.example", "junk", "junk_folder")
    store.set_sender("b@spammy.example", "junk", "junk_folder")
    store.set_sender("x@gmail.com", "junk", "junk_folder")
    store.set_sender("y@gmail.com", "junk", "junk_folder")
    store.set_sender("ap@vendor.example", "keep", "filed")
    known = {"client@client.example"}
    assert reputation(store, "garrett@tag.example", known, "tag.example")[0] == "keep"
    assert reputation(store, "client@client.example", known, "tag.example")[0] == "keep"
    # Domain-wide guesses go to the slip, never straight to Junk.
    assert reputation(store, "c@spammy.example", known, "tag.example") == ("junk_domain", "spammy.example is a domain you junk")
    assert reputation(store, "new@gmail.com", known, "tag.example")[0] is None    # never generalize free mail
    assert reputation(store, "sales@vendor.example", known, "tag.example")[0] == "keep"

    store.set_sender("a@spammy.example", "keep", "undo")
    store.set_sender("a@spammy.example", "junk", "auto")                          # can't override Dave
    assert store.sender_verdicts(["a@spammy.example"]) == {"a@spammy.example": "keep"}


# ── triage and acting ────────────────────────────────────────────────────────

INBOX = [
    msg("known-junk", "spam@leads.example", "Leads again"),
    msg("staff", "garrett@tag.example", "Renewals"),
    msg("client", "client@client.example", "Question"),
    msg("sure", "rep@pitch.example", "We build websites"),
    msg("bulky", "news@promo.example", "Big sale", headers=[("List-Unsubscribe", "<mailto:x>")]),
    msg("maybe", "hello@unknown.example", "Partnership?"),
    msg("unsure", "it@dental.example", "Wifi down"),
    msg("own", "dave.personal@icloud.example", "", name="Dave Vener"),
    msg("own2", "davevener@icloud.example", ""),
]
VERDICTS = [
    {"n": 0, "verdict": "junk", "confidence": "high", "reason": "cold pitch"},
    {"n": 1, "verdict": "junk", "confidence": "medium", "reason": "promotion"},
    {"n": 2, "verdict": "junk", "confidence": "medium", "reason": "vague partnership pitch"},
    {"n": 3, "verdict": "unsure", "confidence": "low", "reason": "could be a prospect"},
]


def test_triage_auto_ask_and_unsure_and_the_model_never_sees_known_senders():
    graph, store, _, llm = setup(verdicts=VERDICTS)
    store.set_sender("spam@leads.example", "junk", "junk_folder")
    store.set_sender("a@lists.example", "junk", "junk_folder")
    store.set_sender("b@lists.example", "junk", "junk_folder")
    result = triage(graph, llm, store, INBOX + [msg("domain", "c@lists.example", "Webinar")])
    assert [i["id"] for i in result["auto"]] == ["known-junk", "sure", "bulky"]
    assert [i["id"] for i in result["ask"]] == ["domain", "maybe"]
    assert [i["id"] for i in result["unsure"]] == ["unsure"]
    sent_to_model = llm.calls[0]["content"]
    assert "garrett@" not in sent_to_model and "client@" not in sent_to_model and "spam@leads" not in sent_to_model


def test_auto_moves_now_and_ask_items_share_one_rolling_slip():
    graph, store, actions, llm = setup()
    first = act(store, actions, {"auto": [{"id": "m1", "from": "Rep", "address": "rep@pitch.example",
                                           "subject": "Pitch", "folder": "inbox-id", "reason": "cold pitch"}],
                                 "ask": [{"id": "m2", "from": "A", "address": "a@x.example", "subject": "S",
                                          "folder": "inbox-id", "reason": "r"}], "unsure": []})
    assert first["moved"] == 1 and graph.posts[0] == (f"/users/{DAVE}/messages/m1/move", {"destinationId": "junkemail"})
    second = act(store, actions, {"auto": [], "ask": [{"id": "m3", "from": "B", "address": "b@y.example",
                                                       "subject": "T", "folder": "inbox-id", "reason": "r"}],
                                  "unsure": []})
    assert second["ask_action"]["action_id"] == first["ask_action"]["action_id"]
    assert [i["id"] for i in second["ask_action"]["items"]] == ["m2", "m3"]
    assert store.sender_verdicts(["rep@pitch.example"]) == {"rep@pitch.example": "junk"}  # learned from auto


def test_slip_keeps_unticked_emails_and_learns_from_dave():
    graph, store, actions, _ = setup()
    out = act(store, actions, {"auto": [], "unsure": [], "ask": [
        {"id": "m2", "from": "A", "address": "a@x.example", "subject": "S", "folder": "inbox-id", "reason": "r"},
        {"id": "m3", "from": "B", "address": "b@y.example", "subject": "T", "folder": "inbox-id", "reason": "r"}]})
    done = actions.approve(out["ask_action"]["action_id"], "dave", edits={"keep_ids": "m3"})
    assert done["result"]["moved"] == 1 and [p for p, _ in graph.posts] == [f"/users/{DAVE}/messages/m2/move"]
    assert store.sender_verdicts(["a@x.example", "b@y.example"]) == {"a@x.example": "junk", "b@y.example": "keep"}


def test_declining_the_slip_means_keep_those_senders():
    graph, store, actions, _ = setup()
    out = act(store, actions, {"auto": [], "unsure": [], "ask": [
        {"id": "m2", "from": "A", "address": "a@x.example", "subject": "S", "folder": "inbox-id", "reason": "r"}]})
    actions.reject(out["ask_action"]["action_id"], "dave")
    assert store.sender_verdicts(["a@x.example"]) == {"a@x.example": "keep"} and graph.posts == []


def test_undo_moves_it_back_once_and_keeps_the_sender():
    graph, store, actions, _ = setup()
    out = act(store, actions, {"auto": [{"id": "m1", "from": "Rep", "address": "rep@pitch.example",
                                         "subject": "Pitch", "folder": "filed-id", "reason": "cold pitch"}],
                               "ask": [], "unsure": []})
    aid = out["auto_action"]["action_id"]
    [today] = moved_today(store)
    assert today["auto"] is True and today["undone"] is False

    undo(graph, store, aid, "m1")
    assert graph.posts[-1] == (f"/users/{DAVE}/messages/moved-m1/move", {"destinationId": "filed-id"})
    assert store.sender_verdicts(["rep@pitch.example"]) == {"rep@pitch.example": "keep"}
    assert moved_today(store)[0]["undone"] is True
    with pytest.raises(ValueError):
        undo(graph, store, aid, "m1")


def test_sweep_only_looks_at_mail_since_the_last_check():
    graph, store, actions, llm = setup(inbox=[msg("n1", "client@client.example", received="2026-10-08T12:00:00Z")])
    sweep_new(graph, llm, store, actions)
    assert "receivedDateTime gt" in graph.inbox_filters[0]
    assert store.get_state("junk_checkpoint") == "2026-10-08T12:00:00Z"
    graph.inbox = []
    sweep_new(graph, llm, store, actions)
    assert graph.inbox_filters[1] == "receivedDateTime gt 2026-10-08T12:00:00Z"
    assert store.get_state("junk_checkpoint") == "2026-10-08T12:00:00Z"


def test_chat_tool_reports_what_happened_and_shows_the_slip():
    graph, store, actions, llm = setup(inbox=INBOX[3:6], verdicts=VERDICTS[:3])
    content, is_error = ToolRegistry(junk_tools(graph, llm, actions, store)).run(ToolCall("t", "sweep_junk", {}))
    out = json.loads(content)
    assert not is_error and out["moved_automatically"] == 2 and out["awaiting_approval"] == 1
    cards = build_cards([ToolTrace("sweep_junk", {}, content, False)])
    assert [c["type"] for c in cards] == ["action"]
