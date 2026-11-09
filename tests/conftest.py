"""Shared test helpers: a fake MCP connection that answers from recorded fixtures."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from mcp.types import CallToolResult, TextContent

from stockapp import create_app
from stockapp.config import Settings
from stockapp.providers import INDmoneyProvider, KiteProvider, TrendlyneProvider

from .fake_servers import fixture


def text_result(text: str, *, error: bool = False, structured: bool = True) -> CallToolResult:
    """A tool result like MCP servers send: text, plus {"result": text} from Python servers."""
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content={"result": text} if structured and not error else None,
        is_error=error,
    )


Route = CallToolResult | Callable[[dict[str, Any]], CallToolResult]


class FakeConnection:
    """Stands in for McpConnection: maps tool names to canned results and records calls."""

    def __init__(self, routes: dict[str, Route], state: str = "connected"):
        self.routes = routes
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._state = state
        self.oauth = None
        self.storage = None
        self.stopped = False

    @property
    def state(self) -> str:
        return self._state

    def call_tool(self, tool: str, arguments: dict[str, Any] | None = None, timeout: float = 60) -> CallToolResult:
        arguments = arguments or {}
        self.calls.append((tool, arguments))
        route = self.routes[tool]
        return route(arguments) if callable(route) else route

    def status(self) -> dict[str, Any]:
        return {"state": self._state, "error": None, "saved_login": False}

    def connect(self) -> dict[str, Any]:
        return self.status()

    def stop(self, forget_login: bool = False) -> None:
        self.stopped = True
        self._state = "idle"


def indmoney_routes() -> dict[str, Route]:
    return {
        "lookup_ind_keys": lambda a: text_result(
            fixture("indmoney_lookup_infy.json" if a["names"] == ["INFY"] else "indmoney_lookup_infosys.json")
        ),
        "get_indian_stocks_details": text_result(fixture("indmoney_details_infosys.json")),
        "get_indian_stocks_ohlc": text_result(fixture("indmoney_ohlc_infosys_1day_14d.json")),
    }


class KiteLogin:
    """Login state for the fake Kite server."""

    def __init__(self, logged_in: bool = True):
        self.logged_in = logged_in

    def guard(self, name: str) -> CallToolResult:
        if not self.logged_in:
            return text_result("Error executing tool: Please log in first using the login tool", error=True)
        return text_result(fixture(name), structured=False)


def kite_routes(login: KiteLogin) -> dict[str, Route]:
    return {
        "login": text_result(fixture("kite_login.txt"), structured=False),
        "get_profile": lambda a: login.guard("kite_profile.json"),
        "search_instruments": lambda a: text_result(
            fixture("kite_search_id_nse_infy.json" if a["filter_on"] == "id" else "kite_search_name_infosys.json"), structured=False
        ),
        "get_quotes": lambda a: login.guard("kite_quote_nse_infy.json"),
        "get_historical_data": lambda a: login.guard(
            "kite_history_infy_5minute.json" if a["interval"] == "5minute" else "kite_history_infy_day.json"
        ),
    }


def trendlyne_routes() -> dict[str, Route]:
    return {
        "search_entities": text_result(fixture("trendlyne_search_infosys.txt")),
        "get_overview_news_corp_events": lambda a: text_result(
            fixture("trendlyne_overview_infy.txt" if a["type"] == "overview" else "trendlyne_news_infy.txt")
        ),
        "get_stock_eod_ohlc": text_result(fixture("trendlyne_plan_error.json")),
    }


@pytest.fixture
def indmoney() -> INDmoneyProvider:
    return INDmoneyProvider(FakeConnection(indmoney_routes()))


@pytest.fixture
def kite_login() -> KiteLogin:
    return KiteLogin(logged_in=True)


@pytest.fixture
def kite(kite_login: KiteLogin) -> KiteProvider:
    return KiteProvider(FakeConnection(kite_routes(kite_login)))


@pytest.fixture
def trendlyne() -> TrendlyneProvider:
    return TrendlyneProvider(FakeConnection(trendlyne_routes()))


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(data_dir=tmp_path, public_base_url="http://localhost", allowed_hosts=frozenset({"localhost"}))


@pytest.fixture
def client(settings, indmoney, kite, trendlyne):
    app = create_app(settings, {"indmoney": indmoney, "kite": kite, "trendlyne": trendlyne})
    app.testing = True
    return app.test_client()
