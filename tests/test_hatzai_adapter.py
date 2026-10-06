import pytest

from llm.hatzai import MESSAGES_URL, HatzAIError, HatzAIProvider, parse_response
from llm.provider import ToolCall, ToolSpec, tool_result_block, tool_results_message

TOOL = ToolSpec(
    name="find_free_time",
    description="Find open slots.",
    input_schema={"type": "object", "properties": {"days": {"type": "integer"}}},
)


def test_targets_anthropic_gateway_not_chat_completions():
    assert MESSAGES_URL == "https://ai.hatz.ai/v1/anthropic/messages"


def test_payload_includes_optional_fields_only_when_given():
    llm = HatzAIProvider(api_key="k", model="m")
    full = llm.build_payload([], system="sys", tools=[TOOL], max_tokens=100, temperature=0.2)
    bare = llm.build_payload([], max_tokens=100)

    assert full["system"] == "sys"
    assert full["tools"] == [{
        "name": "find_free_time",
        "description": "Find open slots.",
        "input_schema": {"type": "object", "properties": {"days": {"type": "integer"}}},
    }]
    assert full["temperature"] == 0.2
    assert set(bare) == {"model", "max_tokens", "messages"}


def test_parses_text():
    result = parse_response({"content": [{"type": "text", "text": "hi"}], "stop_reason": "end_turn"})
    assert result.text == "hi"
    assert result.tool_calls == []


def test_parses_tool_use_blocks():
    result = parse_response({
        "content": [
            {"type": "text", "text": "Checking."},
            {"type": "tool_use", "id": "tu_1", "name": "find_free_time", "input": {"days": 5}},
            {"type": "tool_use", "id": "tu_2", "name": "lookup_company", "input": {}},
        ],
        "stop_reason": "tool_use",
    })

    assert result.stop_reason == "tool_use"
    assert result.text == "Checking."
    assert result.tool_calls == [
        ToolCall(id="tu_1", name="find_free_time", input={"days": 5}),
        ToolCall(id="tu_2", name="lookup_company", input={}),
    ]


def test_non_object_tool_input_raises():
    with pytest.raises(HatzAIError):
        parse_response({"content": [{"type": "tool_use", "id": "a", "name": "x", "input": "nope"}]})


def test_unexpected_shape_raises():
    with pytest.raises(HatzAIError):
        parse_response({"error": "nope"})


def test_round_trip_messages():
    result = parse_response({
        "content": [{"type": "tool_use", "id": "tu_1", "name": "find_free_time", "input": {"days": 5}}],
        "stop_reason": "tool_use",
    })
    call = result.tool_calls[0]

    assert result.assistant_message() == {"role": "assistant", "content": result.content}
    assert tool_results_message([tool_result_block(call, "ok"), tool_result_block(call, "boom", is_error=True)]) == {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "tu_1", "content": "ok"},
            {"type": "tool_result", "tool_use_id": "tu_1", "content": "boom", "is_error": True},
        ],
    }
