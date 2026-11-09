"""Stock Data Python: a Flask web app for Indian market data from INDmoney, Kite and Trendlyne.

The app talks to each provider's MCP (Model Context Protocol) server. See
``stockapp.mcp_bridge`` for how sessions and logins work and
``stockapp.providers`` for the data each source supplies.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlencode

from flask import Flask, Response, abort, jsonify, redirect, render_template, request

from .config import Settings
from .mcp_bridge import CONNECTED, FAILED
from .providers import (
    RANGES,
    InvalidRequest,
    NotConfigured,
    Provider,
    ProviderError,
    build_providers,
    history_csv,
    normalize_symbol,
)

log = logging.getLogger(__name__)

PRICE_SOURCES = ("indmoney", "kite")  # providers with quotes and price history
SEARCH_ORDER = ("trendlyne", "indmoney", "kite")  # richest search results first
CSRF_HEADER = "X-Requested-With"

CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self' https://cdn.jsdelivr.net",
        "style-src 'self' 'unsafe-inline'",  # Plotly sets inline styles
        "img-src 'self' data:",
        "connect-src 'self'",
        "frame-ancestors 'none'",
        "base-uri 'none'",
        "form-action 'self'",
    ]
)


def create_app(settings: Settings | None = None, providers: dict[str, Provider] | None = None) -> Flask:
    settings = settings or Settings.from_env()
    providers = providers if providers is not None else build_providers(settings)
    app = Flask(__name__)
    app.config["SETTINGS"] = settings
    app.extensions["stock_providers"] = providers
    app.json.sort_keys = False  # keep the order the data was built in

    def provider(name: str) -> Provider:
        found = providers.get(name)
        if found is None:
            raise InvalidRequest("app", f"Unknown data source '{name[:30]}'. Use one of: {', '.join(providers)}.")
        return found

    def price_provider() -> Provider:
        name = request.args.get("source", PRICE_SOURCES[0])
        if name not in PRICE_SOURCES:
            raise InvalidRequest("app", f"Price data comes from {' or '.join(PRICE_SOURCES)}, not '{name[:30]}'.")
        return provider(name)

    # -- request guards ------------------------------------------------------------

    @app.before_request
    def guard() -> Any:
        # Only answer requests addressed to this app (stops DNS-rebinding attacks).
        if settings.allowed_hosts and request.host not in settings.allowed_hosts:
            return Response("Unknown host. Add it to ALLOWED_HOSTS if you meant to use it.", 400)
        # State-changing calls must come from the app's own JavaScript (stops CSRF).
        if request.method == "POST" and request.headers.get(CSRF_HEADER) != "stockapp":
            return jsonify(error="Missing X-Requested-With header.", code="csrf"), 403
        return None

    @app.after_request
    def security_headers(response: Response) -> Response:
        response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    @app.errorhandler(ProviderError)
    def provider_error(exc: ProviderError) -> tuple[Response, int]:
        return jsonify(exc.to_dict()), exc.status

    @app.errorhandler(404)
    @app.errorhandler(405)
    def http_error(exc: Any) -> Any:
        if request.path.startswith("/api/"):
            return jsonify(error=exc.description, code="http_error"), exc.code
        return exc

    @app.errorhandler(Exception)
    def unexpected_error(exc: Exception) -> Any:
        if hasattr(exc, "code") and hasattr(exc, "description"):  # werkzeug HTTPException
            return exc
        log.exception("Unexpected error")
        if request.path.startswith("/api/"):
            return jsonify(error="Unexpected server error; see the terminal running the app.", code="internal"), 500
        return Response("Unexpected server error; see the terminal running the app.", 500)

    # -- pages ---------------------------------------------------------------------

    @app.get("/")
    def index() -> str:
        config = {
            "providers": [p.status() for p in providers.values()],
            "ranges": [{"key": key, "label": label} for key, label in RANGES.items()],  # a list keeps the order
            "priceSources": [name for name in PRICE_SOURCES if name in providers],
        }
        return render_template("index.html", config=config)

    # -- providers -----------------------------------------------------------------

    @app.get("/api/providers")
    def list_providers() -> Response:
        return jsonify([p.status() for p in providers.values()])

    @app.get("/api/providers/<name>")
    def provider_status(name: str) -> Response:
        found = provider(name)
        if request.args.get("check") and hasattr(found, "profile"):
            return jsonify(found.status(check_login=True))  # type: ignore[call-arg]
        return jsonify(found.status())

    @app.post("/api/providers/<name>/connect")
    def connect_provider(name: str) -> Response:
        return jsonify(provider(name).connect())

    @app.post("/api/providers/<name>/disconnect")
    def disconnect_provider(name: str) -> Response:
        found = provider(name)
        found.disconnect()
        return jsonify(found.status())

    @app.get("/oauth/<name>/callback")
    def oauth_callback(name: str) -> Any:
        found = providers.get(name)
        connection = found.connection if found else None
        if connection is None or connection.oauth is None:
            abort(404)
        accepted = connection.oauth.complete(
            code=request.args.get("code"),
            state=request.args.get("state"),
            iss=request.args.get("iss"),
            error=request.args.get("error"),
            error_description=request.args.get("error_description"),
        )
        if not accepted:
            return redirect("/?" + urlencode({"provider": name, "login_error": "This login link has expired. Connect again."}))
        state = connection.wait(60, through_login=True)
        if state == CONNECTED:
            return redirect("/?" + urlencode({"connected": name}))
        message = connection.status().get("error") if state == FAILED else "The login did not finish. Try connecting again."
        return redirect("/?" + urlencode({"provider": name, "login_error": message or "Login failed."}))

    # -- data ----------------------------------------------------------------------

    @app.get("/api/search")
    def search() -> Response:
        query = request.args.get("q", "").strip()
        if not query:
            return jsonify(query=query, results=[], source=None)
        if len(query) > 60 or not re.search(r"\w", query):
            raise InvalidRequest("app", "Search for a company name or symbol (up to 60 characters).")
        requested = request.args.get("source")
        order = [requested] if requested else [n for n in SEARCH_ORDER if n in providers]
        problems: list[dict[str, Any]] = []
        for name in order:
            source = provider(name)
            if "search" not in source.supports:
                continue
            try:
                return jsonify(query=query, results=source.search(query), source=name)
            except NotConfigured:
                continue
            except ProviderError as exc:
                problems.append(exc.to_dict())
        return jsonify(query=query, results=[], source=None, problems=problems)

    @app.get("/api/quote/<symbol>")
    def quote(symbol: str) -> Response:
        return jsonify(price_provider().quote(normalize_symbol(symbol)))

    @app.get("/api/history/<symbol>")
    def history(symbol: str) -> Response:
        symbol = normalize_symbol(symbol)
        source = price_provider()
        range_key = request.args.get("range", "1y")
        data = source.history(symbol, range_key)
        if request.args.get("format") == "csv":
            safe = re.sub(r"[^A-Za-z0-9-]+", "_", symbol).strip("_")
            filename = f"{safe}_{range_key}_{source.name}.csv"
            return Response(
                history_csv(data),
                mimetype="text/csv",
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )
        return jsonify(data)

    @app.get("/api/insights/<symbol>")
    def insights(symbol: str) -> Response:
        return jsonify(provider("trendlyne").insights(normalize_symbol(symbol)))

    @app.get("/api/news/<symbol>")
    def news(symbol: str) -> Response:
        return jsonify(provider("trendlyne").news(normalize_symbol(symbol)))

    return app
