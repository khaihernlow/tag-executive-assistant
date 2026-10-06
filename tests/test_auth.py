import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ALLOWED_USERS", "dave@tagsolutions.com, Kai@TagSolutions.com")
    from app.main import app

    return TestClient(app)


def test_allowlist_is_case_insensitive(monkeypatch):
    from app.auth import is_allowed

    monkeypatch.setenv("ALLOWED_USERS", "dave@tagsolutions.com, Kai@TagSolutions.com")
    assert is_allowed({"preferred_username": "kai@tagsolutions.com"})
    assert is_allowed({"email": "DAVE@tagsolutions.com"})
    assert not is_allowed({"email": "someone@tagsolutions.com"})
    assert not is_allowed({})


def test_unauthenticated_home_redirects_to_login(client, monkeypatch):
    monkeypatch.setenv("REQUIRE_AUTH", "True")
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "/auth/login"


def test_bypass_still_enforces_allowlist_behind_cloudflare(client, monkeypatch):
    monkeypatch.setenv("REQUIRE_AUTH", "False")
    blocked = client.get("/", headers={"Cf-Access-Authenticated-User-Email": "intern@tagsolutions.com"})
    allowed = client.get("/", headers={"Cf-Access-Authenticated-User-Email": "dave@tagsolutions.com"})
    assert blocked.status_code == 403
    assert allowed.status_code == 200


def test_bypass_local_dev(client, monkeypatch):
    monkeypatch.setenv("REQUIRE_AUTH", "False")
    resp = client.get("/")
    assert resp.status_code == 200
    assert "TAG Assistant" in resp.text


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}
