"""Stock Data as a Streamlit app, for Streamlit Community Cloud or `streamlit run`.

    streamlit run streamlit_app.py

It uses the same providers as the Flask app (``stockapp``), with two differences
that matter on a public host:

* Every visitor gets their own MCP sessions. Logins are kept in memory for that
  visitor only, never written to disk, and end when the visitor leaves.
* Settings come from Streamlit secrets (or environment variables). The owner's
  Trendlyne URL is only used once the visitor enters ``APP_PASSWORD``.
"""

from __future__ import annotations

import concurrent.futures
import hmac
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from stockapp.config import DEFAULT_URLS
from stockapp.mcp_bridge import AUTHORIZING, CONNECTED, FAILED, EventLoopThread, McpConnection, MemoryTokenStorage
from stockapp.providers import (
    PROVIDER_CLASSES,
    RANGES,
    NotConfigured,
    Provider,
    ProviderError,
    history_csv,
    normalize_symbol,
)

PRICE_SOURCES = ("indmoney", "kite")
SEARCH_ORDER = ("trendlyne", "indmoney", "kite")
IDLE_LIMIT = 2 * 3600  # seconds before an inactive visitor's sessions are closed
POPULAR = ("INFY", "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "SBIN", "ITC")

st.set_page_config(page_title="Stock Data", page_icon="📈", layout="wide")


# -- settings ------------------------------------------------------------------------


def setting(name: str, default: str = "") -> str:
    """A value from Streamlit secrets, else the environment, else ``default``."""
    try:
        if name in st.secrets:
            return str(st.secrets[name]).strip()
    except Exception:  # noqa: BLE001 - no secrets file
        pass
    return os.environ.get(name, default).strip()


def public_base_url() -> str:
    """The address the app is opened at; providers send logins back here."""
    configured = setting("PUBLIC_BASE_URL")
    if configured:
        return configured.rstrip("/")
    url = urlsplit(st.context.url or "http://localhost:8501")
    return f"{url.scheme}://{url.netloc}"


# -- shared state (one per server process) ---------------------------------------------


@dataclass
class Visitor:
    providers: dict[str, Provider]
    trendlyne_url: str
    last_seen: float = field(default_factory=time.monotonic)

    def close(self) -> None:
        for provider in self.providers.values():
            if provider.connection is not None:
                try:
                    provider.connection.stop(forget_login=True)
                except Exception:  # noqa: BLE001 - shutting down regardless
                    pass


class Hub:
    """Visitors' provider sets, and pending OAuth logins keyed by their ``state``."""

    def __init__(self) -> None:
        self.loop = EventLoopThread()
        self.lock = threading.Lock()
        self.visitors: dict[str, Visitor] = {}
        self.logins: dict[str, tuple[McpConnection, float]] = {}

    def visitor(self, sid: str, trendlyne_url: str) -> Visitor:
        with self.lock:
            found = self.visitors.get(sid)
            if found is not None and found.trendlyne_url != trendlyne_url:
                self.visitors.pop(sid)
                threading.Thread(target=found.close, daemon=True).start()
                found = None
            if found is None:
                found = Visitor(self.build(trendlyne_url), trendlyne_url)
                self.visitors[sid] = found
            found.last_seen = time.monotonic()
        self.reap()
        return found

    def build(self, trendlyne_url: str) -> dict[str, Provider]:
        redirect_uri = public_base_url() + "/"
        urls = {name: setting(f"{name.upper()}_MCP_URL", default) for name, default in DEFAULT_URLS.items()}
        urls["trendlyne"] = trendlyne_url
        providers: dict[str, Provider] = {}
        for cls in PROVIDER_CLASSES:
            url = urls.get(cls.name)
            connection = None
            if url:
                storage = MemoryTokenStorage(redirect_uri=redirect_uri)
                connection = McpConnection(cls.name, url, self.loop, storage=storage, client_name="Stock Data (Streamlit)")
            providers[cls.name] = cls(connection)
        return providers

    def remember_login(self, connection: McpConnection, auth_url: str) -> None:
        state = parse_qs(urlsplit(auth_url).query).get("state", [""])[0]
        if state:
            with self.lock:
                self.logins[state] = (connection, time.monotonic())

    def take_login(self, state: str) -> McpConnection | None:
        with self.lock:
            entry = self.logins.pop(state, None)
        return entry[0] if entry else None

    def reap(self) -> None:
        now = time.monotonic()
        with self.lock:
            stale = [sid for sid, v in self.visitors.items() if now - v.last_seen > IDLE_LIMIT]
            gone = [self.visitors.pop(sid) for sid in stale]
            for state in [s for s, (_, t) in self.logins.items() if now - t > 900]:
                self.logins.pop(state)
        for visitor in gone:
            threading.Thread(target=visitor.close, daemon=True).start()


@st.cache_resource
def hub() -> Hub:
    return Hub()


# -- OAuth redirect back from a provider ---------------------------------------------------


def handle_oauth_callback() -> None:
    params = st.query_params
    if "state" not in params or not ("code" in params or "error" in params):
        return
    connection = hub().take_login(params.get("state", ""))
    st.title("Stock Data")
    if connection is None or connection.oauth is None:
        st.error("This login link has expired or was already used. Go back to the app and log in again.")
    else:
        connection.oauth.complete(
            code=params.get("code"),
            state=params.get("state"),
            iss=params.get("iss"),
            error=params.get("error"),
            error_description=params.get("error_description"),
        )
        with st.spinner("Finishing the login…"):
            state = connection.wait(60, through_login=True)
        if state == CONNECTED:
            st.success("Connected. You can close this tab and return to the app tab, which updates by itself.")
        else:
            st.error(connection.status().get("error") or "The login did not finish. Try again from the app tab.")
    st.query_params.clear()
    st.stop()


# -- password gate -------------------------------------------------------------------------


def unlocked() -> bool:
    """True if the visitor entered APP_PASSWORD (or none is set)."""
    password = setting("APP_PASSWORD")
    if not password:
        return False
    return bool(st.session_state.get("unlocked"))


def password_gate() -> None:
    password = setting("APP_PASSWORD")
    if not password or st.session_state.get("unlocked"):
        return
    st.title("📈 Stock Data")
    with st.form("unlock"):
        entered = st.text_input("Password", type="password")
        if st.form_submit_button("Open"):
            if hmac.compare_digest(entered.encode(), password.encode()):
                st.session_state.unlocked = True
                st.rerun()
            st.error("Wrong password.")
    st.stop()


# -- helpers ---------------------------------------------------------------------------------


def inr(value: Any, digits: int = 2) -> str:
    if value is None:
        return "–"
    return f"₹{value:,.{digits}f}"


def num(value: Any, digits: int = 2) -> str:
    return "–" if value is None else f"{value:,.{digits}f}"


def pct(value: Any) -> str:
    return "–" if value is None else f"{value:+.2f}%"


def span(low: Any, high: Any) -> str:
    return "–" if low is None and high is None else f"{inr(low)} – {inr(high)}"


def crore(value: Any) -> str:
    return "–" if value is None else f"₹{value:,.0f} Cr"


def big(value: Any) -> str:
    if value is None:
        return "–"
    if value >= 1e7:
        return f"{value / 1e7:,.2f} Cr"
    if value >= 1e5:
        return f"{value / 1e5:,.2f} L"
    return f"{value:,.0f}"


def show_error(exc: ProviderError) -> None:
    if exc.code == "login_required":
        st.info(f"{exc.message} Use the **Data sources** panel in the sidebar.")
    elif exc.code in ("not_configured", "plan_limit", "unsupported", "not_found"):
        st.info(exc.message)
    else:
        st.warning(exc.message)


def attempt(fn: Any, *args: Any) -> tuple[Any, ProviderError | None]:
    try:
        return fn(*args), None
    except ProviderError as exc:
        return None, exc


# -- sidebar: data sources ------------------------------------------------------------------


def provider_panel(providers: dict[str, Provider]) -> None:
    hub_ = hub()
    waiting = False
    for name, provider in providers.items():
        status = provider.status(check_login=True) if name == "kite" and provider.configured else provider.status()  # type: ignore[call-arg]
        state = status.get("state")
        box = st.container(border=True)
        if state == "not_configured":
            box.markdown(f"**{provider.label}** · not set up")
            if name == "trendlyne":
                box.caption("Paste your Trendlyne MCP URL below to turn it on.")
            continue
        if state == CONNECTED and status.get("logged_in", True):
            who = f" as {status['user']}" if status.get("user") else ""
            box.markdown(f"🟢 **{provider.label}** · connected{who}")
            if box.button("Disconnect", key=f"off-{name}"):
                provider.disconnect()
                st.rerun()
            continue
        if state == AUTHORIZING and status.get("auth_url"):
            waiting = True
            hub_.remember_login(provider.connection, status["auth_url"])  # type: ignore[arg-type]
            box.markdown(f"🟡 **{provider.label}** · waiting for login")
            box.link_button(f"Open {provider.label} login", status["auth_url"], use_container_width=True)
            box.caption("Log in on the new tab, then come back here.")
            continue
        if state == CONNECTED and status.get("login") == "kite":
            waiting = True
            box.markdown(f"🟡 **{provider.label}** · log in to Kite")
            if status.get("auth_url"):
                box.link_button("Open Kite login", status["auth_url"], use_container_width=True)
            box.caption("Kite sessions end daily, so you log in once a day.")
            continue
        if state == FAILED:
            box.markdown(f"🔴 **{provider.label}**")
            box.caption(status.get("error") or "Could not connect.")
        else:
            box.markdown(f"⚪ **{provider.label}** · not connected")
        if box.button("Connect" if state != FAILED else "Retry", key=f"on-{name}"):
            with st.spinner(f"Connecting to {provider.label}…"):
                try:
                    provider.connect()
                except ProviderError as exc:
                    st.session_state[f"err-{name}"] = exc.message
            st.rerun()
        if st.session_state.get(f"err-{name}"):
            box.caption(st.session_state.pop(f"err-{name}"))
    if waiting:
        if not st.session_state.get("waiting_for_login"):
            st.session_state.waiting_for_login = True
            st.rerun()  # switch to the self-refreshing panel
    elif st.session_state.pop("waiting_for_login", False):
        st.rerun()


@st.fragment(run_every=3)
def provider_panel_live(providers: dict[str, Provider]) -> None:
    """Re-check while a login is pending, so the page updates when the visitor returns."""
    before = {n: p.status().get("state") for n, p in providers.items()}
    provider_panel(providers)
    after = {n: p.status().get("state") for n, p in providers.items()}
    if before != after:
        st.rerun(scope="app")


# -- main page sections ------------------------------------------------------------------------


def search_box(providers: dict[str, Provider]) -> str | None:
    query = st.text_input("Search a company or NSE symbol", placeholder="e.g. Infosys or INFY", max_chars=60)
    cols = st.columns(len(POPULAR) + 1)
    cols[0].caption("Try")
    for col, symbol in zip(cols[1:], POPULAR):
        if col.button(symbol, key=f"try-{symbol}", use_container_width=True):
            st.session_state.symbol = symbol
    if query.strip() and re.search(r"\w", query):
        results, problems = [], []
        for name in SEARCH_ORDER:
            provider = providers.get(name)
            if provider is None or not provider.configured:
                continue
            try:
                results = provider.search(query.strip())
                break
            except NotConfigured:
                continue
            except ProviderError as exc:
                problems.append(exc)
        if results:
            labels = [f"{r['symbol']} · {r.get('name') or ''} ({r.get('exchange') or 'NSE'})" for r in results]
            choice = st.selectbox("Matches", range(len(results)), format_func=lambda i: labels[i])
            if st.button("Look up", type="primary"):
                st.session_state.symbol = results[choice]["symbol"]
        else:
            try:
                st.session_state.symbol = normalize_symbol(query)
            except ProviderError:
                pass
            for exc in problems:
                show_error(exc)
            if not problems and not any(p.configured for p in providers.values()):
                st.info("Connect a data source in the sidebar to search.")
    return st.session_state.get("symbol")


def quote_header(quote: dict[str, Any]) -> None:
    left, right = st.columns([3, 2])
    left.subheader(quote.get("name") or quote["symbol"])
    meta = [quote["symbol"], quote.get("exchange"), quote.get("market_cap_category")]
    left.caption(" · ".join(str(m).title() if m == quote.get("market_cap_category") else str(m) for m in meta if m))
    right.metric(
        "Price",
        inr(quote.get("price")),
        f"{num(quote.get('change'))} ({pct(quote.get('change_pct'))})" if quote.get("change") is not None else None,
    )
    if quote.get("updated_at"):
        right.caption(f"{quote['updated_at']} · via {quote['source']}")


def chart(history: dict[str, Any], style: str) -> None:
    candles = history["candles"]
    if not candles:
        st.info("No candles for this range.")
        return
    df = pd.DataFrame(candles)
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.78, 0.22], vertical_spacing=0.03)
    x = df["time"]
    if style == "Candles":
        fig.add_trace(
            go.Candlestick(
                x=x, open=df["open"], high=df["high"], low=df["low"], close=df["close"], name="Price",
                increasing_line_color="#0e9f6e", decreasing_line_color="#e02424",
            ),
            row=1, col=1,
        )
    else:
        fig.add_trace(go.Scatter(x=x, y=df["close"], mode="lines", name="Close", line={"color": "#1c64f2"}), row=1, col=1)
    colors = ["#0e9f6e" if c >= o else "#e02424" for o, c in zip(df["open"], df["close"])]
    fig.add_trace(go.Bar(x=x, y=df["volume"], marker_color=colors, opacity=0.5, name="Volume"), row=2, col=1)
    # A category axis has no gaps for nights, weekends and holidays.
    fig.update_xaxes(type="category", nticks=8, rangeslider_visible=False)
    fig.update_layout(height=520, showlegend=False, margin={"l": 10, "r": 10, "t": 10, "b": 10}, yaxis_tickprefix="₹")
    st.plotly_chart(fig, use_container_width=True)
    s = history.get("summary") or {}
    if s:
        st.caption(
            f"{pct(s.get('change_pct'))} over {RANGES[history['range']]} · High {inr(s.get('high'))} · "
            f"Low {inr(s.get('low'))} · Avg volume {big(s.get('avg_volume'))} · {s.get('count')} × {history['interval']} candles"
        )
    if history.get("note"):
        st.caption(history["note"])


