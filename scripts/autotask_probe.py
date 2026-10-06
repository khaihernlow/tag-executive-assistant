"""Phase 0 probe: what can the assistant's Autotask API user see?

Read-only. Checks the credentials, reads one record of each entity the
assistant will use, and finds Dave's resource id (opportunities are owned
by a resource). It never creates anything: Autotask's entityInformation
only says what the API supports, not what this user's security level
allows, so write access is checked in the Autotask UI instead.

Usage:
  python scripts/autotask_probe.py
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

from connectors.autotask import AutotaskAPIError, AutotaskClient

# Entity -> why the assistant needs it
ENTITIES = {
    "Companies": "client/prospect lookups",
    "Contacts": "people at clients",
    "Opportunities": "create/track opportunities",
    "Quotes": "quotes on opportunities",
    "Resources": "TAG staff (owners, assignees)",
    "TimeEntries": "time-entry oversight",
    "Tickets": "client issues, review requests",
    "Contracts": "renewals",
    "Projects": "onboarding projects",
    "Tasks": "onboarding tasks",
    "Appointments": "contractual client meetings",
    "CompanyNotes": "meeting notes on accounts",
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args()
    load_dotenv(args.env_file, override=True)

    try:
        at = AutotaskClient.from_env()
        print(f"Zone: {at.discover_zone()}")
    except (ValueError, AutotaskAPIError) as e:
        print(f"Setup failed: {e}")
        return 1

    print(f"\n{'Entity':14} {'read':6} {'API create/update':18} purpose")
    any_failed = False
    for entity, purpose in ENTITIES.items():
        try:
            at.query(entity, [{"op": "exist", "field": "id"}], include_fields=["id"], max_records=1)
            read = "OK"
        except AutotaskAPIError as e:
            read = f"FAIL ({e.status})"
            any_failed = True
        try:
            info = at.entity_information(entity)
            caps = f"{'C' if info.get('canCreate') else '-'}/{'U' if info.get('canUpdate') else '-'}"
        except AutotaskAPIError:
            caps = "?"
        print(f"{entity:14} {read:6} {caps:18} {purpose}")

    mailbox = os.environ.get("GRAPH_MAILBOX", "")
    if mailbox:
        try:
            matches = at.query("Resources", [{"op": "eq", "field": "email", "value": mailbox}],
                               include_fields=["id", "firstName", "lastName", "email", "isActive"])
            if matches:
                r = matches[0]
                print(f"\nDave's resource: id={r['id']} {r.get('firstName')} {r.get('lastName')} active={r.get('isActive')}")
                print(f"-> add AUTOTASK_DAVE_RESOURCE_ID={r['id']} to .env")
            else:
                print(f"\nNo Autotask resource with email {mailbox}")
        except AutotaskAPIError as e:
            print(f"\nResource lookup failed: {e}")

    print("\nC/U = the API supports create/update for that entity. Whether THIS user may create "
          "depends on its security level (Autotask > Admin > Resources/Users > the API user).")
    return 1 if any_failed else 0


if __name__ == "__main__":
    sys.exit(main())
