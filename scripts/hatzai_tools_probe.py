"""Phase 0 probe: client-managed tool calling through HatzAI.

Targets the Anthropic-compatible gateway (/v1/anthropic/messages). The
Hatz-native /v1/chat/completions drops client `tools`, which an earlier
version of this probe mistakenly tested.

Checks:
  1. plain chat with a top-level system prompt works
  2. offering a tool yields a structured `tool_use` block (stop_reason tool_use)
  3. a `tool_result` round-trips into a final answer built from that result
  4. the model can request two tools in one turn

Usage:
  python scripts/hatzai_tools_probe.py --env-file ../tag-tool-suite/.env
  python scripts/hatzai_tools_probe.py --model anthropic.claude-sonnet-5 --quiet
  python scripts/hatzai_tools_probe.py --list-models
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

from llm.hatzai import HatzAIError, HatzAIProvider, parse_response
from llm.provider import ToolSpec, tool_result_block, tool_results_message

FIND_FREE_TIME = ToolSpec(
    name="find_free_time",
    description="Find open slots on Dave's calendar within a date range.",
    input_schema={
        "type": "object",
        "properties": {
            "start_date": {"type": "string", "description": "ISO date, e.g. 2026-10-12"},
            "end_date": {"type": "string", "description": "ISO date"},
            "duration_minutes": {"type": "integer"},
        },
        "required": ["start_date", "end_date", "duration_minutes"],
    },
)

LOOKUP_COMPANY = ToolSpec(
    name="lookup_company",
    description="Look up a client company in Autotask by name.",
    input_schema={
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    },
)

SYSTEM = "You are Dave's executive assistant. Use the tools when they help. Today is Monday 2026-10-05."

# Distinctive values the model cannot guess, so a correct final answer proves it read the tool result.
FAKE_SLOTS = {"slots": ["2026-10-13T10:40", "2026-10-14T15:20"]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--list-models", action="store_true")
    parser.add_argument("--quiet", action="store_true", help="hide raw responses")
    args = parser.parse_args()

    load_dotenv(args.env_file, override=True)
    llm = HatzAIProvider(model=args.model)

    if args.list_models:
        for model in llm.list_models():
            print(model.get("name", model) if isinstance(model, dict) else model)
        return 0

    def call(label: str, payload: dict) -> dict | None:
        try:
            data = llm.post_raw(payload)
        except HatzAIError as e:
            print(f"\n--- {label}: ERROR ---\n{e}")
            return None
        if not args.quiet:
            print(f"\n--- {label} (raw) ---")
            print(json.dumps(data, indent=2)[:3000])
        return data

    print(f"Model: {llm.model}")
    results: dict[str, str] = {}

    # 1. plain chat
    data = call("1 plain chat", llm.build_payload(
        [{"role": "user", "content": "Reply with just the word: ready"}], system=SYSTEM, max_tokens=20))
    results["plain chat"] = "PASS" if data and parse_response(data).text else "FAIL"

    # 2. single tool call
    messages = [{"role": "user", "content": "Find me a free 30 minute slot next week to meet Kai."}]
    data = call("2 tool call", llm.build_payload(messages, system=SYSTEM, tools=[FIND_FREE_TIME], max_tokens=512))
    first = parse_response(data) if data else None
    if first and first.tool_calls:
        c = first.tool_calls[0]
        results["tool_use"] = f"PASS stop={first.stop_reason} -> {c.name}({c.input})"
    elif first and "<function_calls>" in first.text:
        results["tool_use"] = "FAIL (tool call written into text)"
    else:
        results["tool_use"] = "FAIL (no tool_use block)"

    # 3. round trip with a tool result
    if first and first.tool_calls:
        messages.append(first.assistant_message())
        messages.append(tool_results_message(
            [tool_result_block(c, json.dumps(FAKE_SLOTS)) for c in first.tool_calls]))
        data = call("3 round trip", llm.build_payload(messages, system=SYSTEM, tools=[FIND_FREE_TIME], max_tokens=512))
        if data:
            final = parse_response(data)
            ok = "10:40" in final.text or "15:20" in final.text or "3:20" in final.text
            results["round trip"] = "PASS (used tool result)" if ok else f"UNCLEAR -> {final.text[:200]!r}"
        else:
            results["round trip"] = "FAIL"
    else:
        results["round trip"] = "SKIPPED"

    # 4. two tools in one turn
    messages = [{"role": "user", "content": (
        "Before I meet Acme Dental, look them up in Autotask and also find a free hour this Thursday. "
        "Call both tools now.")}]
    data = call("4 parallel tools", llm.build_payload(
        messages, system=SYSTEM, tools=[FIND_FREE_TIME, LOOKUP_COMPANY], max_tokens=512))
    names = [c.name for c in parse_response(data).tool_calls] if data else []
    results["parallel tools"] = f"PASS {names}" if len(names) >= 2 else f"PARTIAL {names}"

    print("\n=== Verdict ===")
    for name, outcome in results.items():
        print(f"{name:15} {outcome}")
    native = results["tool_use"].startswith("PASS") and results["round trip"].startswith("PASS")
    print("\nClient tool calling:", "SUPPORTED" if native else "NOT WORKING")
    return 0 if native else 1


if __name__ == "__main__":
    sys.exit(main())