def key_stats(quote: dict[str, Any]) -> None:
    rows = [
        ("Open", inr(quote.get("open"))),
        ("Previous close", inr(quote.get("prev_close"))),
        ("Day range", span(quote.get("low"), quote.get("high"))),
        ("52-week range", span(quote.get("week52_low"), quote.get("week52_high"))),
        ("Volume", big(quote.get("volume"))),
        ("Market cap", crore(quote.get("market_cap_cr"))),
        ("Circuit limits", span(quote.get("lower_circuit"), quote.get("upper_circuit"))),
    ]
    st.dataframe(pd.DataFrame(rows, columns=["", "Value"]), hide_index=True, use_container_width=True)


def analyst_views(quote: dict[str, Any] | None, insights: dict[str, Any] | None) -> None:
    a = (quote or {}).get("analyst")
    if a:
        st.markdown(f"**INDmoney consensus:** {a.get('consensus') or '–'}")
        st.caption(
            f"Buy {num(a.get('buy_pct'), 1)}% · Hold {num(a.get('hold_pct'), 1)}% · Sell {num(a.get('sell_pct'), 1)}% · "
            f"{num(a.get('analysts'), 0)} analysts"
        )
        st.markdown(
            f"Mean target **{inr(a.get('target_mean'))}** ({pct(a.get('upside_pct'))}) · "
            f"range {inr(a.get('target_low'))} – {inr(a.get('target_high'))}"
        )
    t = (insights or {}).get("analysts") or {}
    if t.get("brokers"):
        st.markdown(f"**Trendlyne brokers:** {t.get('consensus') or '–'}")
        st.caption(
            f"Buy {num(t.get('buy'), 0)} · Hold {num(t.get('hold'), 0)} · Sell {num(t.get('sell'), 0)} · "
            f"{num(t.get('brokers'), 0)} brokers"
        )
        st.markdown(f"Average target **{inr(t.get('avg_target'))}** ({pct(t.get('upside_pct'))})")
    if not a and not t.get("brokers"):
        st.caption("No analyst views for this symbol.")


