"""The web app with a scripted fake model. No API calls."""

import pytest
from fastapi.testclient import TestClient

from agent import agent as agent_module
from tests.test_agent_loop import FakeClient, text


@pytest.fixture
def client(world, monkeypatch):
    monkeypatch.setenv("NYCHA_SYNC_DISABLED", "1")
    from server import app as app_module

    app_module._sessions.clear()
    fake = FakeClient(*[text(f"Answer {i}. See [Housing Connect](https://housingconnect.nyc.gov/PublicWeb/) "
                             f"and [spam](http://bit.ly/x).") for i in range(5)])
    monkeypatch.setattr(agent_module, "_client", lambda: fake)
    with TestClient(app_module.app) as c:
        yield c


def test_pages_load(client):
    for path in ("/", "/about", "/privacy", "/static/app.js", "/static/styles.css"):
        assert client.get(path).status_code == 200
    assert "not affiliated with the City of New York" in client.get("/").text


def test_chat_sets_a_session_cookie_and_strips_bad_links(client):
    r = client.post("/api/chat", json={"message": "hello"})
    assert r.status_code == 200 and "nycha_session" in r.cookies
    body = r.json()
    assert "housingconnect.nyc.gov" in body["reply"] and "bit.ly" not in body["reply"] and body["turn_id"]


def test_conversation_continues_with_the_cookie(client):
    from server import app as app_module

    client.post("/api/chat", json={"message": "first"})
    client.post("/api/chat", json={"message": "second"})
    (session, _, _), = app_module._sessions.values()
    assert [m["role"] for m in session.messages] == ["user", "assistant", "user", "assistant"]


def test_tampered_cookie_starts_a_new_session(client):
    from server import app as app_module

    client.post("/api/chat", json={"message": "first"})
    client.cookies.clear()
    client.cookies.set("nycha_session", "abc.def")
    client.post("/api/chat", json={"message": "second"})
    assert len(app_module._sessions) == 2


def test_long_message_is_rejected_without_a_model_call(client):
    r = client.post("/api/chat", json={"message": "x" * 2001})
    assert r.json()["blocked"] == "message_too_long"


def test_feedback_and_new_conversation(client):
    turn = client.post("/api/chat", json={"message": "hi"}).json()["turn_id"]
    assert client.post("/api/feedback", json={"turn_id": turn, "rating": "up"}).json() == {"ok": True}
    assert client.post("/api/feedback", json={"turn_id": turn, "rating": "meh"}).status_code == 422
    assert client.post("/api/new").json() == {"ok": True}


def test_status_reports_lottery_data(client):
    assert client.get("/api/status").json()["data_as_of"]


def test_bot_check_blocks_until_passed_then_remembers(client, monkeypatch):
    from server import app as app_module

    monkeypatch.setattr(app_module, "TURNSTILE_ON", True)
    monkeypatch.setattr(app_module, "_turnstile_ok", lambda token, ip: token == "good")
    app_module._verified_sessions.clear()
    r = client.post("/api/chat", json={"message": "hi"})
    assert r.status_code == 403 and r.json()["error"] == "bot_check"
    assert client.post("/api/chat", json={"message": "hi", "turnstile_token": "good"}).status_code == 200
    assert client.post("/api/chat", json={"message": "again"}).status_code == 200  # session stays verified


def test_production_uses_the_proxy_appended_ip(monkeypatch):
    from starlette.requests import Request

    from server import app as app_module

    monkeypatch.setattr(app_module, "PRODUCTION", True)
    req = Request({"type": "http", "headers": [(b"x-forwarded-for", b"6.6.6.6, 203.0.113.9")], "client": ("10.0.0.1", 1)})
    assert app_module._client_ip(req) == "203.0.113.9"  # the visitor-supplied 6.6.6.6 is ignored


def test_security_headers(client):
    r = client.get("/")
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
