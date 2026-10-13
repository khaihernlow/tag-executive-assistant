"""One-time Microsoft sign-in (as the assistant's account, or as Dave). Saves a renewable token locally.

Signs in as GRAPH_SIGNIN_AS (e.g. assistant@tagsolutions.com), else as GRAPH_MAILBOX,
then checks the signed-in account can reach GRAPH_MAILBOX's mail and calendar.

Usage:
  python scripts/connect_microsoft.py                # opens a browser sign-in
  python scripts/connect_microsoft.py --device-code  # prints a code to enter at microsoft.com/devicelogin
                                                     # (use this to pick the exact browser that's signed in as Dave)
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests
from dotenv import load_dotenv

from connectors.ms_auth import SCOPES, build_app, cache_path, load_cache, save_cache, signin_account


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--device-code", action="store_true")
    args = parser.parse_args()
    load_dotenv(args.env_file, override=True)

    mailbox = os.environ.get("GRAPH_MAILBOX", "")
    account = signin_account()
    path = cache_path()
    cache = load_cache(path)
    app = build_app(os.environ.get("GRAPH_TENANT_ID", ""), os.environ.get("GRAPH_CLIENT_ID", ""), cache)

    if args.device_code:
        flow = app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            print(f"Device code sign-in unavailable: {flow.get('error_description', flow)}")
            print("Turn on 'Allow public client flows' in the app registration, or run without --device-code.")
            return 1
        print(flow["message"])
        result = app.acquire_token_by_device_flow(flow)
    else:
        print(f"Opening a browser for Microsoft sign-in. Sign in as {account}.")
        result = app.acquire_token_interactive(SCOPES, login_hint=account or None, prompt="select_account")

    if "access_token" not in result:
        print(f"Sign-in failed: {result.get('error')}: {result.get('error_description')}")
        if "AADSTS65001" in str(result) or "AADSTS90094" in str(result):
            print("-> The tenant requires admin approval for these permissions.")
        return 1

    me = requests.get(
        "https://graph.microsoft.com/v1.0/me?$select=displayName,mail,userPrincipalName",
        headers={"Authorization": f"Bearer {result['access_token']}"},
        timeout=30,
    ).json()
    signed_in = (me.get("mail") or me.get("userPrincipalName") or "").lower()
    save_cache(cache, path)
    print(f"Connected as {me.get('displayName')} <{signed_in}>. Token saved to {path}")

    if account and signed_in != account:
        print(f"WARNING: expected to sign in as {account} but signed in as {signed_in}. Sign in again with the right account.")
        return 1
    if mailbox and signed_in != mailbox.lower():
        # Delegate setup: prove the access Dave's mailbox needs, read-only.
        headers = {"Authorization": f"Bearer {result['access_token']}"}
        for label, path in (("mail", f"/users/{mailbox}/mailFolders/inbox/messages?$top=1&$select=subject"),
                            ("calendar", f"/users/{mailbox}/calendar/events?$top=1&$select=subject"),
                            ("people", f"/users/{mailbox}/people?$top=1")):
            r = requests.get("https://graph.microsoft.com/v1.0" + path, headers=headers, timeout=30)
            print(f"  {mailbox} {label}: {'ok' if r.ok else f'FAILED {r.status_code} {r.text[:200]}'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
