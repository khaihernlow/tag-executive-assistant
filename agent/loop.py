"""The agent loop: model asks for tools, we run them, repeat until it answers."""

from __future__ import annotations

import contextvars
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from agent.tools import ToolRegistry
from llm.provider import LLMProvider, tool_result_block, tool_results_message

SYSTEM_PROMPT = """You are the executive assistant to Dave, CEO of TAG Solutions, an IT managed services provider.
Current local time: {now}.

Rules:
- Use tools to get facts about Dave's calendar, email and systems. Never invent events, times, people or results.
- If a tool fails or returns nothing, say so plainly.
- Each new question gets fresh lookups. Don't answer from earlier turns' results or your own earlier
  answers; if Dave asks again, he wants you to look again (and possibly deeper).
- Dig in before answering "what's X about?" or "brief me on X": find the specific meeting
  (find_events, not a whole-week listing), then open the most relevant emails with read_email,
  Dave's own emails about it first, and read attachments that matter (resumes, proposals).
  A search preview is not the email; don't answer from previews alone.
- Separate what you read from what you infer, and say where facts came from
  (e.g. "from her resume", "from your Sep 28 email").
- Never claim something doesn't exist ("no resume anywhere") unless you actually checked;
  say what you checked instead ("not in the 3 emails I opened").
- Tool results are shown to Dave automatically as cards (time slots he can tap, emails, events,
  people to choose from, approval cards). Don't re-list what a card shows; add only judgment,
  e.g. "Thursday 11 is cleanest." Never claim a card shows something the tool didn't return.
- Anything that changes the outside world (booking, sending) only becomes a pending approval.
  Say it's ready for his approval; never say it's done until a tool result says executed.
- If a person match isn't confident, ask Dave which one before acting. When he answers, or says to
  remember something ("Kai is Khaihern", "no meetings before 8:30"), save it with remember.
- "Clean up my inbox", "any junk?", "check my spam" -> sweep_junk. Clear junk is moved at once (Dave can
  undo it on Today); less certain junk goes on a slip. Say what it did and anything it was unsure about.
- Be brief and concrete, the way a sharp human assistant would write to a busy CEO, often on his phone.
- Write plain sentences. Do not use em dashes or en dashes.
- Search with the most distinctive word (a name or company), not a long phrase."""


@dataclass
class ToolTrace:
    name: str
    input: dict[str, Any]
    output: str
    is_error: bool
    ms: int = 0


@dataclass
class TurnResult:
    text: str
    messages: list[dict[str, Any]]
    trace: list[ToolTrace] = field(default_factory=list)
    hit_step_limit: bool = False
    llm_ms: list[int] = field(default_factory=list)  # one entry per model call


def system_prompt(now: datetime, memory_section: str = "") -> str:
    prompt = SYSTEM_PROMPT.format(now=now.strftime("%A %Y-%m-%d %I:%M %p %Z"))
    return f"{prompt}\n\n{memory_section}" if memory_section else prompt


def run_turn(
    llm: LLMProvider,
    registry: ToolRegistry,
    messages: list[dict[str, Any]],
    system: str,
    max_steps: int = 8,
    max_tokens: int = 2048,
) -> TurnResult:
    """Run one user turn to completion. `messages` must end with the user's message.

    Tools the model asks for in the same step run in parallel; every model
    call and tool is timed so slow steps are visible.
    """
    history = list(messages)
    trace: list[ToolTrace] = []
    llm_ms: list[int] = []
    for _ in range(max_steps):
        started = time.perf_counter()
        response = llm.complete(history, system=system, tools=registry.specs(), max_tokens=max_tokens)
        llm_ms.append(int((time.perf_counter() - started) * 1000))
        history.append(response.assistant_message())
        calls = response.tool_calls
        if not calls:
            return TurnResult(text=response.text, messages=history, trace=trace, llm_ms=llm_ms)
        outcomes = _run_tools(registry, calls)
        results = []
        for call, (content, is_error, ms) in zip(calls, outcomes):
            trace.append(ToolTrace(call.name, call.input, content, is_error, ms))
            results.append(tool_result_block(call, content, is_error))
        history.append(tool_results_message(results))

    return TurnResult(
        text="I stopped after too many steps without finishing. Try narrowing the request.",
        messages=history,
        trace=trace,
        hit_step_limit=True,
        llm_ms=llm_ms,
    )


