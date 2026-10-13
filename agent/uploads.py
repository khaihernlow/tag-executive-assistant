"""Files Dave attaches in chat, turned into what the model can take in.

HatzAI's Messages gateway (the route that runs our tools) accepts images but
rejects document blocks (502 for PDFs and even plain text documents; checked
Oct 2026), and its own file store only works on the route without our tools.
So code reads documents to text here, as it does for email attachments, and
the model gets that text. Images go as images; a scanned PDF with no text
goes as the pictures of its pages.
"""

from __future__ import annotations

import base64
import io
from typing import Any

from agent.documents import UnreadableDocument, extract_text

MAX_BYTES = 25 * 1024 * 1024
MAX_TEXT = 40000            # characters of a document the model reads (about 10k tokens)
MAX_SIDE = 1568             # longest side of an image sent to the model (its own working size)
MAX_SCAN_PAGES = 5
IMAGE_TYPES = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "tif", "tiff"}


class UploadError(ValueError):
    pass


def _ext(name: str) -> str:
    return name.lower().rsplit(".", 1)[-1] if "." in name else ""


def _image(data: bytes, max_side: int = MAX_SIDE) -> tuple[bytes, str]:
    from PIL import Image, ImageOps

    try:
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img)   # phone photos come sideways otherwise
    except Exception as e:  # noqa: BLE001 - Pillow raises many types on bad files
        raise UploadError("This image couldn't be opened.") from e
    if img.mode not in ("RGB", "L"):
        background = Image.new("RGB", img.size, "white")
        background.paste(img.convert("RGBA"), mask=img.convert("RGBA").split()[-1])
        img = background
    img.thumbnail((max_side, max_side))
    out = io.BytesIO()
    img.convert("RGB").save(out, "JPEG", quality=85, optimize=True)
    return out.getvalue(), f"{img.width}×{img.height}"


def _image_block(jpeg: bytes) -> dict[str, Any]:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.b64encode(jpeg).decode()}}


def _thumb(data: bytes) -> str:
    small, _ = _image(data, 160)
    return "data:image/jpeg;base64," + base64.b64encode(small).decode()


def _scanned_pages(data: bytes) -> list[bytes]:
    """The page pictures of a scanned PDF (a scan is one picture per page)."""
    from pypdf import PdfReader

    pages = []
    try:
        for page in PdfReader(io.BytesIO(data)).pages[:MAX_SCAN_PAGES]:
            images = list(page.images)
            if images:
                largest = max(images, key=lambda i: len(i.data))
                pages.append(largest.data)
    except Exception:  # noqa: BLE001
        return []
    return pages


def prepare(data: bytes, name: str, content_type: str = "") -> dict[str, Any]:
    """{"kind", "summary", "thumb", "blocks"}: blocks are what the model receives,
    the rest is what the chat shows. Raises UploadError with a message for Dave."""
    name = (name or "file").strip()
    if not data:
        raise UploadError(f"{name} is empty.")
    if len(data) > MAX_BYTES:
        raise UploadError(f"{name} is over {MAX_BYTES // (1024 * 1024)} MB.")
    ext, ct = _ext(name), (content_type or "").lower()

    if ext in ("heic", "heif") or "heic" in ct or "heif" in ct:
        raise UploadError("iPhone HEIC photos can't be read here. Share it as a JPEG (most apps do this when "
                          "you attach from Photos), or take a screenshot of it.")
    if ext in IMAGE_TYPES or ct.startswith("image/"):
        jpeg, size = _image(data)
        return {"kind": "image", "summary": f"Image · {size}", "thumb": _thumb(data), "blocks": [
            {"type": "text", "text": f"[Attached image: {name}]"}, _image_block(jpeg)]}

    if ext in ("doc", "xls", "ppt"):
        raise UploadError(f"{name} is in the older Office format. Save it as .{ext}x (File, Save As) and attach that.")
    try:
        text = extract_text(data, name, content_type, max_chars=MAX_TEXT)
    except UnreadableDocument as e:
        if str(e).startswith("Can't read"):
            raise UploadError(f"{name} isn't a kind of file I can read. Attach a PDF, Word, Excel or "
                              "PowerPoint file, text, or a picture.") from e
        # A PDF with no text is usually a scan: send its pages as pictures.
        if (ext == "pdf" or ct == "application/pdf") and (pages := _scanned_pages(data)):
            blocks = [{"type": "text", "text": f"[Attached PDF: {name}. It's a scan; here are its first "
                                               f"{len(pages)} page(s) as images.]"}]
            for page in pages:
                try:
                    blocks.append(_image_block(_image(page)[0]))
                except UploadError:
                    continue
            if len(blocks) > 1:
                return {"kind": "document", "summary": f"Scanned PDF · {len(blocks) - 1} page(s)",
                        "thumb": None, "blocks": blocks}
        raise UploadError(str(e)) from e
    kind_label = {"pdf": "PDF", "docx": "Word", "xlsx": "Excel", "pptx": "PowerPoint", "csv": "CSV"}.get(ext, "Text")
    truncated = text.endswith("[...truncated]")
    words = len(text.split())
    summary = f"{kind_label} · {words:,} word{'' if words == 1 else 's'}" + (" (first part)" if truncated else "")
    return {"kind": "document", "summary": summary, "thumb": None, "blocks": [
        {"type": "text", "text": f"[Attached file: {name} ({summary})]\n{text}"}]}
