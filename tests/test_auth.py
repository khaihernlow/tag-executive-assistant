import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ALLOWED_USERS", "dave@tag.example, KLow@Tag.Example")
    from app.main import app

    return TestClient(app)


def test_allowlist_is_case_insensitive(monkeypatch):
    from app.auth import is_allowed

    monkeypatch.setenv("ALLOWED_USERS", "dave@tag.example, KLow@Tag.Example")
    assert is_allowed({"preferred_username": "klow@tag.example"})
    assert is_allowed({"email": "DAVE@tag.example"})
    assert not is_allowed({"email": "someone@tag.example"})
    assert not is_allowed({})


def test_unauthenticated_home_redirects_to_login(client, monkeypatch):
    monkeypatch.setenv("REQUIRE_AUTH", "True")
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "/auth/login"


def test_bypass_still_enforces_allowlist_behind_cloudflare(client, monkeypatch):
    monkeypatch.setenv("REQUIRE_AUTH", "False")
    blocked = client.get("/", headers={"Cf-Access-Authenticated-User-Email": "intern@tag.example"})
    allowed = client.get("/", headers={"Cf-Access-Authenticated-User-Email": "dave@tag.example"})
    assert blocked.status_code == 403
    assert allowed.status_code == 200


def test_bypass_local_dev(client, monkeypatch):
    monkeypatch.setenv("REQUIRE_AUTH", "False")
    resp = client.get("/")
    assert resp.status_code == 200
    assert "TAG Assistant" in resp.text


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_saving_tokens_keeps_a_sign_in_another_process_added(tmp_path):
    """The app loaded the cache, the sign-in script then added the assistant's account,
    and the app's next save must not wipe it (it once did)."""
    import json

    import msal

    from connectors.ms_auth import load_cache, save_cache

    path = tmp_path / "cache.json"
    path.write_text(json.dumps({"Account": {"dave": {"username": "dave@tag.example"}}}), encoding="utf-8")
    app_copy = load_cache(path)                      # the running app's copy: Dave only

    disk = json.loads(path.read_text(encoding="utf-8"))
    disk["Account"]["assistant"] = {"username": "assistant@tag.example"}
    path.write_text(json.dumps(disk), encoding="utf-8")   # the sign-in script adds the assistant

    state = json.loads(app_copy.serialize())
    state["Account"]["dave"]["refreshed"] = True          # the app renews Dave's token...
    app_copy.deserialize(json.dumps(state))
    app_copy.has_state_changed = True
    save_cache(app_copy, path)                            # ...and saves

    saved = json.loads(path.read_text(encoding="utf-8"))["Account"]
    assert set(saved) == {"dave", "assistant"} and saved["dave"]["refreshed"] is True
    assert isinstance(app_copy, msal.SerializableTokenCache) and not app_copy.has_state_changed