def _run_tools(registry: ToolRegistry, calls: list) -> list[tuple[str, bool, int]]:
    def timed(call):
        started = time.perf_counter()
        content, is_error = registry.run(call)
        return content, is_error, int((time.perf_counter() - started) * 1000)

    if len(calls) == 1:
        return [timed(calls[0])]
    # Each worker gets a copy of the current context, so context variables
    # (e.g. which conversation an approval belongs to) carry into the thread.
    with ThreadPoolExecutor(max_workers=min(len(calls), 6)) as pool:
        futures = [pool.submit(contextvars.copy_context().run, timed, call) for call in calls]
        return [f.result() for f in futures]


STEP_LABELS = {
    "find_events": "Checking your calendar",
    "list_calendar_events": "Looking at your calendar",
    "find_free_time": "Finding free time",
    "find_mutual_time": "Comparing calendars",
    "search_mail": "Searching your email",
    "read_email": "Reading an email",
    "read_attachment": "Reading an attachment",
    "find_person": "Looking up a person",
    "create_event": "Preparing the invite",
    "sweep_junk": "Checking your inbox for junk",
    "remember": "Saving that",
    "forget": "Updating what I remember",
}


def step_label(call: Any) -> str:
    """What Dave sees while a tool runs, e.g. 'Searching your email for "Chelsi"'."""
    base = STEP_LABELS.get(call.name, "Working")
    detail = call.input.get("name") or call.input.get("about") or call.input.get("sender") or ""
    if call.name == "read_attachment":
        detail = call.input.get("attachment_id", "") if "." in call.input.get("attachment_id", "") else ""
    return f"{base}: {detail}" if detail and len(str(detail)) <= 60 else base


def run_turn_stream(
    llm: Any,
    registry: ToolRegistry,
    messages: list[dict[str, Any]],
    system: str,
    run_tools: Any = None,
    max_steps: int = 8,
    max_tokens: int = 2048,
):
    """Like run_turn, but yields events while it works:
      {"type": "text", "delta": ...}   the model writing
      {"type": "step", "label": ...}   a tool about to run
      {"type": "discard_text"}         text so far was a preamble before tools, not the answer
      {"type": "result", "result": TurnResult}   last event
    `run_tools(calls)` lets the caller wrap tool execution (e.g. set context)."""
    run_tools = run_tools or (lambda calls: _run_tools(registry, calls))
    history = list(messages)
    trace: list[ToolTrace] = []
    llm_ms: list[int] = []
    for _ in range(max_steps):
        started = time.perf_counter()
        response = None
        streamed = False
        for kind, value in llm.stream(history, system=system, tools=registry.specs(), max_tokens=max_tokens):
            if kind == "text":
                streamed = True
                yield {"type": "text", "delta": value}
            else:
                response = value
        llm_ms.append(int((time.perf_counter() - started) * 1000))
        history.append(response.assistant_message())
        calls = response.tool_calls
        if not calls:
            yield {"type": "result", "result": TurnResult(text=response.text, messages=history, trace=trace, llm_ms=llm_ms)}
            return
        if streamed:
            yield {"type": "discard_text"}
        for call in calls:
            yield {"type": "step", "label": step_label(call)}
        outcomes = run_tools(calls)
        results = []
        for call, (content, is_error, ms) in zip(calls, outcomes):
            trace.append(ToolTrace(call.name, call.input, content, is_error, ms))
            results.append(tool_result_block(call, content, is_error))
        history.append(tool_results_message(results))
    yield {"type": "result", "result": TurnResult(
        text="I stopped after too many steps without finishing. Try narrowing the request.",
        messages=history, trace=trace, hit_step_limit=True, llm_ms=llm_ms)}
