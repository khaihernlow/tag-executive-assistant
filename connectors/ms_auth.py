"""Delegated Microsoft sign-in: one interactive sign-in, then a renewable token.

Who signs in (GRAPH_SIGNIN_AS) can differ from whose mailbox the assistant
works in (GRAPH_MAILBOX). The assistant's own account (assistant@) has
delegate access to Dave's mailbox and calendar, so it signs in as itself and
every call names Dave's mailbox (/users/{Dave}/...). Left unset, it signs in
as the mailbox owner (the original setup, Dave himself).

`scripts/connect_microsoft.py` does the one-time interactive sign-in and
writes the MSAL token cache to disk (gitignored). After that,
`DelegatedTokenProvider` renews access tokens silently from that cache.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import msal

# Everything the assistant could use across all phases, consented in one go:
# TAG's tenant requires admin consent, so every later addition means another
# trip to an admin. Delegated access never exceeds what Dave himself can
# reach; what the assistant actually does is enforced in code (action policy).
# Must match the app registration's API permissions exactly, or sign-in
# falls back to the "Approval required" screen.
# (offline_access/openid/profile are added by MSAL automatically.)
SCOPES = [
    # identity & directory lookups ("set up a meeting with Kai")
    "User.Read",
    "User.Read.All",
    "People.Read",
    "People.Read.All",   # Dave's people list when signed in as the assistant
    "GroupMember.Read.All",
    "Presence.Read.All",
    "Place.Read.All",
    # mail: triage, folders, inbox rules, drafts, sending, shared/delegate mailboxes
    "Mail.ReadWrite",
    "Mail.ReadWrite.Shared",
    "Mail.Send",
    "Mail.Send.Shared",
    "MailboxSettings.ReadWrite",
    # calendar: Dave's own plus calendars shared with him
    "Calendars.ReadWrite",
    "Calendars.ReadWrite.Shared",
    # contacts: clients and prospects
    "Contacts.ReadWrite",
    # Teams: meetings, transcripts/recordings for notes, chats with the team
    "OnlineMeetings.ReadWrite",
    "OnlineMeetingTranscript.Read.All",
    "OnlineMeetingRecording.Read.All",
    "Chat.ReadWrite",
    "ChatMessage.Send",
    # files: OneDrive/SharePoint (financials, mileage spreadsheet)
    "Files.ReadWrite.All",
    "Sites.ReadWrite.All",
    # tasks and notes
    "Tasks.ReadWrite",
    "Notes.ReadWrite",
]


class NotConnectedError(RuntimeError):
    pass


def signin_account() -> str:
    """The account that signs in: GRAPH_SIGNIN_AS, else the mailbox owner."""
    return (os.environ.get("GRAPH_SIGNIN_AS") or os.environ.get("GRAPH_MAILBOX") or "").lower()


def cache_path() -> Path:
    return Path(os.environ.get("MSAL_CACHE_PATH", ".secrets/msal_token_cache.json"))


def load_cache(path: Path) -> msal.SerializableTokenCache:
    cache = msal.SerializableTokenCache()
    if path.exists():
        cache.deserialize(path.read_text(encoding="utf-8"))
    return cache


def save_cache(cache: msal.SerializableTokenCache, path: Path) -> None:
    """Write our tokens without dropping anyone else's.

    Several processes share this file (the app, the worker, the sign-in script).
    Writing our in-memory copy as-is once wiped a sign-in another process had
    just added, so entries already on disk that we don't have are kept."""
    if not cache.has_state_changed:
        return
    ours = json.loads(cache.serialize())
    if path.exists():
        try:
            disk = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            disk = {}
        for section, entries in disk.items():
            if isinstance(entries, dict):
                merged = ours.setdefault(section, {})
                for key, value in entries.items():
                    merged.setdefault(key, value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ours, indent=1), encoding="utf-8")
    cache.has_state_changed = False


def build_app(tenant_id: str, client_id: str, cache: msal.SerializableTokenCache) -> msal.PublicClientApplication:
    if not tenant_id or not client_id:
        raise ValueError("GRAPH_TENANT_ID and GRAPH_CLIENT_ID are required.")
    return msal.PublicClientApplication(
        client_id,
        authority=f"https://login.microsoftonline.com/{tenant_id}",
        token_cache=cache,
    )


class DelegatedTokenProvider:
    def __init__(self, tenant_id: str, client_id: str, account: str, path: Path | None = None) -> None:
        self.account = account.lower()
        self.path = path or cache_path()
        self.cache = load_cache(self.path)
        self.app = build_app(tenant_id, client_id, self.cache)

    def _accounts(self) -> list[dict]:
        return [a for a in self.app.get_accounts() if a.get("username", "").lower() == self.account]

    def __call__(self) -> str:
        accounts = self._accounts()
        if not accounts and self.path.exists():
            # Signed in by another process since we loaded the file (the sign-in script).
            self.cache.deserialize(self.path.read_text(encoding="utf-8"))
            accounts = self._accounts()
        if not accounts:
            raise NotConnectedError(
                f"No saved Microsoft sign-in for {self.account}. Run: python scripts/connect_microsoft.py"
            )
        result = self.app.acquire_token_silent(SCOPES, account=accounts[0])
        # MSAL may have rotated the refresh token; persist it either way.
        save_cache(self.cache, self.path)
        if not result or "access_token" not in result:
            detail = (result or {}).get("error_description", "refresh token expired or revoked")
            raise NotConnectedError(f"Microsoft sign-in needs renewing ({detail}). Run: python scripts/connect_microsoft.py")
        return result["access_token"]
