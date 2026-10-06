from datetime import datetime, timezone

import pytest

from connectors.graph import GraphClient, GraphError


class Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self.ok = status < 400
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, token_resp, pages):
        self.token_resp = token_resp
        self.pages = list(pages)
        self.posts = 0
        self.gets = []

    def post(self, url, data=None, timeout=None):
        self.posts += 1
        return self.token_resp

    def get(self, url, params=None, headers=None, timeout=None):
        self.gets.append((url, params, headers))
        return self.pages.pop(0)


def client(session):
    return GraphClient("tenant", "client", "secret", "dave@tag.com", session=session)


def test_token_is_cached_and_paging_follows_next_link():
    session = FakeSession(
        Resp(200, {"access_token": "tok", "expires_in": 3600}),
        [Resp(200, {"value": [{"id": 1}], "@odata.nextLink": "https://graph.microsoft.com/v1.0/next?page=2"}),
         Resp(200, {"value": [{"id": 2}]})],
    )
    start = datetime(2026, 10, 13, tzinfo=timezone.utc)
    events = client(session).calendar_view(start, start.replace(day=14))

    assert [e["id"] for e in events] == [1, 2]
    assert session.posts == 1
    first_url, first_params, headers = session.gets[0]
    assert first_url.endswith("/users/dave@tag.com/calendarView")
    assert first_params["startDateTime"] == "2026-10-13T00:00:00Z"
    assert headers["Authorization"] == "Bearer tok"
    assert session.gets[1][0] == "https://graph.microsoft.com/v1.0/next?page=2"
    assert session.gets[1][1] is None


def test_errors_carry_status_and_graph_message():
    session = FakeSession(
        Resp(200, {"access_token": "tok", "expires_in": 3600}),
        [Resp(403, {"error": {"code": "ErrorAccessDenied", "message": "Access is denied."}})],
    )
    with pytest.raises(GraphError) as err:
        client(session).get("/users/x/calendarView")
    assert err.value.status == 403
    assert "ErrorAccessDenied" in str(err.value)


def test_naive_datetimes_are_rejected():
    session = FakeSession(Resp(200, {"access_token": "tok", "expires_in": 3600}), [])
    with pytest.raises(ValueError):
        client(session).calendar_view(datetime(2026, 10, 13), datetime(2026, 10, 14))


def test_missing_config_fails_fast():
    with pytest.raises(ValueError):
        GraphClient("", "client", "secret", "dave@tag.com")
    with pytest.raises(ValueError):
        GraphClient("tenant", "client", "", "dave@tag.com")


def test_delegated_token_provider_skips_client_credentials():
    session = FakeSession(Resp(500, {}), [Resp(200, {"value": []})])
    graph = GraphClient("tenant", "client", "", "dave@tag.com", session=session, token_provider=lambda: "delegated-tok")

    graph.get("/me")

    assert session.posts == 0
    assert session.gets[0][2]["Authorization"] == "Bearer delegated-tok"


def test_delegated_provider_without_saved_sign_in_says_how_to_connect(tmp_path, monkeypatch):
    from connectors import ms_auth
    from connectors.ms_auth import DelegatedTokenProvider, NotConnectedError

    class NoAccounts:
        def __init__(self, *args, **kwargs):
            pass

        def get_accounts(self):
            return []

    # The real MSAL app does authority discovery over the network on construction.
    monkeypatch.setattr(ms_auth.msal, "PublicClientApplication", NoAccounts)
    provider = DelegatedTokenProvider("tenant", "client", "dave@tag.com", path=tmp_path / "cache.json")
    with pytest.raises(NotConnectedError, match="connect_microsoft.py"):
        provider()