def metric_table(rows: list[dict[str, Any]]) -> None:
    if not rows:
        st.caption("Nothing to show.")
        return
    table = [
        {"Metric": r["label"], "Value": num(r["value"]) + (f" {r['unit']}" if r.get("unit") else ""), "Insight": r.get("insight") or ""}
        for r in rows
    ]
    st.dataframe(pd.DataFrame(table), hide_index=True, use_container_width=True)


def research(insights: dict[str, Any]) -> None:
    scores = insights.get("scores") or {}
    cols = st.columns(3)
    for col, key in zip(cols, ("durability", "valuation", "momentum")):
        s = scores.get(key) or {}
        col.metric(key.title(), num(s.get("value"), 0), s.get("label") or None, delta_color="off")
    line = []
    if (scores.get("classification") or {}).get("label"):
        line.append(scores["classification"]["label"])
    if (insights.get("buy_sell_zone") or {}).get("label"):
        line.append(f"Buy/sell zone: **{insights['buy_sell_zone']['label']}**")
    v = insights.get("valuation") or {}
    if v.get("label"):
        line.append(f"PE check: **{v['label']}** (fair price {inr(v.get('fair_price'))})")
    c = insights.get("checklist") or {}
    if c.get("label"):
        line.append(f"Checklist: **{num(c.get('passed'), 0)}/{num(c.get('total'), 0)} passed**")
    if line:
        st.markdown(" · ".join(line))
    tabs = st.tabs(["Fundamentals", "Technicals", "Returns", "SWOT", "About"])
    with tabs[0]:
        metric_table(insights.get("fundamentals") or [])
    with tabs[1]:
        metric_table(insights.get("technicals") or [])
    with tabs[2]:
        returns = insights.get("returns") or []
        if returns:
            st.dataframe(pd.DataFrame([{"Period": r["label"], "Return": pct(r["pct"])} for r in returns]), hide_index=True)
        ranges = insights.get("ranges") or []
        if ranges:
            st.dataframe(
                pd.DataFrame([{"Range": r["label"], "Low": inr(r["low"]), "High": inr(r["high"])} for r in ranges]),
                hide_index=True,
            )
    with tabs[3]:
        swot = insights.get("swot") or {}
        cols = st.columns(4)
        for col, part in zip(cols, ("strengths", "weaknesses", "opportunities", "threats")):
            entry = swot.get(part) or {}
            col.markdown(f"**{part.title()}** ({entry.get('count', 0)})")
            for item in entry.get("items", []):
                col.caption(f"• {item}")
    with tabs[4]:
        if insights.get("description"):
            st.write(insights["description"])
        facts = [f"Sector: {insights.get('sector')}" if insights.get("sector") else None,
                 f"Industry: {insights.get('industry')}" if insights.get("industry") else None,
                 f"ISIN: {insights.get('isin')}" if insights.get("isin") else None]
        st.caption(" · ".join(f for f in facts if f))
        if insights.get("url"):
            st.link_button("More on Trendlyne", insights["url"])


