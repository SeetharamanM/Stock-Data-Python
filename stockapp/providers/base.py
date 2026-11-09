"""Pieces shared by the data providers: errors, caching, symbols and candles."""

from __future__ import annotations

import csv
import io
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from mcp import MCPError
from mcp.types import CallToolResult

from ..mcp_bridge import AuthorizationRequired, BridgeError, McpConnection
from ..parsing import ToolError, decode_result, to_number

IST = timezone(timedelta(hours=5, minutes=30), "IST")  # India has no daylight saving time

# Chart ranges offered by the app. Each provider maps them to its own limits.
RANGES = {
    "1d": "1 day",
    "5d": "5 days",
    "1m": "1 month",
    "3m": "3 months",
    "6m": "6 months",
    "1y": "1 year",
    "5y": "5 years",
}
INTRADAY_RANGES = frozenset({"1d", "5d"})

_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9&.\- ]{0,29}$")


# -- errors ----------------------------------------------------------------------


class ProviderError(Exception):
    """A provider could not answer. ``status`` and ``code`` shape the API response."""

    status = 502
    code = "provider_error"

    def __init__(self, provider: str, message: str, **extra: Any):
        super().__init__(message)
        self.provider = provider
        self.message = message
        self.extra = extra

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.message, "code": self.code, "provider": self.provider, **self.extra}


class InvalidRequest(ProviderError):
    status, code = 400, "invalid_request"


class NotConfigured(ProviderError):
    status, code = 409, "not_configured"


class LoginRequired(ProviderError):
    """The user must log in; ``auth_url`` in ``extra`` is where."""

    status, code = 401, "login_required"


class NotFound(ProviderError):
    status, code = 404, "not_found"


class Unsupported(ProviderError):
    status, code = 400, "unsupported"


class PlanLimit(ProviderError):
    status, code = 403, "plan_limit"


class Unavailable(ProviderError):
    status, code = 502, "unavailable"


def normalize_symbol(symbol: str) -> str:
    """Upper-case an exchange symbol such as ``INFY``, ``M&M`` or ``NIFTY 50``."""
    cleaned = re.sub(r"\s+", " ", (symbol or "").strip().upper())
    if not _SYMBOL_RE.match(cleaned):
        raise InvalidRequest("app", f"'{(symbol or '')[:40]}' is not a valid symbol. Examples: INFY, RELIANCE, TCS.")
    return cleaned


# -- caching ----------------------------------------------------------------------


class TTLCache:
    """A small thread-safe in-memory cache with per-entry expiry."""

    def __init__(self, max_entries: int = 512):
        self._data: OrderedDict[Any, tuple[float, Any]] = OrderedDict()
        self._max = max_entries
        self._lock = threading.Lock()

    def get(self, key: Any) -> Any:
        with self._lock:
            hit = self._data.get(key)
            if hit is None or hit[0] < time.monotonic():
                self._data.pop(key, None)
                return None
            self._data.move_to_end(key)
            return hit[1]

    def set(self, key: Any, value: Any, ttl: float) -> None:
        with self._lock:
            self._data[key] = (time.monotonic() + ttl, value)
            self._data.move_to_end(key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)

    def get_or_set(self, key: Any, ttl: float, factory: Callable[[], Any]) -> Any:
        value = self.get(key)
        if value is None:
            value = factory()  # errors are not cached
            self.set(key, value, ttl)
        return value

    def delete(self, key: Any) -> None:
        with self._lock:
            self._data.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


# -- candles ------------------------------------------------------------------------


def parse_time(value: Any) -> datetime | None:
    """Parse ``2026-11-09 09:15:00`` (IST) or ISO 8601 with an offset; result in IST."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    text = re.sub(r"([+-]\d\d)(\d\d)$", r"\1:\2", text)  # +0530 -> +05:30
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment.astimezone(IST) if moment.tzinfo else moment.replace(tzinfo=IST)


def make_candle(raw: dict[str, Any], time_value: Any, intraday: bool) -> dict[str, Any] | None:
    """A candle ``{time, open, high, low, close, volume}`` from a provider row, or None."""
    moment = parse_time(time_value)
    close = to_number(raw.get("close"))
    if moment is None or close is None:
        return None
    volume = to_number(raw.get("volume"))
    return {
        "time": moment.strftime("%Y-%m-%d %H:%M" if intraday else "%Y-%m-%d"),
        "open": to_number(raw.get("open")) or close,
        "high": to_number(raw.get("high")) or close,
        "low": to_number(raw.get("low")) or close,
        "close": close,
        "volume": int(volume) if volume is not None else None,
    }


def keep_last_sessions(candles: list[dict[str, Any]], sessions: int) -> list[dict[str, Any]]:
    """Intraday candles from the last ``sessions`` trading days present in the data."""
    days = sorted({c["time"][:10] for c in candles})[-sessions:]
    return [c for c in candles if c["time"][:10] in days]


def keep_last_days(candles: list[dict[str, Any]], days: int, today: datetime | None = None) -> list[dict[str, Any]]:
    """Candles within ``days`` calendar days of ``today`` (default: the newest candle)."""
    if not candles:
        return []
    if today is None:
        today = datetime.strptime(max(c["time"][:10] for c in candles), "%Y-%m-%d")
    cutoff = (today - timedelta(days=days)).strftime("%Y-%m-%d")
    return [c for c in candles if c["time"][:10] >= cutoff]


def summarize(candles: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Change, range and volume over the candles shown."""
    if not candles:
        return None
    first, last = candles[0], candles[-1]
    base = first["open"] or first["close"]
    change = last["close"] - base
    volumes = [c["volume"] for c in candles if c["volume"] is not None]
    return {
        "start": first["time"],
        "end": last["time"],
        "first": base,
        "last": last["close"],
        "change": round(change, 4),
        "change_pct": round(change / base * 100, 2) if base else None,
        "high": max(c["high"] for c in candles),
        "low": min(c["low"] for c in candles),
        "avg_volume": round(sum(volumes) / len(volumes)) if volumes else None,
        "count": len(candles),
    }


