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


def extract_text(data: bytes, name: str, content_type: str = "", max_chars: int = MAX_CHARS) -> str:
    kind = _kind(name, content_type)
    if kind == "pdf":
        text = _pdf(data)
    elif kind == "docx":
        text = _docx(data)
    elif kind == "xlsx":
        text = _xlsx(data)
    elif kind == "pptx":
        text = _pptx(data)
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
    return text[:max_chars] + ("\n[...truncated]" if len(text) > max_chars else "")


def _kind(name: str, content_type: str) -> str:
    ext = name.lower().rsplit(".", 1)[-1] if "." in name else ""
    ct = content_type.lower()
    if ext == "pdf" or ct == "application/pdf":
        return "pdf"
    if ext == "docx" or "wordprocessingml" in ct:
        return "docx"
    if ext == "xlsx" or "spreadsheetml" in ct:
        return "xlsx"
    if ext == "pptx" or "presentationml" in ct:
        return "pptx"
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


def _xml_text(xml: str) -> str:
    return unescape(re.sub(r"<[^>]+>", "", xml))


def _numbered(names: list[str], pattern: str) -> list[str]:
    found = [n for n in names if re.match(pattern, n)]
    return sorted(found, key=lambda n: int(re.search(r"(\d+)\.xml$", n).group(1)))


def _xlsx(data: bytes) -> str:
    """Each sheet as rows of cells split by " | " (values as Excel last calculated them)."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
            shared = []
            if "xl/sharedStrings.xml" in names:
                shared_xml = zf.read("xl/sharedStrings.xml").decode("utf-8", "replace")
                shared = [_xml_text(si) for si in re.findall(r"<si>(.*?)</si>", shared_xml, re.S)]
            workbook = zf.read("xl/workbook.xml").decode("utf-8", "replace")
            titles = re.findall(r'<sheet [^>]*name="([^"]*)"', workbook)
            out = []
            for i, sheet in enumerate(_numbered(names, r"xl/worksheets/sheet\d+\.xml$")):
                xml = zf.read(sheet).decode("utf-8", "replace")
                rows = []
                for row in re.findall(r"<row[^>]*>(.*?)</row>", xml, re.S):
                    cells = []
                    for attrs, body in re.findall(r"<c([^>]*?)(?:/>|>(.*?)</c>)", row, re.S):
                        value = re.search(r"<v>(.*?)</v>", body or "", re.S)
                        inline = re.search(r"<is>(.*?)</is>", body or "", re.S)
                        if 't="s"' in attrs and value:
                            index = int(value.group(1))
                            cells.append(shared[index] if index < len(shared) else "")
                        elif inline:
                            cells.append(_xml_text(inline.group(1)))
                        else:
                            cells.append(unescape(value.group(1)) if value else "")
                    if any(c.strip() for c in cells):
                        rows.append(" | ".join(cells).rstrip(" |"))
                title = unescape(titles[i]) if i < len(titles) else f"Sheet {i + 1}"
                out.append(f"## {title}\n" + "\n".join(rows))
            return "\n\n".join(out)
    except (zipfile.BadZipFile, KeyError, ValueError) as e:
        raise UnreadableDocument("Excel file could not be read.") from e


def _pptx(data: bytes) -> str:
    """Each slide's text, in slide order."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            out = []
            for i, slide in enumerate(_numbered(zf.namelist(), r"ppt/slides/slide\d+\.xml$"), 1):
                xml = zf.read(slide).decode("utf-8", "replace")
                paragraphs = [_xml_text(p) for p in re.findall(r"<a:p>(.*?)</a:p>", xml, re.S)]
                out.append(f"## Slide {i}\n" + "\n".join(p for p in paragraphs if p.strip()))
            return "\n\n".join(out)
    except (zipfile.BadZipFile, KeyError) as e:
        raise UnreadableDocument("PowerPoint file could not be read.") from e
