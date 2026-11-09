from __future__ import annotations

from datetime import datetime

import pytest

from stockapp.providers import (
    INDmoneyProvider,
    InvalidRequest,
    LoginRequired,
    NotConfigured,
    NotFound,
    PlanLimit,
    Unsupported,
    history_csv,
    normalize_symbol,
)
from stockapp.providers.base import TTLCache, keep_last_days, keep_last_sessions, parse_time, summarize

from .conftest import FakeConnection, indmoney_routes, text_result

# -- shared helpers ---------------------------------------------------------------


@pytest.mark.parametrize("raw", ["infy", " M&M ", "BAJAJ-AUTO", "nifty  50"])
def test_normalize_symbol_accepts_exchange_symbols(raw):
    assert normalize_symbol(raw) in {"INFY", "M&M", "BAJAJ-AUTO", "NIFTY 50"}


@pytest.mark.parametrize("raw", ["", "<script>", "a;b", "X" * 31])
def test_normalize_symbol_rejects_junk(raw):
    with pytest.raises(InvalidRequest):
        normalize_symbol(raw)


def test_parse_time_handles_both_provider_formats():
    assert parse_time("2026-11-09 09:15:00").strftime("%Y-%m-%d %H:%M %Z") == "2026-11-09 09:15 IST"
    assert parse_time("2026-11-09T03:45:00Z").strftime("%H:%M") == "09:15"  # converted to IST
    assert parse_time("2026-11-09T09:15:00+0530").strftime("%H:%M") == "09:15"
    assert parse_time("not a date") is None


def candle(time, close=10.0, volume=100):
    return {"time": time, "open": close, "high": close + 1, "low": close - 1, "close": close, "volume": volume}


def test_keep_last_sessions_and_days():
    candles = [candle("2026-11-06 15:25"), candle("2026-11-08 14:20"), candle("2026-11-09 09:15"), candle("2026-11-09 09:20")]
    assert [c["time"] for c in keep_last_sessions(candles, 1)] == ["2026-11-09 09:15", "2026-11-09 09:20"]
    assert len(keep_last_sessions(candles, 2)) == 3
    daily = [candle("2026-09-01"), candle("2026-10-20"), candle("2026-11-09")]
    assert [c["time"] for c in keep_last_days(daily, 31)] == ["2026-10-20", "2026-11-09"]  # counted from the newest
    assert keep_last_days(daily, 31, today=datetime(2027, 1, 1)) == []


def test_summarize_and_csv():
    candles = [candle("2026-11-06", 100.0, 10), candle("2026-11-09", 110.0, 30)]
    summary = summarize(candles)
    assert summary["change"] == 10.0 and summary["change_pct"] == 10.0
    assert (summary["high"], summary["low"], summary["avg_volume"]) == (111.0, 99.0, 20)
    assert summarize([]) is None
    csv_text = history_csv({"intraday": False, "candles": candles})
    assert csv_text.splitlines() == ["Date,Open,High,Low,Close,Volume", "2026-11-06,100.0,101.0,99.0,100.0,10", "2026-11-09,110.0,111.0,109.0,110.0,30"]


def test_ttl_cache_expires_and_evicts():
    cache = TTLCache(max_entries=2)
    cache.set("a", 1, ttl=60)
    cache.set("b", 2, ttl=-1)  # already expired
    assert cache.get("a") == 1 and cache.get("b") is None
    cache.set("c", 3, ttl=60)
    cache.set("d", 4, ttl=60)
    assert cache.get("a") is None  # least recently used entry was evicted
    assert cache.get_or_set("e", 60, lambda: 5) == 5


# -- INDmoney ------------------------------------------------------------------------


def test_indmoney_resolve_verifies_fuzzy_lookup(indmoney):
    assert indmoney.resolve("infy") == "INDS00577"
    assert indmoney.connection.calls[0] == ("lookup_ind_keys", {"names": ["INFY"], "filter_type": "IN_STOCKS"})
    indmoney.resolve("INFY")
    assert len(indmoney.connection.calls) == 2  # lookup + details, then cached


