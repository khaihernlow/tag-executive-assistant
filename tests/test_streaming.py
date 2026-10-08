import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from agent.actions import Actions
from agent.assistant import Assistant
from agent.loop import run_turn_stream, step_label
from agent.tools import Tool, ToolRegistry
from llm.hatzai import HatzAIProvider
from llm.provider import LLMResponse, ToolCall, ToolSpec
from store.db import Store


def sse(*events):
    return [f"data: {json.dumps(e)}" for e in events]


class FakeStreamResponse:
    def __init__(self, lines):
        self.lines, self.ok, self.status_code, self.text = lines, True, 200, ""

    def iter_lines(self, decode_unicode=True):
        yield from self.lines

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_hatzai_stream_yields_text_and_assembles_tool_calls():
    lines = sse(
        {"type": "message_start"},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Let me "}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "check."}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "t1", "name": "find_events", "input": {}}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"about": "Che'}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": 'lsi"}'}},
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
        {"type": "message_stop"},
    )
    llm = HatzAIProvider(api_key="k", model="m", session=SimpleNamespace(
        headers={}, post=lambda *a, **k: FakeStreamResponse(["event: x", ""] + lines)))
    events = list(llm.stream([{"role": "user", "content": "hi"}]))
    assert [e for e in events if e[0] == "text"] == [("text", "Let me "), ("text", "check.")]
    response = events[-1][1]
    assert response.text == "Let me check."
    assert response.tool_calls == [ToolCall("t1", "find_events", {"about": "Chelsi"})]
    assert response.stop_reason == "tool_use"


class ScriptedStream:
    """Streams pre-baked responses, text in two pieces."""

    def __init__(self, responses):
        self.responses = list(responses)

    def stream(self, messages, system=None, tools=None, max_tokens=2048):
        response = self.responses.pop(0)
        text = response.text
        if text:
            yield "text", text[: len(text) // 2]
            yield "text", text[len(text) // 2:]
        yield "response", response


def tool_use(name, args, preamble=""):
    content = ([{"type": "text", "text": preamble}] if preamble else []) + \
              [{"type": "tool_use", "id": "t1", "name": name, "input": args}]
    return LLMResponse(content=content, stop_reason="tool_use")


def text(value):
    return LLMResponse(content=[{"type": "text", "text": value}], stop_reason="end_turn")


ECHO = Tool(ToolSpec("find_events", "Find.", {"type": "object"}), handler=lambda args: {"events": []})


def test_stream_turn_emits_steps_discards_preambles_and_ends_with_result():
    llm = ScriptedStream([tool_use("find_events", {"about": "Chelsi"}, preamble="Checking."), text("You meet at 3.")])
    events = list(run_turn_stream(llm, ToolRegistry([ECHO]), [{"role": "user", "content": "q"}], "sys"))
    kinds = [e["type"] for e in events]
    assert kinds == ["text", "text", "discard_text", "step", "text", "text", "result"]
    assert events[3]["label"] == "Checking your calendar: Chelsi"
    assert events[-1]["result"].text == "You meet at 3."


def test_step_labels_stay_short():
    assert step_label(ToolCall("x", "search_mail", {"about": "renewals"})) == "Searching your email: renewals"
    assert step_label(ToolCall("x", "read_email", {"message_id": "AAMk..."})) == "Reading an email"
    assert step_label(ToolCall("x", "find_person", {"name": "x" * 80})) == "Looking up a person"


def test_stream_endpoint_sends_ndjson_and_saves_the_conversation(monkeypatch):
    monkeypatch.setenv("REQUIRE_AUTH", "False")
    monkeypatch.setenv("GRAPH_MAILBOX", "dave@tag.example")
    from app.main import app

    store = Store(":memory:")
    actions = Actions(store, [], auto=set())
    llm = ScriptedStream([tool_use("find_events", {"about": "Chelsi"}), text("Reach sam@staffing.example.")])
    app.state.services = SimpleNamespace(store=store, actions=actions,
                                         assistant=Assistant(llm, ToolRegistry([ECHO]), store, actions))
    try:
        resp = TestClient(app).post("/api/chat/stream", json={"message": "When is it?"})
        events = [json.loads(line) for line in resp.text.splitlines() if line]
        assert resp.headers["content-type"].startswith("application/x-ndjson")
        assert [e["type"] for e in events][:2] == ["start", "step"]
        done = events[-1]
        assert done["type"] == "done"
        # The final text is fact-checked: an address no tool returned is flagged.
        assert done["text"] == "Reach sam@staffing.example (unverified)."
        convo = store.get_conversation(done["conversation_id"])
        assert [m["role"] for m in convo["display"]] == ["user", "assistant"]
    finally:
        app.state.services = None


def test_stream_endpoint_reports_errors_as_an_event(monkeypatch):
    monkeypatch.setenv("REQUIRE_AUTH", "False")
    from app.main import app

    class Broken:
        def stream(self, *a, **k):
            raise RuntimeError("gateway down")
            yield  # pragma: no cover

    store = Store(":memory:")
    actions = Actions(store, [], auto=set())
    app.state.services = SimpleNamespace(store=store, actions=actions,
                                         assistant=Assistant(Broken(), ToolRegistry([]), store, actions))
    try:
        events = [json.loads(l) for l in TestClient(app).post("/api/chat/stream", json={"message": "hi"}).text.splitlines()]
        assert events[-1] == {"type": "error", "message": "RuntimeError: gateway down"}
    finally:
        app.state.services = None
