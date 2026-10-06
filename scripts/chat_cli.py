"""Talk to the assistant in the terminal, with its real tools.

Usage:
  python scripts/chat_cli.py
  python scripts/chat_cli.py --model anthropic.claude-sonnet-5 --show-tools
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

from agent.calendar import calendar_tools, local_zone
from agent.loop import run_turn, system_prompt
from agent.mail import mail_tools
from agent.people import people_tools
from agent.scheduling import scheduling_tools
from agent.tools import ToolRegistry
from connectors.graph import GraphClient
from llm.hatzai import HatzAIProvider


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--show-tools", action="store_true")
    args = parser.parse_args()
    load_dotenv(args.env_file, override=True)

    llm = HatzAIProvider(model=args.model)
    graph = GraphClient.from_env()
    registry = ToolRegistry(
        calendar_tools(graph) + people_tools(graph) + mail_tools(graph) + scheduling_tools(graph)
    )
    messages: list[dict] = []
    print(f"Assistant ready ({llm.model}). Ctrl+C to quit.\n")

    while True:
        try:
            user = input("you> ").strip()
        except (KeyboardInterrupt, EOFError):
            print()
            return 0
        if not user:
            continue
        messages.append({"role": "user", "content": user})
        result = run_turn(llm, registry, messages, system_prompt(datetime.now(local_zone())))
        messages = result.messages
        if args.show_tools:
            for t in result.trace:
                flag = "ERROR " if t.is_error else ""
                print(f"  [{flag}{t.name}({json.dumps(t.input)}) -> {t.output[:200]}]")
        print(f"\nassistant> {result.text}\n")


if __name__ == "__main__":
    sys.exit(main())