def test_indmoney_resolve_rejects_unrelated_matches():
    routes = indmoney_routes()
    routes["lookup_ind_keys"] = text_result('[{"ind_key": "INDS00577", "name": "Infosys"}]')
    provider = INDmoneyProvider(FakeConnection(routes))
    with pytest.raises(NotFound):
        provider.resolve("WIPRO")  # lookup returned Infosys, whose symbol is INFY


def test_indmoney_quote_and_analysts(indmoney):
    quote = indmoney.quote("INFY")
    assert (quote["name"], quote["price"], quote["change"], quote["change_pct"]) == ("Infosys Ltd", 1142.5, -29.1, -2.48)
    assert (quote["week52_low"], quote["week52_high"], quote["market_cap_cr"]) == (980.4, 1728.0, 475469.51)
    assert quote["analyst"] == {
        "consensus": "HOLD", "analysts": 42.0, "buy_pct": 45.24, "hold_pct": 45.24, "sell_pct": 9.52,
        "target_mean": 1160.0, "target_low": 908.0, "target_high": 1610.0, "upside_pct": -0.99,
    }
    assert ("get_indian_stocks_details", {"ind_keys": ["INDS00577"], "segments": ["analyst"]}) in indmoney.connection.calls


def test_indmoney_history(indmoney):
    history = indmoney.history("INFY", "1m")
    assert history["interval"] == "1day" and not history["intraday"]
    assert history["candles"][0] == {"time": "2026-10-26", "open": 1115.0, "high": 1166.0, "low": 1107.0, "close": 1166.0, "volume": 25964569}
    assert "2026-11-08" in [c["time"] for c in history["candles"]]  # Diwali Muhurat session (a Sunday) is kept
    assert history["summary"]["last"] == 1142.5
    tool, args = indmoney.connection.calls[-1]
    assert (tool, args["interval"], args["lookback"]) == ("get_indian_stocks_ohlc", "1day", "1y")


def test_indmoney_has_no_five_year_history(indmoney):
    with pytest.raises(Unsupported, match="1D, 5D, 1M, 3M, 6M, 1Y"):
        indmoney.history("INFY", "5y")
    with pytest.raises(InvalidRequest):
        indmoney.history("INFY", "10y")


def test_indmoney_search_returns_symbols(indmoney):
    assert indmoney.search("Infosys") == [{"symbol": "INFY", "name": "Infosys Ltd", "exchange": "NSE", "type": "stock", "source": "indmoney"}]


def test_index_details_without_volume_or_circuits():
    routes = indmoney_routes()
    routes["lookup_ind_keys"] = text_result('[{"ind_key": "INDI00012", "name": "NIFTY 50"}]')
    from .fake_servers import fixture

    routes["get_indian_stocks_details"] = text_result(fixture("indmoney_details_nifty.json"))
    quote = INDmoneyProvider(FakeConnection(routes)).quote("NIFTY 50")
    assert quote["symbol"] == "NIFTY" and quote["type"] == "index"
    assert quote["volume"] is None and quote["upper_circuit"] is None and quote["analyst"] is None


def test_unconfigured_provider_explains_setup():
    with pytest.raises(NotConfigured, match="INDMONEY_MCP_URL"):
        INDmoneyProvider(None).quote("INFY")


# -- Kite -------------------------------------------------------------------------------


def test_kite_status_reports_login_link_then_user(kite, kite_login):
    kite_login.logged_in = False
    status = kite.status(check_login=True)
    assert status["logged_in"] is False and status["login"] == "kite"
    assert status["auth_url"].startswith("https://kite.zerodha.com/connect/login")
    kite_login.logged_in = True
    status = kite.status(check_login=True)
    assert (status["logged_in"], status["user"]) == (True, "Test User")


def test_kite_data_calls_need_login(kite, kite_login):
    kite_login.logged_in = False
    with pytest.raises(LoginRequired) as raised:
        kite.quote("INFY")
    assert raised.value.status == 401 and raised.value.extra["login"] == "kite"


