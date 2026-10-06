import json

import pytest

from agent.actions import Actions
from agent.cards import build_cards
from agent.junk import bulk_markers, classify, junk_kind, junk_tools, sweep, unknown_senders
from agent.loop import ToolTrace
from agent.tools import ToolRegistry
from llm.provider import LLMResponse, ToolCall
from store.db import Store


@pytest.fixture(autouse=True)
def env(monkeypatch):
    from agent import junk

    monkeypatch.setenv("GRAPH_MAILBOX", "dave@tag.example")
    junk._correspondents.clear()


def msg(mid, address, subject="Hi", headers=(), name=""):
    return {"id": mid, "subject": subject, "receivedDateTime": "2026-10-05T12:00:00Z", "bodyPreview": "...",
            "from": {"emailAddress": {"name": name, "address": address}},
            "internetMessageHeaders": [{"name": n, "value": v} for n, v in headers]}


INBOX = [
    msg("colleague", "garrett@tag.example"),
    msg("client", "owner@client.example"),                                   # Dave has emailed them
    msg("pitch", "rep@leads.example", "Buy 10,000 MSP leads", [("List-Unsubscribe", "<mailto:x>")]),
    msg("prospect", "it@dental.example", "Need help with our network"),
    msg("maybe", "news@vendor.example", "Q4 webinar"),
]


class FakeGraph:
    mailbox = "dave@tag.example"

    def __init__(self):
        self.posts = []

    def get_all(self, path, params=None, limit=500, headers=None):
        if path.endswith("/sentitems/messages"):
            return [{"toRecipients": [{"emailAddress": {"address": "Owner@Client.example"}}]}]
        if path == "/me/people":
            return []
        if path.endswith("/inbox/messages"):
            return INBOX
        return []

    def post(self, path, body, headers=None):
        self.posts.append((path, body))
        if "bad" in path:
            raise RuntimeError("404 not found")
        return {}


class FakeLLM:
    """Answers the forced classify_emails call by email number."""

    def __init__(self, verdicts):
        self.verdicts = verdicts
        self.calls = []

    def complete(self, messages, system=None, tools=None, max_tokens=2048, temperature=None, tool_choice=None):
        self.calls.append((messages[0]["content"], tool_choice))
        return LLMResponse(content=[{"type": "tool_use", "id": "c", "name": "classify_emails",
                                     "input": {"verdicts": self.verdicts}}], stop_reason="tool_use")


def test_bulk_markers():
    assert bulk_markers([{"name": "List-Unsubscribe", "value": "x"}, {"name": "Precedence", "value": "bulk"},
                         {"name": "Subject", "value": "y"}]) == ["list-unsubscribe", "precedence:bulk"]


def test_known_and_internal_senders_are_never_candidates():
    candidates = unknown_senders(INBOX, {"owner@client.example"}, "tag.example")
    assert [c["id"] for c in candidates] == ["pitch", "prospect", "maybe"]
    assert candidates[0]["bulk"] == ["list-unsubscribe"]


def test_only_model_junk_verdicts_are_proposed_and_garbage_counts_as_unsure():
    llm = FakeLLM([
        {"n": 0, "verdict": "junk", "reason": "lead-list pitch"},
        {"n": 1, "verdict": "keep", "reason": "prospect asking for IT help"},
        {"n": 7, "verdict": "junk", "reason": "out of range"},          # ignored
        {"n": 2, "verdict": "delete-it", "reason": "invalid verdict"},  # ignored -> unsure
    ])
    result = sweep(FakeGraph(), llm)
    assert result["checked"] == 5 and result["unknown_senders"] == 3
    assert [i["id"] for i in result["junk"]] == ["pitch"]
    assert [i["id"] for i in result["unsure"]] == ["maybe"]
    assert llm.calls[0][1] == "classify_emails"           # structured output was forced
    assert "garrett@" not in llm.calls[0][0]               # known senders never reach the model


def test_correspondents_are_cached_between_sweeps():
    from agent import junk

    junk._correspondents.clear()

    class CountingGraph(FakeGraph):
        def __init__(self):
            super().__init__()
            self.sent_scans = 0

        def get_all(self, path, params=None, limit=500, headers=None):
            if path.endswith("/sentitems/messages"):
                self.sent_scans += 1
            return super().get_all(path, params, limit, headers)

    graph = CountingGraph()
    assert junk.known_correspondents(graph) == junk.known_correspondents(graph) == {"owner@client.example"}
    assert graph.sent_scans == 1


def test_classify_skips_the_model_when_nothing_is_unknown():
    llm = FakeLLM([])
    assert classify(llm, []) == {}
    assert llm.calls == []


def test_tool_proposes_one_slip_and_approval_moves_to_junk():
    graph = FakeGraph()
    actions = Actions(Store(":memory:"), [junk_kind(graph)], auto=set())
    llm = FakeLLM([{"n": 0, "verdict": "junk", "reason": "lead-list pitch"}])
    content, is_error = ToolRegistry(junk_tools(graph, llm, actions)).run(ToolCall("t", "sweep_junk", {}))
    out = json.loads(content)

    assert not is_error
    proposal = out["proposal"]
    assert proposal["status"] == "pending" and proposal["kind"] == "move_to_junk"
    assert proposal["items"] == [{"from": "rep@leads.example", "subject": "Buy 10,000 MSP leads", "reason": "lead-list pitch"}]
    assert graph.posts == []  # nothing moved before approval

    done = actions.approve(proposal["action_id"], "dave")
    assert done["status"] == "executed" and done["result"] == {"moved": 1, "failed": []}
    assert graph.posts == [("/users/dave@tag.example/messages/pitch/move", {"destinationId": "junkemail"})]

    cards = build_cards([ToolTrace("sweep_junk", {}, content, False)])
    assert [c["type"] for c in cards] == ["action", "unsure"]


def test_one_failed_move_does_not_stop_the_rest():
    graph = FakeGraph()
    result = junk_kind(graph).execute({"items": [{"id": "bad", "subject": "a"}, {"id": "ok", "subject": "b"}]}, "x")
    assert result["moved"] == 1 and result["failed"][0]["subject"] == "a"
