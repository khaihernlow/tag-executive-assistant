"""The names a meeting recording is likely to contain, so speech-to-text spells them right.

Azure Speech misheard "Peconic Landing" as "Pecanic Landing" and "Mitel" as "middle". Two fixes use
the same list: Azure is told to expect these phrases while it transcribes (a phrase list), and the
write-up gets them too, to report any it still misheard (agent.notes applies those in code, only when
the corrected words are on this list).

Most specific first, since Azure works best with a few hundred phrases: the meeting itself (title,
the people at it, their companies), then TAG's staff, the people Dave deals with most, the vendors
TAG sells, and the companies on opportunities in the past year.
"""

from __future__ import annotations

import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

MAX_PHRASES = 450
CACHE_SECONDS = 12 * 3600
# What TAG sells and supports; the products come up by name in client meetings
VENDORS = [
    "Mitel", "ShoreTel", "RingCentral", "Microsoft Teams", "Teams Phone", "Microsoft 365", "Copilot", "Azure", "Intune",
    "Entra", "SharePoint", "OneDrive", "Exchange Online", "Cisco Meraki", "Meraki", "Ubiquiti", "UniFi", "SonicWall",
    "Fortinet", "FortiGate", "Palo Alto", "SentinelOne", "Blackpoint", "Huntress", "Sophos", "CrowdStrike", "KnowBe4",
    "Duo", "Datto", "Veeam", "Cyberstone", "Autotask", "HubSpot", "ConnectWise", "Kaseya", "Dell", "Lenovo", "HP",
    "Proofpoint", "Mimecast", "Barracuda", "Zix", "Egnyte", "Dropbox", "QuickBooks", "Salesforce", "Zoom", "Webex",
    "vCIO", "vCISO", "MSP", "MSSP", "SOC", "MDR", "EDR", "XDR", "SIEM", "VoIP", "SIP trunk", "UCaaS", "PBX",
    "TAG Solutions", "TeamsMaestro", "Hatz", "Geronimo",
]


def _clean(phrases: list[str]) -> list[str]:
    seen, out = set(), []
    for p in phrases:
        p = re.sub(r"\s+", " ", (p or "").strip(" ,.-"))
        p = re.sub(r",?\s+(inc|llc|l\.l\.c|corp|co|ltd|pllc|pc)\.?$", "", p, flags=re.I)
        if 2 <= len(p) <= 60 and "@" not in p and p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return out


class Vocabulary:
    def __init__(self, graph: Any = None, directory: Any = None):
        self.graph, self.directory = graph, directory
        self._general: list[str] = []
        self._at = 0.0
        self._lock = threading.Lock()

    def general(self) -> list[str]:
        """Staff, Dave's frequent contacts, vendors, recent opportunity companies; cached for half a day."""
        with self._lock:
            if self._general and time.time() - self._at < CACHE_SECONDS:
                return self._general
            names: list[str] = []
            if self.graph is not None:
                try:
                    domain = self.graph.mailbox.split("@")[-1].lower()
                    for u in self.graph.get_all("/users", {"$select": "displayName,mail,accountEnabled", "$top": 999}, limit=2000):
                        if u.get("accountEnabled", True) and (u.get("mail") or "").lower().endswith("@" + domain):
                            names.append(u.get("displayName") or "")
                    for p in self.graph.get_all(f"/users/{self.graph.mailbox}/people", {"$top": 100}, limit=100):
                        names.append(p.get("displayName") or "")
                        names.append(p.get("companyName") or "")
                except Exception:  # noqa: BLE001 - fewer hints, not a failed transcription
                    pass
            names += VENDORS
            if self.directory is not None:
                try:
                    since = (datetime.now(timezone.utc) - timedelta(days=365)).strftime("%Y-%m-%dT00:00:00Z")
                    opps = self.directory.at.query("Opportunities", [{"op": "gte", "field": "createDate", "value": since}],
                                                   ["id", "companyID", "createDate"])
                    ids = list(dict.fromkeys(o["companyID"] for o in sorted(opps, key=lambda o: o["createDate"], reverse=True)
                                             if o.get("companyID")))[:200]
                    self.directory.prefetch_companies(ids)
                    names += [self.directory.company_name(i) for i in ids]
                except Exception:  # noqa: BLE001
                    pass
            self._general, self._at = _clean(names), time.time()
            return self._general

    def for_note(self, note: dict[str, Any]) -> list[str]:
        """This meeting's own names first (its title, who was there, their companies), then the general list."""
        specific = [note.get("title") or ""]
        for p in note.get("people") or []:
            specific.append(p.get("name") or "")
            email = (p.get("email") or "").lower()
            if email and self.directory is not None:
                try:
                    specific += [c["name"] for c in self.directory.find_companies(email=email)[:1]]
                except Exception:  # noqa: BLE001
                    pass
        # A meeting title like "Peconic/TAG: Onboarding" also names a company
        specific += [w for w in re.split(r"[/&|:,\-–]| and | with ", note.get("title") or "") if w.strip()]
        return _clean(specific + self.general())[:MAX_PHRASES]
