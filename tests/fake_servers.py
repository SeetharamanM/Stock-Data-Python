"""Real MCP servers, run locally over HTTP, that imitate INDmoney, Kite and Trendlyne.

They answer with the recorded responses in tests/fixtures, so the tests exercise
the whole path: the MCP SDK's HTTP transport, sessions, OAuth and our parsing.
"""

from __future__ import annotations

import secrets
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import uvicorn
from mcp.server.auth.provider import AccessToken, AuthorizationCode, AuthorizationParams, RefreshToken, construct_redirect_uri
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import PlainTextResponse

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def serve(server: MCPServer, port: int) -> Iterator[str]:
    """Run ``server`` on 127.0.0.1:``port``; yields the MCP endpoint URL."""
    app = server.streamable_http_app()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", lifespan="on")
    runner = uvicorn.Server(config)
    thread = threading.Thread(target=runner.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not runner.started:
        if time.monotonic() > deadline:
            raise RuntimeError("test MCP server did not start")
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        runner.should_exit = True
        thread.join(timeout=10)


class InMemoryOAuthProvider:
    """An OAuth authorization server that approves every request (for tests)."""

    def __init__(self) -> None:
        self.clients: dict[str, OAuthClientInformationFull] = {}
        self.codes: dict[str, AuthorizationCode] = {}
        self.access: dict[str, AccessToken] = {}
        self.refresh: dict[str, RefreshToken] = {}

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self.clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self.clients[client_info.client_id] = client_info

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        code = secrets.token_urlsafe(16)
        self.codes[code] = AuthorizationCode(
            code=code,
            scopes=params.scopes or [],
            expires_at=time.time() + 300,
            client_id=client.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
        )
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str) -> AuthorizationCode | None:
        return self.codes.get(authorization_code)

    def _issue(self, client_id: str, scopes: list[str], resource: str | None) -> OAuthToken:
        access, refresh = secrets.token_hex(16), secrets.token_hex(16)
        self.access[access] = AccessToken(token=access, client_id=client_id, scopes=scopes, expires_at=int(time.time()) + 3600, resource=resource)
        self.refresh[refresh] = RefreshToken(token=refresh, client_id=client_id, scopes=scopes, resource=resource)
        return OAuthToken(access_token=access, expires_in=3600, refresh_token=refresh, scope=" ".join(scopes) or None)

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode) -> OAuthToken:
        self.codes.pop(authorization_code.code, None)
        return self._issue(client.client_id, authorization_code.scopes, authorization_code.resource)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        return self.refresh.get(refresh_token)

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        self.refresh.pop(refresh_token.token, None)
        return self._issue(client.client_id, scopes or refresh_token.scopes, refresh_token.resource)

    async def load_access_token(self, token: str) -> AccessToken | None:
        return self.access.get(token)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self.access.pop(token.token, None)
        self.refresh.pop(token.token, None)


def indmoney_server(port: int, *, oauth: InMemoryOAuthProvider | None = None) -> MCPServer:
    """INDmoney-like tools; with ``oauth`` the server demands OAuth like the real one."""
    kwargs = {}
    if oauth is not None:
        base = f"http://127.0.0.1:{port}"
        kwargs = {
            "auth_server_provider": oauth,
            "auth": AuthSettings(
                issuer_url=base,
                resource_server_url=f"{base}/mcp",
                client_registration_options=ClientRegistrationOptions(enabled=True),
                validate_token_resource=True,
            ),
        }
    server = MCPServer("fake-indmoney", **kwargs)

    @server.tool()
    async def lookup_ind_keys(names: list[str], filter_type: str = "IN_STOCKS") -> str:
        return fixture("indmoney_lookup_infy.json" if names == ["INFY"] else "indmoney_lookup_infosys.json")

    @server.tool()
    async def get_indian_stocks_details(ind_keys: list[str], segments: list[str] | None = None) -> str:
        return fixture("indmoney_details_infosys.json")

    @server.tool()
    async def get_indian_stocks_ohlc(ind_key: str, interval: str, lookback: str) -> str:
        return fixture("indmoney_ohlc_infosys_1day_14d.json")

    return server


def session_id(ctx: Context) -> str | None:
    """The MCP session id the client sent (what mcp.kite.trade ties a login to)."""
    return (ctx.headers or {}).get("mcp-session-id")


def kite_server(port: int) -> MCPServer:
    """Kite-like tools whose login is tied to the MCP session, like mcp.kite.trade."""
    logged_in: set[str] = set()
    server = MCPServer("fake-kite")
    login_hint = "Please log in first using the login tool"

    def require_login(ctx: Context) -> None:
        if session_id(ctx) not in logged_in:
            raise ToolError(login_hint)

    @server.custom_route("/fake-login", methods=["GET"])
    async def fake_login(request: Request) -> PlainTextResponse:
        logged_in.add(request.query_params["session"])
        return PlainTextResponse("Logged in. You can close this tab.")

    @server.tool()
    async def login(ctx: Context) -> str:
        if session_id(ctx) in logged_in:
            return "You are already logged in as Test User"
        url = f"http://127.0.0.1:{port}/fake-login?session={session_id(ctx)}"
        return fixture("kite_login.txt").replace(
            "https://kite.zerodha.com/connect/login?api_key=kitemcp&v=3&redirect_params=session_id%3D44ce2d96-710b-4442-bf57-cd0d5d0e4050", url
        )

    @server.tool()
    async def get_profile(ctx: Context) -> str:
        require_login(ctx)
        return fixture("kite_profile.json")

    @server.tool()
    async def search_instruments(query: str, filter_on: str = "id", limit: int = 0) -> str:
        return fixture("kite_search_id_nse_infy.json" if filter_on == "id" else "kite_search_name_infosys.json")

    @server.tool()
    async def get_quotes(instruments: list[str], ctx: Context) -> str:
        require_login(ctx)
        return fixture("kite_quote_nse_infy.json")

    @server.tool()
    async def get_historical_data(instrument_token: int, from_date: str, to_date: str, interval: str, ctx: Context) -> str:
        require_login(ctx)
        return fixture("kite_history_infy_5minute.json" if interval == "5minute" else "kite_history_infy_day.json")

    return server


def trendlyne_server() -> MCPServer:
    server = MCPServer("fake-trendlyne")

    @server.tool()
    async def search_entities(query: str, entity_type: str = "stock", limit: int = 10) -> str:
        return fixture("trendlyne_search_infosys.txt")

    @server.tool()
    async def get_overview_news_corp_events(stock_code: str, type: str) -> str:  # noqa: A002 - the real tool's name
        return fixture("trendlyne_overview_infy.txt" if type == "overview" else "trendlyne_news_infy.txt")

    @server.tool()
    async def get_stock_eod_ohlc(stock_code: str, period: str = "weekly") -> str:
        return fixture("trendlyne_plan_error.json")

    return server
