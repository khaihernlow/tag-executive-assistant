import pytest

from agent import relationship
from agent.relationship import (
    automated, clean_preview, describe, relationship_label, thread_deleted_before, thread_history,
)
from store.db import Store

DAVE = "dave@tag.example"


@pytest.fixture(autouse=True)
def env():
    relationship._deleted_folder.clear()


class FakeGraph:
    mailbox = DAVE

    def __init__(self, earlier=()):
        self.earlier = list(earlier)

    def get(self, path, params=None, headers=None):
        return {"id": "deleted-id"}

    def get_all(self, path, params=None, limit=500, headers=None):
        return self.earlier


def test_clean_preview_drops_gateway_banners_and_links():
    text = ("[This email is from an external sender] New sender. Do you want to Trust or Silence them? Trust Silence "
            "Hi Dave, <https://track.example/x> a quick call?")
    assert clean_preview(text) == "Hi Dave, a quick call?"


@pytest.mark.parametrize("text, expected", [
    ("Reply 'Stop' if you'd rather not hear from me", True),
    ("Click here to unsubscribe", True),
    ("If you prefer not to receive these emails...", True),
    ("Could we meet Thursday about the renewal?", False),
])
def test_automated_sequence_text(text, expected):
    assert automated(text) is expected


def test_describe_knows_staff_correspondents_kept_junked_and_strangers():
    store = Store(":memory:")
    store.set_sender("kept@news.example", "keep", "filed")
    store.set_sender("rep@pitch.example", "junk", "junk_folder")
    known = {"client@client.example"}
    kinds = [describe(a, known, store, "tag.example")["kind"] for a in (
        "Kai@tag.example", "client@client.example", "kept@news.example", "rep@pitch.example", "new@prospect.example")]
    assert kinds == ["staff", "correspondent", "kept", "junk", "first_contact"]


def earlier(address, folder="inbox-id", received="2026-10-01T12:00:00Z", name=""):
    return {"id": f"m-{address}-{received}", "parentFolderId": folder, "receivedDateTime": received,
            "from": {"emailAddress": {"address": address, "name": name}}}


MESSAGE = {"id": "m2", "conversationId": "t1", "receivedDateTime": "2026-10-08T14:00:00Z",
           "from": {"emailAddress": {"address": "rep@pitch.example"}}}


def test_deleted_before_means_this_sender_was_already_turned_down():
    assert thread_deleted_before(FakeGraph([earlier("rep@pitch.example", "deleted-id")]), MESSAGE)
    assert not thread_deleted_before(FakeGraph([earlier("rep@pitch.example")]), MESSAGE)
    assert not thread_deleted_before(FakeGraph([earlier("someone@else.example", "deleted-id")]), MESSAGE)
    assert not thread_deleted_before(FakeGraph(), {"id": "m3"})  # no thread: nothing to check


def test_a_thread_tag_is_part_of_is_never_a_turned_down_pitch():
    # A colleague's intro that Dave was copied on and deleted, then Dave replied: an ongoing deal.
    graph = FakeGraph([earlier("rj@tag.example", "deleted-id", "2026-10-01T12:00:00Z", "Riley Jones"),
                       earlier(DAVE, "sent-id", "2026-10-03T12:00:00Z")])
    history = thread_history(graph, {**MESSAGE, "from": {"emailAddress": {"address": "cfo@bank.example"}}}, "tag.example")
    assert not history["deleted_before"]
    assert relationship_label({"label": "first contact"}, history) == "Riley Jones (TAG) started this thread Oct 1, you replied Oct 3"
