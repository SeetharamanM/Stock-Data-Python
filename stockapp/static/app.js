"use strict";

/* Stock Data front end. All data comes from the Flask JSON API (see stockapp/__init__.py). */

const config = JSON.parse(document.getElementById("app-config").textContent);
const $ = (id) => document.getElementById(id);
const LABELS = { indmoney: "INDmoney", kite: "Kite", trendlyne: "Trendlyne" };
const RANGE_KEYS = config.ranges.map((r) => r.key);
const RANGE_LABELS = Object.fromEntries(config.ranges.map((r) => [r.key, r.label]));
const VIEW_KEY = "stock-data:view";

const state = {
  providers: Object.fromEntries(config.providers.map((p) => [p.name, p])),
  symbol: null,
  name: null,
  source: null,
  range: "1y",
  chartType: "candles",
  quote: null,
  history: null,
  insights: null,
  load: 0, // bumped for every new request batch, so stale answers are dropped
  suggestions: new Map(),
};

/* ---------- formatting (Indian number grouping) ---------- */

const nf2 = new Intl.NumberFormat("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const nf0 = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 });
const isNum = (v) => typeof v === "number" && Number.isFinite(v);
const sign = (v) => (v > 0 ? "+" : v < 0 ? "−" : "");
const fmt = {
  price: (v) => (isNum(v) ? `₹${nf2.format(v)}` : "—"),
  num: (v, digits = 2) => (isNum(v) ? new Intl.NumberFormat("en-IN", { maximumFractionDigits: digits }).format(v) : "—"),
  int: (v) => (isNum(v) ? nf0.format(v) : "—"),
  signed: (v) => (isNum(v) ? `${sign(v)}${nf2.format(Math.abs(v))}` : "—"),
  pct: (v) => (isNum(v) ? `${sign(v)}${nf2.format(Math.abs(v))}%` : "—"),
  compact(v) {
    if (!isNum(v)) return "—";
    const a = Math.abs(v);
    if (a >= 1e7) return `${nf2.format(v / 1e7)} Cr`;
    if (a >= 1e5) return `${nf2.format(v / 1e5)} L`;
    if (a >= 1e3) return `${nf2.format(v / 1e3)} K`;
    return nf0.format(v);
  },
  crore: (v) => (isNum(v) ? `₹${nf0.format(v)} Cr` : "—"),
  range: (lo, hi, f = fmt.price) => (isNum(lo) && isNum(hi) ? `${f(lo)} – ${f(hi)}` : "—"),
  when(iso) {
    const d = iso ? new Date(iso) : null;
    if (!d || Number.isNaN(d.getTime())) return "";
    return d.toLocaleString("en-IN", { timeZone: "Asia/Kolkata", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) + " IST";
  },
  date(iso) {
    const d = iso ? new Date(iso) : null;
    if (!d || Number.isNaN(d.getTime())) return "";
    return d.toLocaleDateString("en-IN", { timeZone: "Asia/Kolkata", day: "numeric", month: "short", year: "numeric" });
  },
};
const direction = (v) => (v > 0 ? "up" : v < 0 ? "down" : "");
const colorClass = (c) => (["positive", "negative", "neutral"].includes(c) ? c : "");
const titleCase = (s) => String(s).toLowerCase().replace(/\b\w/g, (m) => m.toUpperCase());
const safeUrl = (u) => (typeof u === "string" && /^https?:\/\//i.test(u) ? u : null);

/* ---------- DOM helpers ---------- */

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value == null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child == null || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

let noticeTimer = null;
function notify(message, kind = "", { sticky = false } = {}) {
  const box = $("notice");
  clearTimeout(noticeTimer);
  if (!message) {
    box.hidden = true;
    return;
  }
  box.className = `notice ${kind}`;
  box.textContent = message;
  box.hidden = false;
  if (!sticky) noticeTimer = setTimeout(() => (box.hidden = true), 6000);
}

/* ---------- API ---------- */

class ApiError extends Error {
  constructor(body, status) {
    super((body && body.error) || `Request failed (HTTP ${status}).`);
    this.body = body || {};
    this.status = status;
    this.code = this.body.code;
  }
}

async function api(path, { method = "GET" } = {}) {
  const headers = { Accept: "application/json" };
  if (method !== "GET") headers["X-Requested-With"] = "stockapp";
  let response;
  try {
    response = await fetch(path, { method, headers });
  } catch {
    throw new ApiError({ error: "Could not reach the app server. Is it still running?" }, 0);
  }
  let body = null;
  try {
    body = await response.json();
  } catch {
    /* not JSON */
  }
  if (!response.ok) throw new ApiError(body, response.status);
  return body;
}

/* ---------- data sources ---------- */

const isReady = (p) => Boolean(p && p.configured && p.state === "connected" && p.logged_in !== false);

function setProvider(status) {
  state.providers[status.name] = status;
  renderSources();
  renderSourceSwitch();
}

function rememberView() {
  try {
    sessionStorage.setItem(VIEW_KEY, JSON.stringify({ symbol: state.symbol, name: state.name, source: state.source, range: state.range }));
  } catch {
    /* storage unavailable */
  }
}

function oauthLink(p, text) {
  return el("a", { class: "button primary", href: p.auth_url, onclick: rememberView }, text);
}

function kiteLoginButton(p, text = "Log in to Kite") {
  return el("button", {
    type: "button",
    class: "primary",
    onclick: () => {
      window.open(p.auth_url, "_blank", "noopener");
      watchKiteLogin();
      notify("Finish logging in to Kite in the new tab. This page updates by itself.", "", { sticky: true });
    },
  }, text);
}

async function connectSource(name, { interactive = true } = {}) {
  setProvider({ ...state.providers[name], state: "connecting", error: null });
  try {
    const status = await api(`/api/providers/${name}/connect`, { method: "POST" });
    setProvider(status);
    if (interactive && status.state === "authorizing" && safeUrl(status.auth_url)) {
      rememberView();
      window.location.assign(status.auth_url); // the provider sends you back to /oauth/<name>/callback
    } else if (interactive && status.state === "connected" && status.logged_in === false) {
      notify("Kite needs a login: use the Log in to Kite button.", "warn");
    } else if (interactive && isReady(status)) {
      notify(`${LABELS[name]} connected.`, "ok");
      if (state.symbol) loadStock(state.symbol);
    }
    return status;
  } catch (err) {
    setProvider({ ...state.providers[name], state: "failed", error: err.message });
    return null;
  }
}

async function disconnectSource(name) {
  try {
    setProvider(await api(`/api/providers/${name}/disconnect`, { method: "POST" }));
    notify(`${LABELS[name]} disconnected and its saved login removed.`);
  } catch (err) {
    notify(err.message, "error");
  }
}

let kiteTimer = null;
function watchKiteLogin() {
  if (kiteTimer) return;
  const started = Date.now();
  kiteTimer = setInterval(async () => {
    if (Date.now() - started > 5 * 60 * 1000) {
      clearInterval(kiteTimer);
      kiteTimer = null;
      return;
    }
    try {
      const status = await api("/api/providers/kite?check=1");
      setProvider(status);
      if (status.logged_in) {
        clearInterval(kiteTimer);
        kiteTimer = null;
        notify(`Kite connected${status.user ? ` as ${status.user}` : ""}.`, "ok");
        if (state.symbol) loadStock(state.symbol);
      }
    } catch {
      /* keep waiting */
    }
  }, 3000);
}

function sourceView(p) {
  if (!p.configured) return { dot: "", text: p.setup_hint || "Not set up", actions: [] };
  const connect = (label = "Connect") => el("button", { type: "button", onclick: () => connectSource(p.name) }, label);
  switch (p.state) {
    case "connecting":
      return { dot: "wait", text: "Connecting…", actions: [el("span", { class: "spinner", "aria-hidden": "true" })] };
    case "authorizing":
      return { dot: "wait", text: "Log in to finish connecting", actions: safeUrl(p.auth_url) ? [oauthLink(p, "Log in")] : [connect()] };
    case "connected":
      if (p.logged_in === false && safeUrl(p.auth_url)) return { dot: "wait", text: "Kite login needed (once a day)", actions: [kiteLoginButton(p, "Log in")] };
      return {
        dot: "on",
        text: p.user ? `Connected as ${p.user}` : "Connected",
        actions: [el("button", { type: "button", class: "link-button", onclick: () => disconnectSource(p.name), title: "Disconnect and forget the saved login" }, "Disconnect")],
      };
    case "failed":
      return { dot: "err", text: p.error || "Connection failed", error: true, actions: [connect("Retry")] };
    default:
      return { dot: "", text: p.saved_login ? "Saved login found" : "Not connected", actions: [connect()] };
  }
}

function renderSources() {
  const list = $("sources");
  list.replaceChildren(
    ...Object.values(state.providers).map((p) => {
      const view = sourceView(p);
      return el(
        "li",
        { class: "source" },
        el(
          "div",
          { class: "source-info" },
          el("span", { class: `dot ${view.dot}`, "aria-hidden": "true" }),
          el(
            "div",
            { class: "source-text" },
            el("div", { class: "source-name" }, p.label || LABELS[p.name]),
            el("div", { class: `source-state ${view.error ? "error" : ""}`, title: view.text }, view.text),
          ),
        ),
        el("div", { class: "source-actions" }, view.actions),
      );
    }),
  );
}

/* ---------- toolbar ---------- */

function defaultSource() {
  const ready = config.priceSources.find((n) => isReady(state.providers[n]));
  return ready || config.priceSources.find((n) => state.providers[n] && state.providers[n].configured) || config.priceSources[0];
}

function rangesFor(source) {
  const p = state.providers[source];
  return p && p.ranges ? p.ranges : RANGE_KEYS;
}

function renderSourceSwitch() {
  const box = $("source-switch");
  box.replaceChildren(
    el("span", { class: "visually-hidden" }, "Price data from"),
    ...config.priceSources.map((name) => {
      const p = state.providers[name] || {};
      return el(
        "button",
        {
          type: "button",
          "aria-pressed": String(state.source === name),
          disabled: !p.configured,
          title: p.configured ? `Prices from ${LABELS[name]}` : `${LABELS[name]} is not set up`,
          onclick: () => {
            if (state.source === name) return;
            state.source = name;
            if (!rangesFor(name).includes(state.range)) state.range = "1y";
            renderSourceSwitch();
            renderRangeSwitch();
            if (state.symbol) loadPrices();
          },
        },
        LABELS[name],
      );
    }),
  );
}

function renderRangeSwitch() {
  const allowed = rangesFor(state.source);
  $("range-switch").replaceChildren(
    ...RANGE_KEYS.map((key) =>
      el(
        "button",
        {
          type: "button",
          "aria-pressed": String(state.range === key),
          disabled: !allowed.includes(key),
          title: allowed.includes(key) ? RANGE_LABELS[key] : `${LABELS[state.source]} does not offer ${RANGE_LABELS[key]} here`,
          onclick: () => {
            if (state.range === key) return;
            state.range = key;
            renderRangeSwitch();
            if (state.symbol) loadPrices({ historyOnly: true });
          },
        },
        key.toUpperCase(),
      ),
    ),
  );
}

/* ---------- loading a stock ---------- */

function saveView(push) {
  const params = new URLSearchParams({ symbol: state.symbol, source: state.source, range: state.range });
  const url = `/?${params}`;
  if (push) history.pushState(null, "", url);
  else history.replaceState(null, "", url);
  try {
    localStorage.setItem(VIEW_KEY, JSON.stringify({ symbol: state.symbol, name: state.name, source: state.source, range: state.range }));
  } catch {
    /* storage unavailable */
  }
}

function loadStock(symbol, { name, source, range, push = false } = {}) {
  symbol = symbol.trim().toUpperCase();
  if (symbol !== state.symbol) {
    state.quote = state.history = state.insights = null;
    state.name = name || state.suggestions.get(symbol) || null;
  } else if (name) {
    state.name = name;
  }
  state.symbol = symbol;
  if (source && config.priceSources.includes(source)) state.source = source;
  if (!state.source) state.source = defaultSource();
  if (range && RANGE_LABELS[range]) state.range = range;
  if (!rangesFor(state.source).includes(state.range)) state.range = "1y";
  $("symbol").value = symbol;
  saveView(push);

  $("empty").hidden = true;
  $("stock").hidden = false;
  renderSourceSwitch();
  renderRangeSwitch();
  renderHeader();
  renderAnalysts();
  const token = ++state.load;
  loadPrices({ token });
  loadResearch(token);
}

function loadPrices({ historyOnly = false, token } = {}) {
  token = token || ++state.load;
  const sym = encodeURIComponent(state.symbol);
  const source = state.source;
  saveView(false);
  if (!historyOnly) {
    $("stats-source").textContent = LABELS[source];
    $("stats").classList.add("loading");
    api(`/api/quote/${sym}?source=${source}`)
      .then((q) => token === state.load && renderQuote(q))
      .catch((err) => token === state.load && quoteFailed(err));
  }
  $("chart").classList.add("loading");
  $("period-summary").textContent = "Loading prices…";
  api(`/api/history/${sym}?source=${source}&range=${state.range}`)
    .then((h) => token === state.load && renderHistory(h))
    .catch((err) => token === state.load && historyFailed(err));
}

function loadResearch(token) {
  const sym = encodeURIComponent(state.symbol);
  const trendlyne = state.providers.trendlyne;
  if (!trendlyne || !trendlyne.configured) {
    showMessage($("research-body"), new ApiError({ code: "not_configured", provider: "trendlyne", error: trendlyne ? trendlyne.setup_hint : "Trendlyne is not set up." }, 409));
    $("news").replaceChildren();
    showMessage($("news-msg"), new ApiError({ code: "not_configured", provider: "trendlyne", error: "News comes from Trendlyne, which is not set up." }, 409));
    renderAnalysts();
    return;
  }
  $("research-body").replaceChildren(el("p", { class: "muted" }, el("span", { class: "spinner" }), " Loading Trendlyne research…"));
  $("news").replaceChildren();
  $("news-msg").hidden = true;
  api(`/api/insights/${sym}`)
    .then((i) => token === state.load && renderInsights(i))
    .catch((err) => {
      if (token !== state.load) return;
      state.insights = null;
      showMessage($("research-body"), err);
      renderAnalysts();
    });
  api(`/api/news/${sym}`)
    .then((items) => token === state.load && renderNews(items))
    .catch((err) => token === state.load && showMessage($("news-msg"), err));
}

/* ---------- messages inside panels ---------- */

function showMessage(box, err, { retry } = {}) {
  const body = err.body || {};
  const provider = state.providers[body.provider];
  const parts = [el("p", {}, err.message)];
  const actions = [];
  if (body.code === "login_required" && provider) {
    if (body.login === "kite" && safeUrl(body.auth_url)) {
      actions.push(kiteLoginButton({ ...provider, auth_url: body.auth_url }));
    } else if (safeUrl(body.auth_url)) {
      actions.push(oauthLink({ ...provider, auth_url: body.auth_url }, `Log in to ${LABELS[provider.name]}`));
    } else {
      actions.push(el("button", { type: "button", class: "primary", onclick: () => connectSource(provider.name) }, `Connect ${LABELS[provider.name]}`));
    }
  } else if (body.code === "unsupported" && box === $("chart-msg")) {
    const other = config.priceSources.find((n) => n !== state.source && rangesFor(n).includes(state.range) && state.providers[n] && state.providers[n].configured);
    if (other) {
      actions.push(el("button", {
        type: "button",
        onclick: () => {
          state.source = other;
          renderSourceSwitch();
          renderRangeSwitch();
          loadPrices();
        },
      }, `Use ${LABELS[other]}`));
    }
  } else if (retry && !["not_configured", "not_found", "plan_limit", "invalid_request"].includes(body.code)) {
    actions.push(el("button", { type: "button", onclick: retry }, "Try again"));
  }
  if (actions.length) parts.push(el("div", { class: "msg-actions" }, actions));
  if (box.id === "research-body") box.replaceChildren(el("div", { class: "panel-msg" }, parts));
  else {
    box.replaceChildren(...parts);
    box.hidden = false;
  }
}

/* ---------- quote, header and stats ---------- */

function bestName() {
  return (state.insights && state.insights.name) || (state.quote && state.quote.source === "indmoney" && state.quote.name) || state.name || state.symbol;
}

function renderHeader() {
  $("q-name").textContent = bestName();
  const q = state.quote;
  const meta = [state.symbol, (q && q.exchange) || "NSE"];
  if (q && q.market_cap_category) meta.push(titleCase(q.market_cap_category));
  if (state.insights && state.insights.industry) meta.push(state.insights.industry);
  $("q-meta").textContent = meta.join(" · ");
  document.title = `${state.symbol} · Stock Data`;

  let price = q && q.price;
  let change = q && q.change;
  let pct = q && q.change_pct;
  let updated = "";
  if (q) {
    const when = fmt.when(q.updated_at);
    updated = [when ? `${q.price_mode === "live" ? "Live" : "As of"} ${when}` : null, `via ${LABELS[q.source]}`].filter(Boolean).join(" · ");
  } else if (state.history && state.history.candles.length) {
    const last = state.history.candles[state.history.candles.length - 1];
    price = last.close;
    change = pct = null;
    updated = `Last candle ${last.time} · via ${LABELS[state.history.source]}`;
  }
  $("q-price").textContent = fmt.price(price);
  const changeBox = $("q-change");
  changeBox.className = `change ${direction(change)}`;
  changeBox.textContent = isNum(change) ? `${change > 0 ? "▲" : change < 0 ? "▼" : ""} ${fmt.signed(change)} (${fmt.pct(pct)})` : "";
  $("q-updated").textContent = updated;
}

function renderQuote(q) {
  state.quote = q;
  renderHeader();
  const rows = [
    ["Open", fmt.price(q.open)],
    ["Previous close", fmt.price(q.prev_close)],
    ["Day range", fmt.range(q.low, q.high)],
    ["52-week range", fmt.range(q.week52_low, q.week52_high)],
    ["Volume", fmt.int(q.volume)],
    ["Average price", isNum(q.average_price) ? fmt.price(q.average_price) : null],
    ["Market cap", isNum(q.market_cap_cr) ? fmt.crore(q.market_cap_cr) : null],
    ["Circuit limits", isNum(q.lower_circuit) && isNum(q.upper_circuit) ? fmt.range(q.lower_circuit, q.upper_circuit) : null],
  ].filter(([, value]) => value != null && value !== "—");
  $("stats").classList.remove("loading");
  $("stats").replaceChildren(...rows.map(([k, v]) => el("div", {}, el("dt", {}, k), el("dd", {}, v))));
  $("stats-msg").hidden = true;
  renderAnalysts();
}

function quoteFailed(err) {
  state.quote = null;
  renderHeader();
  $("stats").classList.remove("loading");
  $("stats").replaceChildren();
  showMessage($("stats-msg"), err, { retry: () => loadPrices() });
  renderAnalysts();
}

/* ---------- analysts ---------- */

function analystBar(buy, hold, sell) {
  const total = (buy || 0) + (hold || 0) + (sell || 0);
  if (!total) return null;
  const w = (v) => `width:${((v || 0) / total) * 100}%`;
  return el("div", { class: "bar", role: "img", "aria-label": `Buy ${buy}, hold ${hold}, sell ${sell}` },
    el("span", { class: "buy", style: w(buy) }), el("span", { class: "hold", style: w(hold) }), el("span", { class: "sell", style: w(sell) }));
}

function badgeFor(text) {
  const t = String(text || "").toUpperCase();
  const tone = /BUY|OUTPERFORM/.test(t) ? "positive" : /SELL|UNDERPERFORM/.test(t) ? "negative" : "neutral";
  return el("span", { class: `badge ${tone}` }, t);
}

function renderAnalysts() {
  const box = $("analysts");
  const blocks = [];
  const a = state.quote && state.quote.analyst;
  if (a) {
    blocks.push(el("div", { class: "analyst-block" },
      el("div", { class: "analyst-head" }, el("strong", {}, "INDmoney consensus"), a.consensus ? badgeFor(a.consensus) : null),
      analystBar(a.buy_pct, a.hold_pct, a.sell_pct),
      el("div", { class: "legend" },
        el("span", {}, `Buy ${fmt.num(a.buy_pct, 1)}%`), el("span", {}, `Hold ${fmt.num(a.hold_pct, 1)}%`), el("span", {}, `Sell ${fmt.num(a.sell_pct, 1)}%`),
        isNum(a.analysts) ? el("span", {}, `${fmt.int(a.analysts)} analysts`) : null),
      el("dl", { class: "stats", style: "margin-top:12px" },
        el("div", {}, el("dt", {}, "Mean target"), el("dd", {}, fmt.price(a.target_mean))),
        el("div", {}, el("dt", {}, "Upside"), el("dd", { class: direction(a.upside_pct) }, fmt.pct(a.upside_pct))),
        el("div", {}, el("dt", {}, "Target range"), el("dd", {}, fmt.range(a.target_low, a.target_high)))),
    ));
  }
  const t = state.insights && state.insights.analysts;
  if (t && (isNum(t.avg_target) || t.consensus)) {
    blocks.push(el("div", { class: "analyst-block" },
      el("div", { class: "analyst-head" }, el("strong", {}, "Trendlyne broker summary"), t.consensus ? badgeFor(t.consensus) : null),
      analystBar(t.buy, t.hold, t.sell),
      el("div", { class: "legend" },
        el("span", {}, `Buy ${fmt.int(t.buy)}`), el("span", {}, `Hold ${fmt.int(t.hold)}`), el("span", {}, `Sell ${fmt.int(t.sell)}`),
        isNum(t.brokers) ? el("span", {}, `${fmt.int(t.brokers)} brokers`) : null),
      el("dl", { class: "stats", style: "margin-top:12px" },
        el("div", {}, el("dt", {}, "Average target"), el("dd", {}, fmt.price(t.avg_target))),
        el("div", {}, el("dt", {}, "Upside"), el("dd", { class: direction(t.upside_pct) }, fmt.pct(t.upside_pct))),
        el("div", {}, el("dt", {}, "1-year target range"), el("dd", {}, fmt.range(t.target_low, t.target_high)))),
      t.consensus_text ? el("p", { class: "muted small", style: "margin-top:8px" }, t.consensus_text) : null,
    ));
  }
  box.replaceChildren(...(blocks.length ? blocks : [el("p", { class: "muted" }, "Analyst views come from INDmoney (when it is the price source) and Trendlyne.")]));
}

/* ---------- chart and history table ---------- */

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function tickLabel(time, intraday, rangeKey) {
  if (intraday) {
    const [day, clock] = time.split(" ");
    if (rangeKey === "1d") return clock;
    const d = new Date(`${day}T00:00:00`);
    return `${d.getDate()} ${d.toLocaleString("en-IN", { month: "short" })}`;
  }
  const d = new Date(`${time}T00:00:00`);
  const month = d.toLocaleString("en-IN", { month: "short" });
  return ["1y", "5y"].includes(rangeKey) ? `${month} ’${String(d.getFullYear()).slice(2)}` : `${d.getDate()} ${month}`;
}

function drawChart() {
  const h = state.history;
  const box = $("chart");
  if (!h || !h.candles.length) return;
  if (!window.Plotly) {
    box.hidden = true;
    showMessage($("chart-msg"), new ApiError({ error: "The chart library could not load (it comes from cdn.jsdelivr.net). The table below has the same data." }, 0));
    return;
  }
  box.hidden = false;
  const c = h.candles;
  const x = c.map((k) => k.time);
  const [up, down, muted, border, accent, text, surface] = ["--up", "--down", "--muted", "--border", "--accent", "--text", "--surface"].map(cssVar);
  const price =
    state.chartType === "candles"
      ? {
          type: "candlestick", x, open: c.map((k) => k.open), high: c.map((k) => k.high), low: c.map((k) => k.low), close: c.map((k) => k.close),
          name: state.symbol, increasing: { line: { color: up, width: 1 }, fillcolor: up }, decreasing: { line: { color: down, width: 1 }, fillcolor: down },
        }
      : { type: "scatter", mode: "lines", x, y: c.map((k) => k.close), name: "Close", line: { color: accent, width: 2 }, hovertemplate: "₹%{y:,.2f}<extra></extra>" };
  const hasVolume = c.some((k) => k.volume > 0);
  const traces = [price];
  if (hasVolume) {
    traces.push({
      type: "bar", x, y: c.map((k) => k.volume), name: "Volume", yaxis: "y2",
      marker: { color: c.map((k) => (k.close >= k.open ? up : down)), opacity: 0.35 }, hovertemplate: "Volume %{y:,}<extra></extra>",
    });
  }
  let tickvals;
  if (h.range === "5d") {
    tickvals = x.filter((t, i) => i === 0 || t.slice(0, 10) !== x[i - 1].slice(0, 10)); // first candle of each session
  } else {
    const step = Math.max(1, Math.ceil(x.length / (box.clientWidth < 520 ? 4 : 7)));
    tickvals = x.filter((_, i) => i % step === Math.floor(step / 2)); // keeps labels off the edges
  }
  const layout = {
    margin: { l: 36, r: 70, t: 8, b: 30 },
    paper_bgcolor: "rgba(0,0,0,0)",
    plot_bgcolor: "rgba(0,0,0,0)",
    font: { family: getComputedStyle(document.body).fontFamily, color: muted, size: 11 },
    showlegend: false,
    hovermode: "x unified",
    hoverlabel: { bgcolor: surface, bordercolor: border, font: { color: text } },
    xaxis: {
      type: "category", // no gaps for weekends, holidays or special sessions
      tickvals, ticktext: tickvals.map((t) => tickLabel(t, h.intraday, h.range)), tickangle: 0,
      showgrid: false, linecolor: border, rangeslider: { visible: false },
      showspikes: true, spikemode: "across", spikecolor: muted, spikethickness: 1, spikedash: "dot",
    },
    yaxis: { domain: hasVolume ? [0.25, 1] : [0, 1], side: "right", gridcolor: border, zeroline: false, tickformat: ",.2f", tickprefix: "₹" },
    yaxis2: { domain: [0, 0.18], side: "right", showgrid: false, zeroline: false, tickformat: "~s" },
  };
  window.Plotly.react(box, traces, layout, {
    responsive: true, displaylogo: false, modeBarButtonsToRemove: ["select2d", "lasso2d", "autoScale2d", "toImage"],
  });
}

function renderHistory(h) {
  state.history = h;
  $("chart").classList.remove("loading");
  $("chart-msg").hidden = true;
  $("table-source").textContent = LABELS[h.source];
  const sym = encodeURIComponent(h.symbol);
  $("csv-link").href = `/api/history/${sym}?source=${h.source}&range=${h.range}&format=csv`;
  $("time-col").textContent = h.intraday ? "Time (IST)" : "Date";

  const s = h.summary;
  if (!s) {
    $("chart").hidden = true;
    showMessage($("chart-msg"), new ApiError({ error: "No candles for this range yet (the market may be closed)." }, 0));
    $("period-summary").textContent = "";
    $("history-table").tBodies[0].replaceChildren();
    $("table-note").textContent = "";
    if (!state.quote) renderHeader();
    return;
  }
  const summary = el("span", {},
    el("strong", { class: direction(s.change_pct) }, fmt.pct(s.change_pct)),
    ` over ${RANGE_LABELS[h.range]} · High ${fmt.price(s.high)} · Low ${fmt.price(s.low)}`,
    isNum(s.avg_volume) ? ` · Avg volume ${fmt.compact(s.avg_volume)}` : "",
    ` · ${s.count} × ${h.interval} candles`);
  $("period-summary").replaceChildren(summary);
  drawChart();

  const rows = [];
  const candles = h.candles;
  const shown = Math.min(candles.length, 500);
  for (let i = candles.length - 1; i >= candles.length - shown; i--) {
    const k = candles[i];
    const prev = candles[i - 1];
    const chg = prev ? ((k.close - prev.close) / prev.close) * 100 : null;
    rows.push(el("tr", {},
      el("td", {}, k.time), el("td", {}, nf2.format(k.open)), el("td", {}, nf2.format(k.high)), el("td", {}, nf2.format(k.low)),
      el("td", {}, nf2.format(k.close)), el("td", { class: direction(chg) }, chg == null ? "—" : fmt.pct(chg)), el("td", {}, fmt.int(k.volume))));
  }
  $("history-table").tBodies[0].replaceChildren(...rows);
  const notes = [`${candles.length} candles, newest first${shown < candles.length ? ` (table shows the latest ${shown}; the CSV has all)` : ""}.`];
  if (h.note) notes.push(h.note);
  $("table-note").textContent = notes.join(" ");
  if (!state.quote) renderHeader();
}

function historyFailed(err) {
  state.history = null;
  $("chart").classList.remove("loading");
  $("chart").hidden = true;
  $("period-summary").textContent = "";
  $("history-table").tBodies[0].replaceChildren();
  $("table-note").textContent = "";
  $("csv-link").removeAttribute("href");
  showMessage($("chart-msg"), err, { retry: () => loadPrices({ historyOnly: true }) });
}

/* ---------- Trendlyne research and news ---------- */

function metricValue(m) {
  if (!isNum(m.value)) return "—";
  const unit = String(m.unit || "");
  if (/^cr\.?$/i.test(unit)) return fmt.crore(m.value);
  if (/^rs\.?$/i.test(unit)) return fmt.price(m.value);
  if (unit === "%") return `${fmt.num(m.value)}%`;
  return fmt.num(m.value);
}

function metricTable(title, rows) {
  if (!rows || !rows.length) return null;
  return el("div", {}, el("h3", {}, title),
    el("table", { class: "metric-table" }, el("tbody", {}, rows.map((m) =>
      el("tr", {}, el("th", { scope: "row", title: m.detail || "" }, m.label),
        el("td", { class: colorClass(m.color) === "positive" ? "up" : colorClass(m.color) === "negative" ? "down" : "" }, metricValue(m)),
        el("td", { class: "note" }, m.insight || ""))))));
}

function renderInsights(i) {
  state.insights = i;
  renderHeader();
  renderAnalysts();
  const s = i.scores || {};
  const tile = (label, score) =>
    el("div", { class: `score ${colorClass(score && score.color)}` },
      el("div", { class: "label" }, label),
      el("div", { class: "value" }, score && isNum(score.value) ? fmt.num(score.value, 1) : "—", el("small", {}, " / 100")),
      el("div", { class: "meter" }, el("span", { style: `width:${score && isNum(score.value) ? Math.max(0, Math.min(100, score.value)) : 0}%` })),
      el("div", { class: "insight" }, (score && score.label) || ""));

  const line = [];
  if (i.scores && i.scores.classification && i.scores.classification.label) line.push(el("span", { class: `badge ${colorClass(i.scores.classification.color)}` }, i.scores.classification.label));
  if (i.buy_sell_zone && i.buy_sell_zone.label) line.push(el("span", {}, "Buy/sell zone: ", el("strong", {}, i.buy_sell_zone.label)));
  if (i.valuation && i.valuation.label) {
    line.push(el("span", {}, "PE check: ", el("strong", {}, i.valuation.label),
      isNum(i.valuation.fair_price) ? ` (fair price ${fmt.price(i.valuation.fair_price)})` : ""));
  }
  if (i.checklist && i.checklist.label) line.push(el("span", {}, "Checklist: ", el("strong", {}, `${fmt.int(i.checklist.passed)}/${fmt.int(i.checklist.total)} passed`)));
  if (i.price_insight && i.price_insight.label) line.push(el("span", { class: colorClass(i.price_insight.color) === "negative" ? "down" : "" }, i.price_insight.label));

  const returns = (i.returns || []).length
    ? el("dl", { class: "returns", "aria-label": "Price returns" }, i.returns.map((r) =>
        el("div", {}, el("dt", {}, r.label), el("dd", { class: direction(r.pct) }, fmt.pct(r.pct)))))
    : null;

  const swotNames = { strengths: "Strengths", weaknesses: "Weaknesses", opportunities: "Opportunities", threats: "Threats" };
  const swot = i.swot
    ? el("div", { class: "swot" }, Object.entries(swotNames).map(([key, label]) => {
        const part = i.swot[key] || { count: 0, items: [] };
        return el("div", {}, el("span", { class: "muted small" }, label), el("strong", {}, String(part.count)),
          part.items.length ? el("ul", {}, part.items.slice(0, 3).map((t) => el("li", {}, t))) : null);
      }))
    : null;

  const url = safeUrl(i.url);
  $("research-body").replaceChildren(
    el("div", { class: "scores" }, tile("Durability", s.durability), tile("Valuation", s.valuation), tile("Momentum", s.momentum)),
    line.length ? el("div", { class: "research-line" }, line) : null,
    returns,
    el("div", { class: "grid two" }, metricTable("Fundamentals", i.fundamentals), metricTable("Technicals", i.technicals)),
    swot,
    i.description ? el("p", { class: "description" }, i.description, url ? el("span", {}, " ", el("a", { href: url, target: "_blank", rel: "noopener noreferrer" }, "More on Trendlyne")) : null) : null,
    i.updated ? el("p", { class: "muted small", style: "margin-top:8px" }, `Updated ${i.updated}`) : null,
  );
}

function renderNews(items) {
  $("news-msg").hidden = true;
  if (!items.length) {
    showMessage($("news-msg"), new ApiError({ error: "No recent news or filings." }, 0));
    $("news").replaceChildren();
    return;
  }
  $("news").replaceChildren(...items.map((n) => {
    const url = safeUrl(n.url);
    const title = url ? el("a", { href: url, target: "_blank", rel: "noopener noreferrer" }, n.title) : el("strong", {}, n.title);
    return el("li", {}, title,
      el("div", { class: "meta" }, [n.source, n.type, fmt.date(n.published)].filter(Boolean).join(" · ")),
      n.summary ? el("p", { class: "summary" }, n.summary) : null);
  }));
}

/* ---------- search ---------- */

let searchTimer = null;
let searchSeq = 0;
function suggest(query) {
  clearTimeout(searchTimer);
  if (query.length < 2 || state.suggestions.has(query.toUpperCase())) return;
  searchTimer = setTimeout(async () => {
    const seq = ++searchSeq;
    try {
      const data = await api(`/api/search?q=${encodeURIComponent(query)}`);
      if (seq !== searchSeq) return;
      $("suggestions").replaceChildren(...data.results.map((r) => {
        state.suggestions.set(r.symbol, r.name);
        return el("option", { value: r.symbol, label: `${r.name} · ${r.exchange}` });
      }));
    } catch {
      /* suggestions are optional */
    }
  }, 300);
}

async function submitSearch(raw) {
  const query = raw.trim();
  if (!query) return;
  const upper = query.toUpperCase();
  if (state.suggestions.has(upper)) {
    loadStock(upper, { push: true });
    return;
  }
  // "Infosys" is a name, "INFY" a symbol: search first and prefer an exact symbol match.
  const looksLikeSymbol = /^[A-Z0-9&.\-]{1,20}$/.test(upper);
  notify(`Searching for “${query}”…`, "", { sticky: true });
  try {
    const data = await api(`/api/search?q=${encodeURIComponent(query)}`);
    const match = data.results.find((r) => r.symbol === upper) || data.results[0];
    if (match) {
      notify("");
      loadStock(match.symbol, { name: match.name, push: true });
    } else if (looksLikeSymbol) {
      notify("");
      loadStock(upper, { push: true });
    } else if (data.problems && data.problems.length) {
      notify(`Search needs a connected data source. ${data.problems[0].error}`, "warn", { sticky: true });
    } else {
      notify(`No stocks match “${query}”. Try the NSE symbol, e.g. INFY.`, "warn");
    }
  } catch (err) {
    if (looksLikeSymbol) {
      notify("");
      loadStock(upper, { push: true });
    } else {
      notify(err.message, "error");
    }
  }
}

/* ---------- start-up ---------- */

function restoredView() {
  for (const storage of [sessionStorage, localStorage]) {
    try {
      const view = JSON.parse(storage.getItem(VIEW_KEY) || "null");
      if (view && view.symbol) return view;
    } catch {
      /* ignore */
    }
  }
  return null;
}

function handleLoginReturn(params) {
  const connected = params.get("connected");
  const failedProvider = params.get("provider");
  const error = params.get("login_error");
  if (connected && LABELS[connected]) notify(`${LABELS[connected]} connected.`, "ok");
  else if (error) notify(`${LABELS[failedProvider] || "Login"}: ${error}`, "error", { sticky: true });
  if (connected || error) history.replaceState(null, "", "/");
  return Boolean(connected || error);
}

function init() {
  const params = new URLSearchParams(location.search);
  const returned = handleLoginReturn(params);
  renderSources();

  $("search-form").addEventListener("submit", (event) => {
    event.preventDefault();
    submitSearch($("symbol").value);
  });
  $("symbol").addEventListener("input", (event) => suggest(event.target.value.trim()));
  document.querySelectorAll(".chips button").forEach((b) => b.addEventListener("click", () => loadStock(b.dataset.symbol, { push: true })));
  document.querySelectorAll("#type-switch button").forEach((b) =>
    b.addEventListener("click", () => {
      state.chartType = b.dataset.type;
      document.querySelectorAll("#type-switch button").forEach((o) => o.setAttribute("aria-pressed", String(o === b)));
      drawChart();
    }));
  window.addEventListener("popstate", () => {
    const p = new URLSearchParams(location.search);
    if (p.get("symbol")) loadStock(p.get("symbol"), { source: p.get("source"), range: p.get("range") });
  });
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", drawChart);

  // Reconnect sources in the background (saved logins make this silent).
  for (const p of Object.values(state.providers)) {
    if (p.configured && ["idle", "failed"].includes(p.state)) connectSource(p.name, { interactive: false });
    else if (p.name === "kite" && p.state === "connected") api("/api/providers/kite?check=1").then(setProvider).catch(() => {});
  }

  const view = params.get("symbol")
    ? { symbol: params.get("symbol"), source: params.get("source"), range: params.get("range") }
    : returned || !location.search
      ? restoredView()
      : null;
  state.source = defaultSource();
  renderSourceSwitch();
  renderRangeSwitch();
  if (view && view.symbol) loadStock(view.symbol, view);
}

document.addEventListener("DOMContentLoaded", init);
