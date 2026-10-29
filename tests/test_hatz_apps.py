import json

import pytest

from agent.hatz_apps import HatzApps, hatz_tools, upload_path
from store.db import Store

RFP = {"id": "rfp-1", "name": "RFP Automation", "definition_state": "present",
       "files": [{"display_name": "TAG Canned Responses"}],
       "user_inputs": [{"variable_name": "rfp_document", "display_name": "RFP Document", "variable_type": "file_upload",
                        "required": True, "position": 1}],
       "steps": [{"id": "s1", "display_name": "Extractor", "position": 0, "step_type": "prompt"},
                 {"id": "s2", "display_name": "Formatter", "position": 1, "step_type": "prompt"},
                 {"id": "s3", "display_name": "DOCX Generation", "position": None, "step_type": "document_processing"}]}
GENERIC = {"id": "rfp-2", "name": "RFP Responder", "definition_state": "present", "files": [],
           "user_inputs": [{"variable_name": "rfp", "variable_type": "file_upload", "required": True}], "steps": [{"id": "x"}]}
HR = {"id": "hr", "name": "HR Specialist", "definition_state": "present", "files": [], "steps": [],
      "user_inputs": [{"variable_name": "question", "display_name": "Ask", "variable_type": "paragraph", "required": True},
                      {"variable_name": "tone", "variable_type": "dropdown", "variants": [{"value": "formal"}, {"value": "casual"}]}]}


class FakeClient:
    def __init__(self):
        self.uploads, self.runs, self.queries = [], [], []

    def list_apps(self):
        return [RFP, GENERIC, HR, {"id": "gone", "name": "Old RFP", "definition_state": "deleted"}]

    def search_apps(self, name):
        return [a for a in (RFP, GENERIC, HR) if name in a["name"].lower()]

    def get_app(self, app_id):
        raise RuntimeError("404")

    def upload(self, data, name, scope_type, scope_id):
        self.uploads.append((data, name, scope_type, scope_id))
        return "file-9"

    def run_workflow(self, app_id, inputs):
        self.runs.append((app_id, inputs))
        return "job-1"

    def query_app(self, app_id, inputs):
        self.queries.append((app_id, inputs))
        return "Exit interviews are scheduled by HR."

    def job(self, job_id):
        return {"app_id": "rfp-1", "status": "complete", "number_of_steps": 3, "step_outputs": [
            {"step_id": "s2", "status": "complete", "step_type": "prompt", "end_time": 2, "output_data": "## Draft proposal"},
            {"step_id": "s1", "status": "complete", "step_type": "prompt", "end_time": 1, "output_data": "Signals"},
            {"step_id": "s3", "status": "complete", "step_type": "document_processing", "end_time": 3, "output_data": {}}]}


class FakeGraph:
    mailbox = "dave@tag.example"

    def get_all(self, path, params=None, limit=None):
        return [{"id": "att-1", "name": "Albany RFP.pdf", "contentType": "application/pdf"}]

    def get_bytes(self, path):
        assert path.endswith("/messages/msg-1/attachments/att-1/$value")
        return b"%PDF rfp"


def hatz(store=None):
    h = HatzApps(FakeClient(), FakeGraph(), store or Store(":memory:"))
    h.refresh()
    return h


def test_search_finds_by_name_and_shows_whose_it_is_and_what_it_needs():
    found = hatz().find("rfp responder")
    assert [a["name"] for a in found] == ["RFP Responder", "RFP Automation"]
    tag = found[1]
    assert tag["kind"] == "workflow" and tag["built_in_files"] == ["TAG Canned Responses"]
    assert tag["inputs"] == [{"name": "rfp_document", "label": "RFP Document", "type": "file_upload", "required": True}]
    assert tag["steps"] == ["Extractor", "Formatter", "DOCX Generation"]   # a step with no position goes last


def test_an_emails_attachment_goes_to_the_workflow_and_a_card_follows_the_run():
    h = hatz()
    out = hatz_tools(h)[1].handler({"app_id": "rfp-1", "inputs": {"rfp_document": {"email_message_id": "msg-1",
                                                                                   "attachment": "Albany RFP.pdf"}}})
    assert out["kind"] == "workflow" and out["job_id"] == "job-1" and "card" in out["note"]
    assert h.client.uploads == [(b"%PDF rfp", "Albany RFP.pdf", "workflow", "rfp-1")]
    assert h.client.runs == [("rfp-1", {"rfp_document": "file-9"})]


def test_a_file_dave_attached_in_chat_is_sent_as_the_file_itself(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.hatz_apps.UPLOADS", str(tmp_path))
    store = Store(":memory:")
    uid = store.add_upload("Scope.docx", "document", "Word", None, [])
    upload_path(uid).write_bytes(b"docx bytes")
    h = hatz(store)
    h.start("rfp-1", {"rfp_document": {"chat_file": "scope.docx"}})
    assert h.client.uploads[0][:2] == (b"docx bytes", "Scope.docx")


def test_inputs_are_checked_before_anything_is_sent():
    h = hatz()
    with pytest.raises(ValueError, match="has no input rfp"):
        h.start("rfp-1", {"rfp": "x"})
    with pytest.raises(ValueError, match="needs: RFP Document"):
        h.start("rfp-1", {})
    with pytest.raises(ValueError, match="must be one of: formal, casual"):
        h.start("hr", {"question": "hi", "tone": "angry"})
    with pytest.raises(ValueError, match="No Hatz app"):
        h.start("gone", {})
    assert not h.client.uploads and not h.client.runs and not h.client.queries


def test_an_app_answers_straight_away():
    out = hatz().start("hr", {"question": "What is an exit interview?"})
    assert out == {"app": "HR Specialist", "kind": "app", "output": "Exit interviews are scheduled by HR."}


def test_a_finished_run_gives_its_last_text_every_step_and_its_documents():
    job = hatz().job("job-1")
    assert job["status"] == "complete" and job["done"] == 3 and job["steps"] == 3
    assert job["result"] == "## Draft proposal"
    assert [s["name"] for s in job["all_steps"]] == ["Extractor", "Formatter"]
    assert job["documents"] == [{"step_id": "s3", "name": "DOCX Generation"}]


def test_the_list_is_kept_so_a_restart_doesnt_wait_minutes_for_it():
    store = Store(":memory:")
    hatz(store)
    again = HatzApps(FakeClient(), FakeGraph(), store)
    assert [a["id"] for a in again._apps] == ["rfp-1", "rfp-2", "hr"]


def test_before_the_list_is_in_a_search_asks_hatz_by_name():
    h = HatzApps(FakeClient(), FakeGraph(), Store(":memory:"))
    h.refresh_later = lambda: None
    assert [a["name"] for a in h.find("specialist")] == ["HR Specialist"]
    # and what it found can be run straight away, before the full list arrives
    assert h.start("hr", {"question": "hi"})["kind"] == "app"


def test_a_steps_words_are_taken_out_of_the_model_reply_hatz_stores():
    from agent.hatz_apps import step_text
    wrapped = json.dumps({"choices": [{"message": {"content": "## Standalone\n\nNo NgModule.", "role": "assistant"}}],
                          "model": "x"})
    assert step_text(wrapped) == "## Standalone\n\nNo NgModule."
    assert step_text("Plain words") == "Plain words"
    assert step_text('{"text": "Scraped page"}') == "Scraped page"
    assert step_text({"file": "x.docx"}) == ""
