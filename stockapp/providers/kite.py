"""Kite (Zerodha), through Zerodha's hosted MCP server (https://mcp.kite.trade/mcp).

The hosted server needs no API key. Its ``login`` tool returns a link; once the
user logs in there, the *same MCP session* is authorized, so the bridge keeps
that session open. Kite ends sessions daily, after which ``login`` is needed again.

Tools used: ``login``, ``get_profile``, ``search_instruments``, ``get_quotes`` and
``get_historical_data``.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from ..parsing import ToolError, decode_result, find_url, result_text, to_number
from .base import (
    IST,
    LoginRequired,
    NotFound,
    Provider,
    ProviderError,
    history_payload,
    keep_last_sessions,
    make_candle,
    normalize_symbol,
)

# range -> (interval, calendar days to request, trading sessions to keep)
RANGE_PLAN: dict[str, tuple[str, int, int | None]] = {
    "1d": ("5minute", 7, 1),
    "5d": ("15minute", 10, 5),
    "1m": ("day", 31, None),
    "3m": ("day", 92, None),
    "6m": ("day", 183, None),
    "1y": ("day", 366, None),
    "5y": ("day", 1827, None),  # Kite allows up to 2000 days of daily candles per request
}

_LOGIN_HINT = re.compile(r"log ?in", re.IGNORECASE)
_EQUITY_SEGMENTS = {"NSE", "BSE", "INDICES"}


def _instruments(data: Any) -> list[dict[str, Any]]:
    """search_instruments answers a list, or {"data": [...], "pagination": ...} with a limit."""
    items = data.get("data") if isinstance(data, dict) else data
    return [item for item in items or [] if isinstance(item, dict)]


class KiteProvider(Provider):
    name = "kite"
    label = "Kite (Zerodha)"
    supports = frozenset({"search", "quote", "history"})
    ranges = tuple(RANGE_PLAN)
    setup_hint = "Set KITE_MCP_URL (default https://mcp.kite.trade/mcp)."

    # -- login

    def call_data(self, tool: str, arguments: dict[str, Any] | None = None) -> Any:
        result = self.call(tool, arguments)
        if result.is_error and _LOGIN_HINT.search(result_text(result)):
            raise self._login_required()
        try:
            return decode_result(result)
        except ToolError as exc:
            raise self.tool_error(tool, str(exc)) from None

    def _login_required(self) -> LoginRequired:
        url = self.cache.get("login_url")
        if not url:
            text = result_text(self.call("login"))
            url = find_url(text)
            if not url:
                raise ProviderError(self.name, f"Kite did not return a login link: {text[:200]}")
            self.cache.set("login_url", url, ttl=600)
        return LoginRequired(self.name, "Log in to Kite, then come back to this page.", auth_url=url, login="kite")

    def profile(self) -> dict[str, Any]:
        profile = self.cache.get("profile")
        if profile is None:
            profile = self.call_data("get_profile")
            if not isinstance(profile, dict):
                raise ProviderError(self.name, "Kite returned an unexpected profile.")
            self.cache.set("profile", profile, ttl=30)
            self.cache.delete("login_url")
        return profile

    def status(self, check_login: bool = False) -> dict[str, Any]:
        info = super().status()
        if info.get("state") != "connected" or not (check_login or self.cache.get("profile")):
            return info
        try:
            profile = self.profile()
        except LoginRequired as exc:
            info.update(logged_in=False, login="kite", auth_url=exc.extra.get("auth_url"))
        except ProviderError as exc:
            info.update(logged_in=False, error=exc.message)
        else:
            info.update(logged_in=True, user=profile.get("user_name") or profile.get("user_id"))
        return info

    def connect(self) -> dict[str, Any]:
        self._require_connection().connect()
        return self.status(check_login=True)

    # -- instruments

    def _search_instruments(self, query: str, filter_on: str, limit: int = 50) -> list[dict[str, Any]]:
        data = self.call_data("search_instruments", {"query": query, "filter_on": filter_on, "limit": limit})
        return _instruments(data)

    def instrument(self, symbol: str, exchange: str = "NSE") -> dict[str, Any]:
        symbol = normalize_symbol(symbol)
        wanted = f"{exchange}:{symbol}"

        def run() -> dict[str, Any]:
            for item in self._search_instruments(wanted, "id", 20):  # "id" matches by substring
                if str(item.get("id", "")).upper() == wanted:
                    return item
            raise NotFound(self.name, f"Kite has no instrument {wanted}.")

        return self.cache.get_or_set(("instrument", wanted), 24 * 3600, run)

    # -- data

    def search(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        query = query.strip()

        def run() -> list[dict[str, Any]]:
            found = []
            if re.fullmatch(r"[A-Za-z0-9&\-]{1,20}", query):
                found += self._search_instruments(query, "tradingsymbol")
            found += self._search_instruments(query, "name")
            best: dict[str, dict[str, Any]] = {}
            for item in found:
                if str(item.get("segment", "")).upper() not in _EQUITY_SEGMENTS:
                    continue  # skip futures and options
                symbol = str(item.get("tradingsymbol", "")).upper()
                exchange = str(item.get("exchange", "")).upper()
                if not symbol or (symbol in best and best[symbol]["exchange"] == "NSE"):
                    continue  # prefer the NSE listing
                best[symbol] = {
                    "symbol": symbol,
                    "name": item.get("name") or symbol,
                    "exchange": exchange,
                    "isin": item.get("isin") or None,
                    "type": "index" if item.get("segment") == "INDICES" else "stock",
                    "source": self.name,
                }
            wanted = query.upper()

            def rank(result: dict[str, Any]) -> tuple[int, str]:
                symbol, name = result["symbol"], str(result["name"]).upper()
                order = [symbol == wanted, name == wanted, name.startswith(wanted), symbol.startswith(wanted)]
                return (order.index(True) if True in order else len(order), symbol)

            return sorted(best.values(), key=rank)[:limit]

        return self.cache.get_or_set(("search", query.lower(), limit), 3600, run)

    def quote(self, symbol: str) -> dict[str, Any]:
        symbol = normalize_symbol(symbol)
        key = f"NSE:{symbol}"

        def run() -> dict[str, Any]:
            data = self.call_data("get_quotes", {"instruments": [key]})
            raw = data.get(key) if isinstance(data, dict) else None
            if not isinstance(raw, dict):
                raise NotFound(self.name, f"Kite has no quote for {key}.")
            return quote_from_kite(symbol, raw)

        return self.cache.get_or_set(("quote", symbol), 10, run)

    def history(self, symbol: str, range_key: str) -> dict[str, Any]:
        symbol = normalize_symbol(symbol)
        self.check_range(range_key)
        interval, days, sessions = RANGE_PLAN[range_key]
        intraday = interval != "day"

        def run() -> dict[str, Any]:
            token = int(self.instrument(symbol)["instrument_token"])
            now = datetime.now(IST)
            rows = self.call_data(
                "get_historical_data",
                {
                    "instrument_token": token,
                    "from_date": (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S"),
                    "to_date": now.strftime("%Y-%m-%d %H:%M:%S"),
                    "interval": interval,
                },
            )
            candles = [c for c in (make_candle(r, r.get("date"), intraday) for r in rows or [] if isinstance(r, dict)) if c]
            if sessions:
                candles = keep_last_sessions(candles, sessions)
            return history_payload(self.name, symbol, range_key, interval, candles)

        return self.cache.get_or_set(("history", symbol, range_key), 60 if intraday else 300, run)


def quote_from_kite(symbol: str, raw: dict[str, Any]) -> dict[str, Any]:
    ohlc = raw.get("ohlc") or {}
    price = to_number(raw.get("last_price"))
    prev_close = to_number(ohlc.get("close"))  # Kite's "close" is the previous session's close
    change = price - prev_close if price is not None and prev_close else None
    return {
        "source": "kite",
        "symbol": symbol,
        "name": symbol,
        "exchange": "NSE",
        "type": None,
        "price": price,
        "change": round(change, 4) if change is not None else None,
        "change_pct": round(change / prev_close * 100, 2) if change is not None else None,
        "prev_close": prev_close,
        "open": to_number(ohlc.get("open")),
        "high": to_number(ohlc.get("high")),
        "low": to_number(ohlc.get("low")),
        "volume": to_number(raw.get("volume")),
        "average_price": to_number(raw.get("average_price")),
        "week52_high": None,
        "week52_low": None,
        "upper_circuit": to_number(raw.get("upper_circuit_limit")) or None,
        "lower_circuit": to_number(raw.get("lower_circuit_limit")) or None,
        "market_cap_cr": None,
        "market_cap_category": None,
        "updated_at": raw.get("timestamp"),
        "analyst": None,
    }
