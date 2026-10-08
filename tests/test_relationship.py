import pytest

from agent import relationship
from agent.relationship import automated, clean_preview, describe, thread_deleted_before
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


def test_thread_deleted_before_only_counts_earlier_messages_in_deleted_items():
    message = {"id": "m2", "conversationId": "t1", "receivedDateTime": "2026-10-08T14:00:00Z"}
    assert thread_deleted_before(FakeGraph([{"id": "m1", "parentFolderId": "deleted-id"}]), message)
    assert not thread_deleted_before(FakeGraph([{"id": "m1", "parentFolderId": "inbox-id"}]), message)
    assert not thread_deleted_before(FakeGraph(), {"id": "m3"})  # no thread: nothing to check
