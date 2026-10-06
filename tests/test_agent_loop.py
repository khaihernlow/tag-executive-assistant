from agent.loop import run_turn
from agent.tools import Tool, ToolRegistry
from llm.provider import LLMResponse, ToolSpec

ECHO = Tool(ToolSpec("echo", "Echo input.", {"type": "object"}), handler=lambda args: {"echo": args["value"]})
BOOM = Tool(ToolSpec("boom", "Always fails.", {"type": "object"}), handler=lambda args: 1 / 0)


class ScriptedLLM:
    """Returns pre-baked responses and records what it was sent."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.seen = []

    def complete(self, messages, system=None, tools=None, max_tokens=2048, temperature=None):
        self.seen.append(list(messages))
        return self.responses.pop(0)


def tool_use(*calls):
    return LLMResponse(
        content=[{"type": "tool_use", "id": f"tu_{i}", "name": name, "input": args}
                 for i, (name, args) in enumerate(calls)],
        stop_reason="tool_use",
    )


def text(value):
    return LLMResponse(content=[{"type": "text", "text": value}], stop_reason="end_turn")


def test_runs_tools_and_feeds_results_back():
    llm = ScriptedLLM([tool_use(("echo", {"value": "hi"})), text("done")])
    result = run_turn(llm, ToolRegistry([ECHO]), [{"role": "user", "content": "go"}], "sys")

    assert result.text == "done"
    assert [t.output for t in result.trace] == ['{"echo": "hi"}']
    tool_results = llm.seen[1][-1]
    assert tool_results["role"] == "user"
    assert tool_results["content"][0] == {"type": "tool_result", "tool_use_id": "tu_0", "content": '{"echo": "hi"}'}


def test_parallel_calls_return_in_one_message_and_errors_are_flagged():
    llm = ScriptedLLM([tool_use(("echo", {"value": 1}), ("boom", {}), ("missing", {})), text("ok")])
    result = run_turn(llm, ToolRegistry([ECHO, BOOM]), [{"role": "user", "content": "go"}], "sys")

    blocks = llm.seen[1][-1]["content"]
    assert [b.get("is_error", False) for b in blocks] == [False, True, True]
    assert "ZeroDivisionError" in blocks[1]["content"]
    assert "Unknown tool" in blocks[2]["content"]
    assert result.text == "ok"


def test_stops_at_step_limit():
    llm = ScriptedLLM([tool_use(("echo", {"value": i})) for i in range(3)])
    result = run_turn(llm, ToolRegistry([ECHO]), [{"role": "user", "content": "go"}], "sys", max_steps=3)

    assert result.hit_step_limit
    assert len(result.trace) == 3


def test_does_not_mutate_callers_history():
    history = [{"role": "user", "content": "go"}]
    run_turn(ScriptedLLM([text("hi")]), ToolRegistry([]), history, "sys")
    assert history == [{"role": "user", "content": "go"}]