def history_payload(
    provider: str, symbol: str, range_key: str, interval: str, candles: list[dict[str, Any]], note: str | None = None
) -> dict[str, Any]:
    candles = sorted(candles, key=lambda c: c["time"])
    return {
        "source": provider,
        "symbol": symbol,
        "range": range_key,
        "interval": interval,
        "intraday": range_key in INTRADAY_RANGES,
        "candles": candles,
        "summary": summarize(candles),
        "note": note,
    }


def history_csv(history: dict[str, Any]) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["Time" if history["intraday"] else "Date", "Open", "High", "Low", "Close", "Volume"])
    for c in history["candles"]:
        writer.writerow([c["time"], c["open"], c["high"], c["low"], c["close"], "" if c["volume"] is None else c["volume"]])
    return out.getvalue()


# -- the provider base class --------------------------------------------------------------


class Provider:
    """A data source reached through an MCP server. Subclasses add the data methods."""

    name = ""
    label = ""
    supports: frozenset[str] = frozenset()  # of: search, quote, history, insights, news
    ranges: tuple[str, ...] = ()
    setup_hint = ""

    def __init__(self, connection: McpConnection | None, cache: TTLCache | None = None):
        self.connection = connection
        self.cache = cache or TTLCache()

    @property
    def configured(self) -> bool:
        return self.connection is not None

    # -- connection management

    def status(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "name": self.name,
            "label": self.label,
            "configured": self.configured,
            "supports": sorted(self.supports),
            "ranges": list(self.ranges),
            "setup_hint": self.setup_hint,
        }
        if self.connection is not None:
            info.update(self.connection.status())
        else:
            info["state"] = "not_configured"
        return info

    def connect(self) -> dict[str, Any]:
        self._require_connection().connect()
        return self.status()

    def disconnect(self) -> None:
        if self.connection is not None:
            self.connection.stop(forget_login=True)
        self.cache.clear()

    def _require_connection(self) -> McpConnection:
        if self.connection is None:
            raise NotConfigured(self.name, f"{self.label} is not set up. {self.setup_hint}".strip())
        return self.connection

    # -- tool calls

    def call(self, tool: str, arguments: dict[str, Any] | None = None) -> CallToolResult:
        connection = self._require_connection()
        try:
            return connection.call_tool(tool, arguments)
        except AuthorizationRequired as exc:
            raise LoginRequired(self.name, f"Connect {self.label} to load this data.", auth_url=exc.auth_url) from None
        except MCPError as exc:
            raise ProviderError(self.name, f"{self.label} rejected '{tool}': {exc}") from None
        except BridgeError as exc:
            raise Unavailable(self.name, str(exc)) from None

    def call_data(self, tool: str, arguments: dict[str, Any] | None = None) -> Any:
        result = self.call(tool, arguments)
        try:
            return decode_result(result)
        except ToolError as exc:
            raise self.tool_error(tool, str(exc)) from None

    def tool_error(self, tool: str, message: str) -> ProviderError:
        """Map a tool's error message to an exception (subclasses refine this)."""
        if re.search(r"not found|no (data|record|match)", message, re.IGNORECASE):
            return NotFound(self.name, f"{self.label}: {message}")
        return ProviderError(self.name, f"{self.label}: {message}")

    # -- data (subclasses override what they support)

    def _unsupported(self, what: str) -> Unsupported:
        return Unsupported(self.name, f"{self.label} does not provide {what} in this app.")

    def search(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        raise self._unsupported("symbol search")

    def quote(self, symbol: str) -> dict[str, Any]:
        raise self._unsupported("quotes")

    def history(self, symbol: str, range_key: str) -> dict[str, Any]:
        raise self._unsupported("price history")

    def insights(self, symbol: str) -> dict[str, Any]:
        raise self._unsupported("research insights")

    def news(self, symbol: str, limit: int = 12) -> list[dict[str, Any]]:
        raise self._unsupported("news")

    def check_range(self, range_key: str) -> None:
        if range_key not in RANGES:
            raise InvalidRequest(self.name, f"Unknown range '{range_key}'. Use one of: {', '.join(RANGES)}.")
        if range_key not in self.ranges:
            offered = ", ".join(r.upper() for r in self.ranges)
            raise Unsupported(self.name, f"{self.label} offers {offered} charts here, not {range_key.upper()}.")
