import io
import zipfile

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from agent.assistant import _ground_truth, trim_history
from agent.documents import extract_text
from agent.uploads import UploadError, prepare
from store.db import Store


def zipped(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    return buf.getvalue()


def png(width=3000, height=1000, mode="RGB") -> bytes:
    buf = io.BytesIO()
    Image.new(mode, (width, height), "white").save(buf, "PNG")
    return buf.getvalue()


def test_excel_reads_each_sheet_as_rows():
    data = zipped({
        "xl/workbook.xml": '<workbook><sheets><sheet name="Quotes" sheetId="1"/><sheet name="Notes" sheetId="2"/></sheets></workbook>',
        "xl/sharedStrings.xml": "<sst><si><t>Client</t></si><si><t>Monthly</t></si><si><t>Casella &amp; Co</t></si></sst>",
        "xl/worksheets/sheet1.xml": '<worksheet><sheetData><row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
                                    '<row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2"><v>2661.58</v></c></row>'
                                    '<row r="3"><c r="A3"/></row></sheetData></worksheet>',
        "xl/worksheets/sheet2.xml": '<worksheet><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>Renew in Nov</t></is></c></row>'
                                    "</sheetData></worksheet>",
    })
    assert extract_text(data, "q.xlsx") == "## Quotes\nClient | Monthly\nCasella & Co | 2661.58\n\n## Notes\nRenew in Nov"


def test_powerpoint_reads_slides_in_order():
    slide = '<p:sld><a:p><a:r><a:t>{}</a:t></a:r></a:p><a:p><a:r><a:t>{}</a:t></a:r></a:p></p:sld>'
    data = zipped({"ppt/slides/slide10.xml": slide.format("Pricing", "$135 per seat"),
                   "ppt/slides/slide2.xml": slide.format("Why TAG", "24/7 help desk")})
    assert extract_text(data, "deck.pptx") == "## Slide 1\nWhy TAG\n24/7 help desk\n\n## Slide 2\nPricing\n$135 per seat"


def test_a_document_becomes_text_the_model_reads():
    out = prepare(b"Invoice 4471\nTotal: $1,280.50\n", "invoice.txt", "text/plain")
    assert out["kind"] == "document" and out["thumb"] is None
    assert out["summary"] == "Text · 4 words"
    [block] = out["blocks"]
    assert block["type"] == "text" and block["text"].startswith("[Attached file: invoice.txt (Text · 4 words)]")
    assert "Total: $1,280.50" in block["text"]


def test_an_image_is_shrunk_to_the_models_size_and_gets_a_thumbnail():
    out = prepare(png(), "whiteboard.png", "image/png")
    assert out["kind"] == "image" and out["summary"] == "Image · 1568×523"
    assert out["thumb"].startswith("data:image/jpeg;base64,")
    label, image = out["blocks"]
    assert label == {"type": "text", "text": "[Attached image: whiteboard.png]"}
    assert image["type"] == "image" and image["source"]["media_type"] == "image/jpeg"
    # A transparent screenshot doesn't turn black
    assert prepare(png(100, 100, "RGBA"), "shot.png", "image/png")["kind"] == "image"


def test_files_it_cannot_read_say_why():
    with pytest.raises(UploadError, match="HEIC"):
        prepare(b"....", "IMG_0042.HEIC", "image/heic")
    with pytest.raises(UploadError, match="isn't a kind of file I can read"):
        prepare(b"PK...", "archive.zip", "application/zip")
    with pytest.raises(UploadError, match=r"older Office format\. Save it as \.docx"):
        prepare(b"\xd0\xcf\x11\xe0", "proposal.doc", "application/msword")
    with pytest.raises(UploadError, match="empty"):
        prepare(b"", "blank.pdf", "application/pdf")


def test_a_scanned_pdf_goes_as_pictures_of_its_pages():
    buf = io.BytesIO()
    Image.new("RGB", (800, 1000), "white").save(buf, "PDF")
    out = prepare(buf.getvalue(), "signed-contract.pdf", "application/pdf")
    assert out["summary"] == "Scanned PDF · 1 page(s)"
    assert [b["type"] for b in out["blocks"]] == ["text", "image"]


def test_a_message_with_files_still_counts_as_daves_turn():
    with_file = {"role": "user", "content": [{"type": "text", "text": "[Attached file: q.txt]\nTotal $90"},
                                             {"type": "text", "text": "Is this right?"}]}
    tool_result = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "x"}]}
    history = [{"role": "user", "content": "older"}, {"role": "assistant", "content": "ok"},
               with_file, {"role": "assistant", "content": [{"type": "tool_use", "id": "t", "name": "x", "input": {}}]},
               tool_result, {"role": "assistant", "content": "done"}]
    # Cut at Dave's turns only: the tool_use and its result stay together
    assert trim_history(history, max_turns=1) == history[2:]
    # And what he attached vouches for the numbers in the reply
    assert any("Total $90" in t for t in _ground_truth(history))


def test_upload_then_ask_about_it(monkeypatch):
    monkeypatch.setenv("REQUIRE_AUTH", "False")
    from types import SimpleNamespace
    from app.main import app

    store = Store(":memory:")
    seen = {}

    class Assistant:
        def chat(self, text, conversation_id, topic=None, attachments=None):
            seen["files"] = [store.get_upload(a)["name"] for a in attachments]
            return {"conversation_id": "c1", "text": "ok", "cards": []}

    app.state.services = SimpleNamespace(store=store, assistant=Assistant())
    try:
        http = TestClient(app)
        ok = http.post("/api/uploads", files={"file": ("notes.txt", b"Saati Deli, 4 salads", "text/plain")})
        assert ok.status_code == 200 and ok.json()["summary"] == "Text · 4 words"
        bad = http.post("/api/uploads", files={"file": ("photo.heic", b"x", "image/heic")})
        assert bad.status_code == 400 and "HEIC" in bad.json()["detail"]
        reply = http.post("/api/chat", json={"message": "", "attachments": [ok.json()["id"]]})
        assert reply.status_code == 200 and seen["files"] == ["notes.txt"]
    finally:
        app.state.services = None


def test_amounts_read_off_a_picture_are_not_called_unverified():
    from agent.assistant import _has_image
    picture = {"role": "user", "content": [{"type": "image", "source": {}}, {"type": "text", "text": "Total?"}]}
    assert _has_image([picture, {"role": "assistant", "content": "It's $96.40."}])
    assert not _has_image([{"role": "user", "content": "Total?"},
                           {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "x"}]}])