def news(items: list[dict[str, Any]]) -> None:
    if not items:
        st.caption("No recent news.")
    for item in items:
        title = f"[{item['title']}]({item['url']})" if item.get("url") else item["title"]
        st.markdown(f"**{title}**")
        meta = " · ".join(str(m) for m in (item.get("published"), item.get("source"), item.get("type")) if m)
        if meta:
            st.caption(meta)
        if item.get("summary"):
            st.write(item["summary"])


# -- the page --------------------------------------------------------------------------------------


def main() -> None:
    handle_oauth_callback()
    password_gate()

    if "sid" not in st.session_state:
        st.session_state.sid = secrets.token_urlsafe(16)

    with st.sidebar:
        st.header("Data sources")
        own_url = setting("TRENDLYNE_MCP_URL")
        if own_url and unlocked():
            trendlyne_url = own_url
        else:
            trendlyne_url = st.text_input(
                "Your Trendlyne MCP URL (optional)", type="password", key="trendlyne_url",
                help="trendlyne.com > profile menu > MCP. Kept in this browser session only.",
            ).strip()
            if trendlyne_url and not trendlyne_url.startswith("https://"):
                st.error("The Trendlyne URL must start with https://")
                trendlyne_url = ""
        visitor = hub().visitor(st.session_state.sid, trendlyne_url)
        providers = visitor.providers
        if st.session_state.get("waiting_for_login"):
            provider_panel_live(providers)
        else:
            provider_panel(providers)
        st.caption("Logins stay in this browser session only and are never saved. Read-only: the app cannot trade.")

    st.title("📈 Stock Data")
    st.caption("Indian stock data from INDmoney, Kite (Zerodha) and Trendlyne.")
    symbol = search_box(providers)
    if not symbol:
        return

    available = [n for n in PRICE_SOURCES if providers[n].configured]
    c1, c2, c3 = st.columns([2, 4, 2])
    source = c1.segmented_control("Price data", available, default=available[0] if available else None,
                                  format_func=lambda n: providers[n].label, key="source") if available else None
    source_ranges = providers[source].ranges if source else tuple(RANGES)
    range_key = c2.segmented_control("Range", list(source_ranges), default="1y" if "1y" in source_ranges else source_ranges[0],
                                     format_func=str.upper, key=f"range-{source}") or "1y"
    style = c3.segmented_control("Chart", ["Candles", "Line"], default="Candles", key="style") or "Candles"

    trendlyne = providers["trendlyne"]
    with st.spinner(f"Loading {symbol}…"), concurrent.futures.ThreadPoolExecutor(4) as pool:
        quote_f = pool.submit(attempt, providers[source].quote, symbol) if source else None
        history_f = pool.submit(attempt, providers[source].history, symbol, range_key) if source else None
        insights_f = pool.submit(attempt, trendlyne.insights, symbol) if trendlyne.configured else None
        news_f = pool.submit(attempt, trendlyne.news, symbol) if trendlyne.configured else None
        quote, quote_err = quote_f.result() if quote_f else (None, None)
        history, history_err = history_f.result() if history_f else (None, None)
        insights, insights_err = insights_f.result() if insights_f else (None, None)
        items, news_err = news_f.result() if news_f else (None, None)

    if quote:
        quote_header(quote)
    elif quote_err:
        st.subheader(symbol)
        show_error(quote_err)
    else:
        st.subheader(symbol)
        st.info("Connect INDmoney or Kite in the sidebar for prices and charts.")

    if history:
        chart(history, style)
        st.download_button(
            "Download CSV", history_csv(history),
            file_name=f"{re.sub(r'[^A-Za-z0-9-]+', '_', symbol)}_{range_key}_{source}.csv", mime="text/csv",
        )
    elif history_err and (quote_err is None or history_err.code != quote_err.code):
        show_error(history_err)  # don't repeat the quote's message

    left, right = st.columns(2)
    with left:
        st.markdown(f"#### Key stats · {providers[source].label}" if source else "#### Key stats")
        if quote:
            key_stats(quote)
        else:
            st.caption("No quote loaded.")
    with right:
        st.markdown("#### Analyst views")
        analyst_views(quote, insights)

    st.markdown("#### Research · Trendlyne")
    if insights:
        research(insights)
    elif insights_err:
        show_error(insights_err)
    else:
        st.caption("Add a Trendlyne MCP URL in the sidebar for scores, fundamentals, technicals and SWOT.")

    st.markdown("#### News and filings · Trendlyne")
    if items is not None:
        news(items)
    elif news_err and not insights_err:
        show_error(news_err)

    st.divider()
    st.caption("Not affiliated with INDmoney, Zerodha or Trendlyne. For information only; not investment advice.")


main()
