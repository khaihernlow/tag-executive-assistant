"""Turn attachment bytes into plain text the model can read.

Code does the extraction (PDF, Word, text, calendar files); the model only
reads the result. Anything we can't read is reported as such, never guessed.
"""

from __future__ import annotations

import io
import re
import zipfile
from html import unescape

MAX_CHARS = 12000


class UnreadableDocument(ValueError):
    pass


def extract_text(data: bytes, name: str, content_type: str = "") -> str:
    kind = _kind(name, content_type)
    if kind == "pdf":
        text = _pdf(data)
    elif kind == "docx":
        text = _docx(data)
    elif kind == "html":
        text = _html(data.decode("utf-8", "replace"))
    elif kind == "text":
        text = data.decode("utf-8", "replace")
    else:
        raise UnreadableDocument(f"Can't read {name or 'this file'} ({content_type or 'unknown type'}).")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        raise UnreadableDocument(f"{name} has no readable text (it may be a scanned image).")
    return text[:MAX_CHARS] + ("\n[...truncated]" if len(text) > MAX_CHARS else "")


def _kind(name: str, content_type: str) -> str:
    ext = name.lower().rsplit(".", 1)[-1] if "." in name else ""
    ct = content_type.lower()
    if ext == "pdf" or ct == "application/pdf":
        return "pdf"
    if ext == "docx" or "wordprocessingml" in ct:
        return "docx"
    if ext in ("htm", "html") or ct == "text/html":
        return "html"
    if ext in ("txt", "csv", "md", "ics", "vcs") or ct.startswith("text/"):
        return "text"
    return ""


def _pdf(data: bytes) -> str:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        return "\n\n".join((page.extract_text() or "") for page in reader.pages[:30])
    except Exception as e:  # noqa: BLE001 - pypdf raises many types on bad files
        raise UnreadableDocument(f"PDF could not be read: {e}") from e


def _docx(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            xml = zf.read("word/document.xml").decode("utf-8", "replace")
    except (zipfile.BadZipFile, KeyError) as e:
        raise UnreadableDocument("Word document could not be read.") from e
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<w:tab/>", "\t", xml)
    return unescape(re.sub(r"<[^>]+>", "", xml))


def _html(html: str) -> str:
    html = re.sub(r"(?is)<(script|style).*?</\1>", "", html)
    html = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>", "\n", html)
    return unescape(re.sub(r"<[^>]+>", "", html))
