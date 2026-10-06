"""Phase 0 probe: can the app registration read Dave's calendar (and only his)?

Usage:
  python scripts/graph_probe.py
  python scripts/graph_probe.py --other-mailbox someone.else@tagsolutions.com
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

from agent.calendar import local_zone, parse_event
from connectors.graph import GraphClient, GraphError

HINTS = {
    401: "Token rejected: check GRAPH_TENANT_ID / GRAPH_CLIENT_ID / GRAPH_CLIENT_SECRET (use the secret VALUE, not its ID).",
    403: "No access: the Exchange role assignment is missing or doesn't cover this mailbox (run Test-ServicePrincipalAuthorization).",
    404: "Mailbox not found: check GRAPH_MAILBOX.",
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--other-mailbox", help="a mailbox that should be BLOCKED, to prove the scope")
    args = parser.parse_args()
    load_dotenv(args.env_file, override=True)

    graph = GraphClient.from_env()
    tz = local_zone()

    try:
        graph.token()
        print("1. Token:            OK")
    except NotConnectedError as e:
        print(f"1. Token:            FAIL  {e}")
        return 1
    except GraphError as e:
        print(f"1. Token:            FAIL  {e}\n   {HINTS.get(e.status, '')}")
        return 1

    start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        raw = graph.calendar_view(start, start + timedelta(days=args.days))
        print(f"2. Dave's calendar:  OK  ({len(raw)} events in the next {args.days} days)")
        for event in [parse_event(r, tz) for r in raw][:5]:
            print(f"     {event.start:%a %b %d %I:%M %p}  {event.subject}")
    except GraphError as e:
        print(f"2. Dave's calendar:  FAIL  {e}\n   {HINTS.get(e.status, '')}")
        return 1

    if args.other_mailbox:
        other = GraphClient(graph.tenant_id, graph.client_id, graph.client_secret, args.other_mailbox,
                            token_provider=graph.token_provider)
        try:
            other.calendar_view(start, start + timedelta(days=1))
            print(f"3. Scope check:      FAIL  could read {args.other_mailbox}; access is NOT limited to Dave")
            return 1
        except GraphError as e:
            ok = e.status in (403, 404)
            print(f"3. Scope check:      {'OK  blocked' if ok else 'UNCLEAR'} ({e.status}) for {args.other_mailbox}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
