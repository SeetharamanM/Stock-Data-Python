"""End-to-end tests of the MCP bridge against real local MCP servers (see fake_servers.py)."""

from __future__ import annotations

import os
import stat
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest

from stockapp import create_app
from stockapp.config import Settings
from stockapp.mcp_bridge import AUTHORIZING, CONNECTED, IDLE, EventLoopThread, FileTokenStorage, McpConnection
from stockapp.providers import INDmoneyProvider, KiteProvider, LoginRequired, Unavailable

from .fake_servers import InMemoryOAuthProvider, free_port, indmoney_server, kite_server, serve

CSRF = {"X-Requested-With": "stockapp"}


@pytest.fixture(scope="module")
def loop_thread():
    return EventLoopThread("test-bridge")


def make_app(tmp_path, providers):
    settings = Settings(data_dir=tmp_path, public_base_url="http://localhost", allowed_hosts=frozenset({"localhost"}))
    app = create_app(settings, providers)
    app.testing = True
    return app


def indmoney_provider(url, tmp_path, loop_thread):
    storage = FileTokenStorage(tmp_path / "tokens" / "indmoney.json", redirect_uri="http://localhost/oauth/indmoney/callback")
    return INDmoneyProvider(McpConnection("indmoney", url, loop_thread, storage=storage))


def test_plain_server_tool_calls_and_session_reuse(tmp_path, loop_thread):
    with serve(indmoney_server(port := free_port()), port) as url:
        provider = indmoney_provider(url, tmp_path, loop_thread)
        quote = provider.quote("INFY")
        assert quote["price"] == 1142.5 and quote["analyst"]["consensus"] == "HOLD"
        history = provider.history("INFY", "1y")
        assert history["summary"]["count"] == 12
        assert provider.connection.state == CONNECTED
        assert not provider.connection.storage.has_tokens()  # no OAuth needed: nothing stored
        provider.connection.stop()


def test_full_oauth_login_through_the_web_app(tmp_path, loop_thread):
    oauth = InMemoryOAuthProvider()
    port = free_port()
    with serve(indmoney_server(port, oauth=oauth), port) as url:
        provider = indmoney_provider(url, tmp_path, loop_thread)
        client = make_app(tmp_path, {"indmoney": provider}).test_client()

        # Data needs a login first: the API says where to log in.
        response = client.get("/api/quote/INFY?source=indmoney")
        assert response.status_code == 401
        assert response.json["code"] == "login_required"

        status = client.post("/api/providers/indmoney/connect", headers=CSRF).json
        assert status["state"] == AUTHORIZING
        auth_url = status["auth_url"]
        assert auth_url.startswith(f"http://127.0.0.1:{port}/authorize?")
        assert parse_qs(urlsplit(auth_url).query)["redirect_uri"] == ["http://localhost/oauth/indmoney/callback"]

        # The "browser" visits the login page, which redirects back to the app.
        login = httpx2.get(auth_url, follow_redirects=False)
        assert login.status_code in (302, 303, 307)
        callback = urlsplit(login.headers["location"])
        assert callback.path == "/oauth/indmoney/callback"
        done = client.get(f"{callback.path}?{callback.query}")
        assert done.status_code == 302
        assert done.headers["location"] == "/?connected=indmoney"

        assert client.get("/api/quote/INFY?source=indmoney").json["price"] == 1142.5

        token_file = tmp_path / "tokens" / "indmoney.json"
        assert token_file.exists()
        if os.name == "posix":
            assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
        provider.connection.stop()

        # A restarted app reuses the saved login without asking again.
        again = indmoney_provider(url, tmp_path, loop_thread)
        assert again.connect()["state"] == CONNECTED
        assert again.quote("INFY")["price"] == 1142.5

        # Disconnecting forgets the login, so the next connect asks again.
        again.disconnect()
        assert not token_file.exists()
        assert again.connect()["state"] == AUTHORIZING
        again.connection.stop()
        assert again.connection.state == IDLE


def test_oauth_callback_without_pending_login_redirects_with_error(tmp_path, loop_thread):
    provider = indmoney_provider("http://127.0.0.1:9/mcp", tmp_path, loop_thread)
    client = make_app(tmp_path, {"indmoney": provider}).test_client()
    response = client.get("/oauth/indmoney/callback?code=abc&state=xyz")
    assert response.status_code == 302
    assert "login_error=" in response.headers["location"]


def test_kite_login_is_bound_to_the_mcp_session(tmp_path, loop_thread):
    port = free_port()
    with serve(kite_server(port), port) as url:
        storage = FileTokenStorage(tmp_path / "kite.json", redirect_uri="http://localhost/oauth/kite/callback")
        kite = KiteProvider(McpConnection("kite", url, loop_thread, storage=storage))

        status = kite.connect()
        assert status["state"] == CONNECTED and status["logged_in"] is False
        with pytest.raises(LoginRequired) as raised:
            kite.quote("INFY")
        assert raised.value.extra["auth_url"] == status["auth_url"]

        assert httpx2.get(status["auth_url"]).status_code == 200  # the user logs in
        status = kite.status(check_login=True)
        assert status["logged_in"] is True and status["user"] == "Test User"
        assert kite.quote("INFY")["price"] == 1142.5
        assert [c["time"][:10] for c in kite.history("INFY", "1d")["candles"]] == ["2026-11-09"] * 4

        # A second, separate session is not logged in: the login belongs to the first one.
        other = KiteProvider(McpConnection("kite", url, loop_thread))
        assert other.connect()["logged_in"] is False
        other.connection.stop()
        kite.connection.stop()


def test_reconnects_after_the_server_restarts(tmp_path, loop_thread):
    port = free_port()
    provider = None
    with serve(indmoney_server(port), port) as url:
        provider = indmoney_provider(url, tmp_path, loop_thread)
        assert provider.quote("INFY")["price"] == 1142.5
    with serve(indmoney_server(port), port):
        provider.cache.clear()
        # The old session is gone ("Session terminated"); the bridge opens a new one.
        assert provider.quote("INFY")["price"] == 1142.5
    provider.connection.stop()


def test_unreachable_server_reports_a_clear_error(tmp_path, loop_thread):
    provider = indmoney_provider(f"http://127.0.0.1:{free_port()}/mcp", tmp_path, loop_thread)
    with pytest.raises(Unavailable) as raised:
        provider.quote("INFY")
    assert "Could not reach 127.0.0.1" in raised.value.message
    assert provider.status()["state"] == "failed"
