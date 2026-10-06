"""The agent loop: model asks for tools, we run them, repeat until it answers."""

from __future__ import annotations

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
- "Clean up my inbox", "any junk?", "check my spam" -> sweep_junk. It only proposes; mention how many
  it found and anything it was unsure about.
- Be brief and concrete, the way a sharp human assistant would write to a busy CEO, often on his phone."""


@dataclass
class ToolTrace:
    name: str
    input: dict[str, Any]
    output: str
    is_error: bool


@dataclass
class TurnResult:
    text: str
    messages: list[dict[str, Any]]
    trace: list[ToolTrace] = field(default_factory=list)
    hit_step_limit: bool = False


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
    """Run one user turn to completion. `messages` must end with the user's message."""
    history = list(messages)
    trace: list[ToolTrace] = []
    for _ in range(max_steps):
        response = llm.complete(history, system=system, tools=registry.specs(), max_tokens=max_tokens)
        history.append(response.assistant_message())
        calls = response.tool_calls
        if not calls:
            return TurnResult(text=response.text, messages=history, trace=trace)
        results = []
        for call in calls:
            content, is_error = registry.run(call)
            trace.append(ToolTrace(call.name, call.input, content, is_error))
            results.append(tool_result_block(call, content, is_error))
        history.append(tool_results_message(results))

    return TurnResult(
        text="I stopped after too many steps without finishing. Try narrowing the request.",
        messages=history,
        trace=trace,
        hit_step_limit=True,
    )
