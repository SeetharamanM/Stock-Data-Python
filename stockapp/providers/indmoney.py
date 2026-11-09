"""INDmoney, through its official MCP server (https://mcp.indmoney.com/mcp).

Tools used: ``lookup_ind_keys`` (name -> ind_key), ``get_indian_stocks_details``
(live quote, analyst views) and ``get_indian_stocks_ohlc`` (candles).
"""

from __future__ import annotations

from typing import Any

from ..parsing import to_number
from .base import (
    NotFound,
    Provider,
    ProviderError,
    history_payload,
    keep_last_days,
    keep_last_sessions,
    make_candle,
    normalize_symbol,
)

# range -> (interval, lookback, trim). INDmoney returns at most 250 candles per
# call; minute candles reach back 7 days and daily candles 1 year.
RANGE_PLAN: dict[str, tuple[str, str, tuple[str, int] | None]] = {
    "1d": ("5minute", "7d", ("sessions", 1)),
    "5d": ("15minute", "7d", ("sessions", 5)),
    "1m": ("1day", "1y", ("days", 31)),
    "3m": ("1day", "1y", ("days", 92)),
    "6m": ("1day", "1y", ("days", 183)),
    "1y": ("1day", "1y", None),
}


class INDmoneyProvider(Provider):
    name = "indmoney"
    label = "INDmoney"
    supports = frozenset({"search", "quote", "history"})
    ranges = tuple(RANGE_PLAN)
    setup_hint = "Set INDMONEY_MCP_URL (default https://mcp.indmoney.com/mcp)."

    # -- lookups

    def _lookup(self, name: str) -> list[str]:
        matches = self.call_data("lookup_ind_keys", {"names": [name], "filter_type": "IN_STOCKS"})
        if isinstance(matches, dict):  # tolerate {"results": [...]} style answers
            matches = next((v for v in matches.values() if isinstance(v, list)), [])
        keys = []
        for match in matches if isinstance(matches, list) else []:
            key = match.get("ind_key") if isinstance(match, dict) else None
            if key and key not in keys:
                keys.append(str(key))
        return keys

    def _details(self, keys: list[str], segments: list[str] | None = None) -> dict[str, Any]:
        arguments: dict[str, Any] = {"ind_keys": keys[:10]}
        if segments:
            arguments["segments"] = segments
        data = self.call_data("get_indian_stocks_details", arguments)
        if not isinstance(data, dict):
            raise ProviderError(self.name, "INDmoney returned an unexpected answer for stock details.")
        return {key: value for key, value in data.items() if isinstance(value, dict)}

    def resolve(self, symbol: str) -> str:
        """The ind_key for an NSE symbol, checked against the instrument's own symbol."""
        symbol = normalize_symbol(symbol)
        cached = self.cache.get(("ind_key", symbol))
        if cached:
            return cached
        candidates = self._lookup(symbol)[:5]
        if candidates:
            details = self._details(candidates)
            for key in candidates:  # lookup is fuzzy ("INFY" also matches "Homesfy"): verify
                basic = details.get(key, {}).get("entity_basic", {})
                names = {str(basic.get(field, "")).upper() for field in ("symbol", "name", "display_name")}
                if symbol in names:
                    self.cache.set(("ind_key", symbol), key, ttl=24 * 3600)
                    return key
        raise NotFound(self.name, f"INDmoney has no instrument with the symbol {symbol}.")

    # -- data

    def search(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        def run() -> list[dict[str, Any]]:
            keys = self._lookup(query.strip())[:limit]
            if not keys:
                return []
            details = self._details(keys)
            results = []
            for key in keys:
                entity = details.get(key, {})
                basic = entity.get("entity_basic", {})
                if not basic.get("symbol"):
                    continue
                results.append(
                    {
                        "symbol": str(basic["symbol"]).upper(),
                        "name": basic.get("name") or basic.get("display_name"),
                        "exchange": basic.get("exchange") or "NSE",
                        "type": str(entity.get("entity_class", "")).lower() or None,
                        "source": self.name,
                    }
                )
                self.cache.set(("ind_key", str(basic["symbol"]).upper()), key, ttl=24 * 3600)
            return results

        return self.cache.get_or_set(("search", query.strip().lower(), limit), 3600, run)

    def quote(self, symbol: str) -> dict[str, Any]:
        symbol = normalize_symbol(symbol)

        def run() -> dict[str, Any]:
            key = self.resolve(symbol)
            entity = self._details([key], ["analyst"]).get(key)
            if not entity:
                raise NotFound(self.name, f"INDmoney returned no details for {symbol}.")
            return quote_from_details(symbol, entity)

        return self.cache.get_or_set(("quote", symbol), 15, run)

    def history(self, symbol: str, range_key: str) -> dict[str, Any]:
        symbol = normalize_symbol(symbol)
        self.check_range(range_key)
        interval, lookback, trim = RANGE_PLAN[range_key]
        intraday = not interval.startswith("1day")

        def run() -> dict[str, Any]:
            key = self.resolve(symbol)
            data = self.call_data("get_indian_stocks_ohlc", {"ind_key": key, "interval": interval, "lookback": lookback})
            rows = data.get("candles", []) if isinstance(data, dict) else []
            candles = [c for c in (make_candle(r, r.get("datetime_ist"), intraday) for r in rows if isinstance(r, dict)) if c]
            if trim and trim[0] == "sessions":
                candles = keep_last_sessions(candles, trim[1])
            elif trim:
                candles = keep_last_days(candles, trim[1])
            note = data.get("data_note") if isinstance(data, dict) else None
            return history_payload(self.name, symbol, range_key, interval, candles, note)

        return self.cache.get_or_set(("history", symbol, range_key), 60 if intraday else 300, run)


def quote_from_details(symbol: str, entity: dict[str, Any]) -> dict[str, Any]:
    basic = entity.get("entity_basic", {})
    stats = entity.get("entity_stats", {})
    quote = {
        "source": "indmoney",
        "symbol": str(basic.get("symbol") or symbol).upper(),
        "name": basic.get("name") or basic.get("display_name") or symbol,
        "exchange": basic.get("exchange") or "NSE",
        "type": str(entity.get("entity_class", "")).lower() or None,
        "price": to_number(stats.get("live_price")),
        "change": to_number(stats.get("day_change")),
        "change_pct": to_number(stats.get("day_change_percentage")),
        "prev_close": to_number(stats.get("prev_close")),
        "open": to_number(stats.get("day_open")),
        "high": to_number(stats.get("day_high")),
        "low": to_number(stats.get("day_low")),
        "volume": to_number(stats.get("volume")),
        "week52_high": to_number(stats.get("52week_high")),
        "week52_low": to_number(stats.get("52week_low")),
        "upper_circuit": to_number(stats.get("upper_circuit")) or None,  # 0 means none (indices)
        "lower_circuit": to_number(stats.get("lower_circuit")) or None,
        "market_cap_cr": to_number(basic.get("market_cap_in_currency")),
        "market_cap_category": basic.get("market_cap"),
        "updated_at": stats.get("price_updated_at_ist"),
        "price_mode": stats.get("price_mode"),
        "analyst": None,
    }
    forecast = entity.get("analyst_forecast") or {}
    consensus, target = forecast.get("consensus") or {}, forecast.get("target_price") or {}
    if consensus or target:
        quote["analyst"] = {
            "consensus": consensus.get("sentiment"),
            "analysts": to_number(consensus.get("num_analysts") or target.get("num_analysts")),
            "buy_pct": to_number(consensus.get("buy_per")),
            "hold_pct": to_number(consensus.get("hold_per")),
            "sell_pct": to_number(consensus.get("sell_per")),
            "target_mean": to_number(target.get("mean") or consensus.get("target_prc")),
            "target_low": to_number(target.get("low")),
            "target_high": to_number(target.get("high")),
            "upside_pct": to_number(target.get("upside_per")),
        }
    return quote
