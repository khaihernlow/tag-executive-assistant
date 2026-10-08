import pytest

from connectors.autotask import AutotaskAPIError, AutotaskClient


class Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self.ok = status < 400
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, method, url, headers=None, timeout=None, **kwargs):
        self.requests.append((method, url, kwargs.get("json")))
        return self.responses.pop(0)


ZONE = "https://webservices1.autotask.net/atservicesrest/v1.0"


def client(responses):
    session = FakeSession(responses)
    return AutotaskClient("user", "secret", "code", zone_url=ZONE, session=session), session


def test_query_paginates_and_respects_max_records():
    at, session = client([
        Resp(200, {"items": [{"id": 1}], "pageDetails": {"nextPageUrl": f"{ZONE}/Companies/query/next?x=1"}}),
        Resp(200, {"items": [{"id": 2}, {"id": 3}], "pageDetails": {}}),
    ])
    assert [i["id"] for i in at.query("Companies", [], max_records=2)] == [1, 2]
    assert session.requests[0][:2] == ("POST", f"{ZONE}/Companies/query")
    assert session.requests[0][2]["MaxRecords"] == 2
    # Later pages are POSTed with the same search: Autotask answers GET with 405.
    assert session.requests[1][:2] == ("POST", f"{ZONE}/Companies/query/next?x=1")
    assert session.requests[1][2] == session.requests[0][2]


def test_create_and_update_return_item_id():
    at, session = client([Resp(200, {"itemId": 42}), Resp(200, {"itemId": 42})])
    assert at.create("Opportunities", {"title": "x"}) == 42
    assert at.update("Opportunities", {"id": 42, "title": "y"}) == 42
    assert [r[0] for r in session.requests] == ["POST", "PATCH"]
    assert session.requests[0][1] == f"{ZONE}/Opportunities"


def test_errors_keep_autotask_explanation():
    at, _ = client([Resp(500, {"errors": ["ownerResourceID is required"]})])
    with pytest.raises(AutotaskAPIError, match="ownerResourceID is required") as err:
        at.create("Opportunities", {})
    assert err.value.status == 500


def test_entity_information_returns_info_block():
    at, _ = client([Resp(200, {"info": {"canCreate": True, "canUpdate": False}})])
    assert at.entity_information("Opportunities") == {"canCreate": True, "canUpdate": False}
