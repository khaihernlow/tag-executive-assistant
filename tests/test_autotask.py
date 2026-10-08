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


def test_query_pages_by_id_until_a_short_page(monkeypatch):
    monkeypatch.setattr("connectors.autotask.PAGE_SIZE", 2)
    at, session = client([
        Resp(200, {"items": [{"id": 1}, {"id": 5}]}),
        Resp(200, {"items": [{"id": 7}, {"id": 9}]}),
        Resp(200, {"items": [{"id": 12}]}),
    ])
    rows = at.query("Companies", [{"op": "eq", "field": "isActive", "value": True}], ["companyName"])
    assert [r["id"] for r in rows] == [1, 5, 7, 9, 12]
    first, second, third = (r[2] for r in session.requests)
    assert all(r[:2] == ("POST", f"{ZONE}/Companies/query") for r in session.requests)
    assert first["filter"] == [{"op": "eq", "field": "isActive", "value": True}] and first["MaxRecords"] == 2
    assert first["IncludeFields"] == ["id", "companyName"]  # id is needed to page
    assert second["filter"][-1] == {"op": "gt", "field": "id", "value": 5}
    assert third["filter"][-1] == {"op": "gt", "field": "id", "value": 9}


def test_query_stops_at_max_records():
    at, session = client([Resp(200, {"items": [{"id": 1}, {"id": 2}]})])
    assert [r["id"] for r in at.query("Companies", [], max_records=2)] == [1, 2]
    assert len(session.requests) == 1 and session.requests[0][2]["MaxRecords"] == 2


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
