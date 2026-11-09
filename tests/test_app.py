from __future__ import annotations

import json

from stockapp import create_app
from stockapp.config import Settings
from stockapp.providers import INDmoneyProvider, KiteProvider, TrendlyneProvider

from .conftest import FakeConnection, KiteLogin, indmoney_routes, kite_routes

CSRF = {"X-Requested-With": "stockapp"}


def test_index_page_embeds_config(client):
    response = client.get("/")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    start = html.index('<script id="app-config" type="application/json">') + len('<script id="app-config" type="application/json">')
    config = json.loads(html[start : html.index("</script>", start)])
    assert [p["name"] for p in config["providers"]] == ["indmoney", "kite", "trendlyne"]
    assert config["priceSources"] == ["indmoney", "kite"]
    assert [r["key"] for r in config["ranges"]] == ["1d", "5d", "1m", "3m", "6m", "1y", "5y"]
    assert "Content-Security-Policy" in response.headers


def test_unknown_host_is_refused(client):
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 400


def test_posts_need_the_csrf_header(client):
    assert client.post("/api/providers/indmoney/connect").status_code == 403
    assert client.post("/api/providers/indmoney/connect", headers=CSRF).status_code == 200


def test_provider_list_and_kite_login_check(client, kite_login):
    names = [p["name"] for p in client.get("/api/providers").json]
    assert names == ["indmoney", "kite", "trendlyne"]
    kite_login.logged_in = False
    status = client.get("/api/providers/kite?check=1").json
    assert status["logged_in"] is False and status["auth_url"].startswith("https://kite.zerodha.com/")


def test_disconnect_stops_the_session(client):
    status = client.post("/api/providers/kite/disconnect", headers=CSRF).json
    assert status["state"] == "idle"


def test_quote_and_history(client):
    quote = client.get("/api/quote/INFY?source=indmoney").json
    assert quote["price"] == 1142.5 and quote["source"] == "indmoney"
    history = client.get("/api/history/infy?source=kite&range=1y").json
    assert history["source"] == "kite" and history["symbol"] == "INFY" and history["summary"]["count"] == 12


def test_history_csv_download(client):
    response = client.get("/api/history/INFY?source=indmoney&range=1y&format=csv")
    assert response.mimetype == "text/csv"
    assert response.headers["Content-Disposition"] == 'attachment; filename="INFY_1y_indmoney.csv"'
    lines = response.get_data(as_text=True).splitlines()
    assert lines[0] == "Date,Open,High,Low,Close,Volume" and len(lines) == 13


def test_symbols_with_special_characters_survive_the_url(client):
    response = client.get("/api/quote/M%26M?source=kite")
    assert response.status_code == 404  # reached the provider as "M&M", which the fixture lacks
    assert "NSE:M&M" in response.json["error"]


def test_errors_are_json_with_codes(client, kite_login):
    assert client.get("/api/quote/INFY?source=trendlyne").json["code"] == "invalid_request"
    assert client.get("/api/quote/%3Cscript%3E").status_code == 400
    response = client.get("/api/history/INFY?source=indmoney&range=5y")
    assert response.status_code == 400 and response.json["code"] == "unsupported"
    kite_login.logged_in = False
    response = client.get("/api/quote/INFY?source=kite")
    assert response.status_code == 401 and response.json["code"] == "login_required"
    assert response.json["auth_url"].startswith("https://kite.zerodha.com/")
    assert client.get("/api/nothing-here").json["code"] == "http_error"


def test_search_uses_trendlyne_first(client):
    data = client.get("/api/search?q=Infosys").json
    assert data["source"] == "trendlyne" and data["results"][0]["symbol"] == "INFY"
    assert client.get("/api/search?q=").json["results"] == []
    assert client.get("/api/search?q=%21%21").status_code == 400


def test_search_falls_back_when_trendlyne_is_not_set_up(settings):
    providers = {
        "indmoney": INDmoneyProvider(FakeConnection(indmoney_routes())),
        "kite": KiteProvider(FakeConnection(kite_routes(KiteLogin()))),
        "trendlyne": TrendlyneProvider(None),
    }
    client = create_app(settings, providers).test_client()
    data = client.get("/api/search?q=Infosys").json
    assert data["source"] == "indmoney" and data["results"][0]["symbol"] == "INFY"
    assert client.get("/api/insights/INFY").json["code"] == "not_configured"


def test_insights_and_news(client):
    assert client.get("/api/insights/INFY").json["scores"]["momentum"]["value"] == 54.4
    assert len(client.get("/api/news/INFY").json) == 5


def test_settings_from_env():
    settings = Settings.from_env({"PORT": "9000", "TRENDLYNE_MCP_URL": " https://example.com/mcp/abc ", "INDMONEY_MCP_URL": ""})
    assert settings.provider_urls == {"indmoney": "", "kite": "https://mcp.kite.trade/mcp", "trendlyne": "https://example.com/mcp/abc"}
    assert settings.public_base_url == "http://localhost:9000"
    assert {"localhost:9000", "127.0.0.1:9000"} <= settings.allowed_hosts