def test_kite_instrument_needs_exact_id(kite):
    assert kite.instrument("INFY")["instrument_token"] == 408065  # not the INFYTRUST decoy
    with pytest.raises(NotFound):
        kite.instrument("INF")


def test_kite_quote_computes_change_from_previous_close(kite):
    quote = kite.quote("INFY")
    assert (quote["price"], quote["prev_close"], quote["change"], quote["change_pct"]) == (1142.5, 1171.6, -29.1, -2.48)
    assert quote["average_price"] == 1151.32


def test_kite_history_requests_and_trimming(kite):
    intraday = kite.history("INFY", "1d")
    assert [c["time"] for c in intraday["candles"]] == ["2026-11-09 09:15", "2026-11-09 09:20", "2026-11-09 09:25", "2026-11-09 09:30"]
    tool, args = kite.connection.calls[-1]
    assert tool == "get_historical_data" and args["instrument_token"] == 408065 and args["interval"] == "5minute"
    assert len(args["from_date"]) == len("2026-11-02 09:15:00")
    daily = kite.history("INFY", "5y")
    assert daily["candles"][-1]["time"] == "2026-11-09" and daily["interval"] == "day"


def test_kite_search_prefers_nse_equities(kite):
    results = kite.search("infosys")
    assert [(r["symbol"], r["exchange"]) for r in results] == [("INFY", "NSE"), ("HCL-INSYS", "NSE")]  # no futures, no BSE duplicate


# -- Trendlyne -------------------------------------------------------------------------------


def test_trendlyne_search(trendlyne):
    results = trendlyne.search("Infosys")
    assert results[0] == {
        "symbol": "INFY", "name": "Infosys Ltd.", "exchange": "NSE", "isin": "INE009A01021", "type": "stock",
        "sector": "Software & Services", "industry": "IT Consulting & Software", "source": "trendlyne",
    }


def test_trendlyne_insights(trendlyne):
    data = trendlyne.insights("INFY")
    assert (data["name"], data["bse_code"], data["sector"], data["price"]) == ("Infosys Ltd.", "500209", "Software & Services", 1171.6)
    assert data["scores"]["durability"] == {"value": 70.0, "label": "High Financial Strength", "color": "positive"}
    assert data["scores"]["classification"]["label"] == "Mid-range Performer"
    assert [r["label"] for r in data["returns"]] == ["1W", "1M", "1Y", "2Y", "3Y", "5Y", "10Y"]
    assert {"label": "52 weeks", "low": 980.4, "high": 1728.0} in data["ranges"]
    assert data["fundamentals"][0]["label"] == "Market Capitalization" and data["fundamentals"][0]["unit"] == "Cr."
    assert data["technicals"][0]["unit"] is None  # "abs" means no unit
    assert data["analysts"]["consensus"] == "BUY" and data["analysts"]["avg_target"] == 1305.23
    assert data["valuation"] == {"label": "Undervalued", "fair_price": 1839.0, "upside_pct": 57.0,
                                 "insight": "Infosys is undervalued at both current PE and future earnings estimates."}
    assert data["swot"]["weaknesses"]["count"] == 4
    assert data["url"] == "https://trendlyne.com/equity/about/630/INFY/infosys-ltd/"


def test_trendlyne_news(trendlyne):
    items = trendlyne.news("INFY")
    assert len(items) == 5
    assert items[0]["published"] >= items[-1]["published"]
    assert items[0]["summary"] == ""  # BSE filings just say "Enclosed"
    assert all(item["url"].startswith("https://") for item in items)


def test_trendlyne_plan_limit_is_explained(trendlyne):
    with pytest.raises(PlanLimit, match="MCP Max"):
        trendlyne.call_data("get_stock_eod_ohlc", {"stock_code": "INFY"})


def test_trendlyne_has_no_quotes(trendlyne):
    with pytest.raises(Unsupported):
        trendlyne.quote("INFY")
