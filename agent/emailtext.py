"""What an email actually says, without the wrapping around it.

Every email arrives wrapped: a security banner, a signature with phone and website,
"Get Outlook for iOS", the Teams joining block, earlier messages quoted underneath,
pasted logos. A preview (a brief's cited source, say) should show the words someone
wrote. Rules, not a model: the same email always reads the same way.
"""

from __future__ import annotations

import re

_INVISIBLE = re.compile(r"[​‌‍⁠﻿]")
_BANNER = re.compile(
    r"\[This (?:email|message) (?:is from|has a|was)[^\]]*\]|"
    r"New sender\.\s*Do you want to Trust or Silence them\?\s*(?:Trust\s+Silence)?|"
    r"CAUTION:[^.]*?(?:outside|external)[^.]*\.|"
    r"\[?\s*Meeting notes ✦ Ask AI about this meeting|View full transcript|Notes were taken by AI\.?|"
    r"\[[A-Z][A-Za-z]{1,20}\]", re.I)
# Pasted images: "[A blue and grey logo Description automatically generated]", "[iVBORw0…]", "[cid:…]"
_IMAGES = re.compile(r"\[[^\]]*Description automatically generated[^\]]*\]|\[(?:data:)?[A-Za-z0-9+/=]{40,}\]?|"
                     r"\[(?:cid|image|signature|logo)[^\]]*\]|<https?://[^>\s]*>?|https?://\S+", re.I)
_PHONE_WEB = re.compile(r"\d{3}[\s.‑-]\d{3}[\s.‑-]\d{4}|www\.\S+", re.I)
# Where the quoted, earlier message starts.
_QUOTED = re.compile(r"\bFrom: .{0,200}?\b(?:Sent|When|Date): |-{2,}\s*Original Message\s*-{2,}|"
                     r"\bOn [A-Z].{5,160}? wrote:", re.S)
# A signature: "First Last", the job title, then the company. The title words come from a
# list, so the capitalised words before the name ("notes from Peconic Landing Dave Vener
# President…") are never taken for it.
_TITLE = (r"(?:President|Vice|CEO|CFO|COO|CTO|CIO|vCIO|Chief|\w*Officer|Director|Manager|Engineer|Technical|"
          r"Alignment|Account|Executive|Assistant|Coordinator|Specialist|Sales|Marketing|Operations|Services?|"
          r"Support|Partner|Owner|Founder|Senior|Sr\.?|Lead|Administrator|Analyst|Consultant|Technician|"
          r"Development|Business|Client|Success|Project|Network|Systems|Security|Help|Desk|Office|"
          r"Representative|Strategist|Architect|Head|Team|Principal|Associate|Advisor|Accounting|HR|Human|"
          r"Resources|Field|IT|of|and|&|[-\u2013|,])")
_COMPANY_SIGNATURE = re.compile(r"\b[A-Z][a-z'\-]+\s+[A-Z][a-z'\-]+\s+(?:" + _TITLE + r"\s+)*TAG Solutions,? LLC\b.*$", re.S)
_SIGN_OFF = re.compile(r"(?:^|(?<=[.!?,])\s+|\s)((?:Warm |Kind |Best )?Regards|Best|Thanks|Thank you|Sincerely|Cheers|"
                       r"All the best|Respectfully),\s+([A-Z][\w'\-]*)")
_MOBILE = re.compile(r"\b(?:Get Outlook for (?:iOS|Android)|Sent from my (?:iPhone|iPad|Android)\S*)", re.I)
_TEAMS = re.compile(r"_{5,}.*$", re.S)
FORWARD = re.compile(r"^\s*(?:FW|Fwd?)\s*:\s*", re.I)


def _plain(text: str) -> str:
    text = _INVISIBLE.sub("", text or "")
    text = _BANNER.sub(" ", text)
    text = _IMAGES.sub(" ", text)
    text = _MOBILE.sub(" ", text)
    return " ".join(text.split())


def _without_signature(text: str) -> str:
    text = _TEAMS.sub("", text).strip()
    sig = _COMPANY_SIGNATURE.search(text)
    if sig:
        text = text[: sig.start()].strip()
    # "Regards, Tom Thomas McLaughlin Exec… 555-…": keep "Regards, Tom", drop the block after it.
    # Only the last sign-off, and only when what follows reads as a signature, not more sentences
    # ("Thanks, Maria for setting this up, see you Tuesday." stays whole).
    offs = [m for m in _SIGN_OFF.finditer(text) if m.start(1) > 0]
    if offs:
        m = offs[-1]
        tail = text[m.end(2):]
        if _PHONE_WEB.search(tail) or (len(tail) < 80 and not re.search(r"[.?!]\s|[.?!]$", tail)):
            text = text[: m.end(2)].strip()
    contact = _PHONE_WEB.search(text)
    if contact and not re.search(r"[.?!](?:\s|$)", text[contact.end():]):
        before = text[: contact.start()]
        end = max(before.rfind(". "), before.rfind("! "), before.rfind("? "))
        if end > 0:
            text = text[: end + 1].strip()
    return text


def readable(body: str, subject: str = "") -> str:
    """The words of this email, one line. Empty when there are none (an "Accepted:" reply)."""
    text = _plain(body)
    quoted = _QUOTED.search(text)
    own, earlier = (text[: quoted.start()], text[quoted.start():]) if quoted else (text, "")
    own = _without_signature(own)
    if own or not earlier:
        return own
    # A forward with nothing added: what was forwarded is the content.
    if FORWARD.match(subject or ""):
        original = FORWARD.sub("", subject).strip()
        at = earlier.find(f"Subject: {original}") if original else -1
        rest = earlier[at + len("Subject: ") + len(original):] if at >= 0 else re.sub(
            r"^.*?\bSubject: \S[^.?!]{0,120}?\s(?=[A-Z])", "", earlier, count=1, flags=re.S)
        inner = _QUOTED.search(rest[1:])
        return _without_signature(rest[: inner.start() + 1] if inner else rest)
    return ""


def excerpt(body: str, subject: str = "", limit: int = 600) -> str:
    text = readable(body, subject)
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"
