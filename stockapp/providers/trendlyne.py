"""Trendlyne, through its MCP server (needs a Trendlyne MCP subscription).

Each subscriber gets a personal server URL from Trendlyne's MCP page; set it as
TRENDLYNE_MCP_URL. Tools used: ``search_entities`` and
``get_overview_news_corp_events`` (overview and news). Price history
(``get_stock_eod_ohlc``) is not used: it needs the MCP Max or Ultra plan and
covers only 30 trading days, while INDmoney and Kite cover a year or more.
"""

from __future__ import annotations

import re
from typing import Any

from ..parsing import coerce, parse_compact, parse_news, safe_http_url, strip_html, to_number
from .base import NotFound, PlanLimit, Provider, ProviderError, normalize_symbol


class TrendlyneProvider(Provider):
    name = "trendlyne"
    label = "Trendlyne"
    supports = frozenset({"search", "insights", "news"})
    setup_hint = "Copy your MCP server URL from Trendlyne (profile menu > MCP) into TRENDLYNE_MCP_URL."

    def tool_error(self, tool: str, message: str) -> ProviderError:
        if "[code:1013]" in message or re.search(r"\bplan\b", message, re.IGNORECASE):
            return PlanLimit(self.name, f"Trendlyne: {message}")
        return super().tool_error(tool, message)

    def _compact(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        data = self.call_data(tool, arguments)
        return parse_compact(data) if isinstance(data, str) else (data if isinstance(data, dict) else {})

    def search(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        query = query.strip()

        def run() -> list[dict[str, Any]]:
            data = self._compact("search_entities", {"query": query, "entity_type": "stock", "limit": limit})
            results = []
            for row in data.get("data") or []:
                if not isinstance(row, dict):
                    continue
                nse, bse = row.get("nse_code"), row.get("bse_code")
                symbol = str(nse or bse or "").upper()
                if not symbol:
                    continue
                results.append(
                    {
                        "symbol": symbol,
                        "name": row.get("name") or symbol,
                        "exchange": "NSE" if nse else "BSE",
                        "isin": row.get("isin"),
                        "type": row.get("entity_type"),
                        "sector": row.get("sector"),
                        "industry": row.get("industry"),
                        "source": self.name,
                    }
                )
            return results

        return self.cache.get_or_set(("search", query.lower(), limit), 3600, run)

    def insights(self, symbol: str) -> dict[str, Any]:
        symbol = normalize_symbol(symbol)

        def run() -> dict[str, Any]:
            overview = self._compact("get_overview_news_corp_events", {"stock_code": symbol, "type": "overview"})
            if not overview.get("stockData") and not overview.get("fundamentalData"):
                raise NotFound(self.name, f"Trendlyne has no overview for {symbol}.")
            return insights_from_overview(symbol, overview)

        return self.cache.get_or_set(("insights", symbol), 900, run)

    def news(self, symbol: str, limit: int = 12) -> list[dict[str, Any]]:
        symbol = normalize_symbol(symbol)

        def run() -> list[dict[str, Any]]:
            text = self.call_data("get_overview_news_corp_events", {"stock_code": symbol, "type": "news"})
            rows = parse_news(text) if isinstance(text, str) else []
            items = []
            for row in rows:
                summary = strip_html(row.get("description"))
                items.append(
                    {
                        "title": strip_html(row.get("title")) or "(untitled)",
                        "summary": "" if summary.lower() == "enclosed" else summary[:280],
                        "published": row.get("pubDate"),
                        "source": row.get("source") or None,
                        "type": row.get("postType"),
                        "url": safe_http_url(row.get("url")),
                    }
                )
            items.sort(key=lambda item: item["published"] or "", reverse=True)
            return items[:limit]

        return self.cache.get_or_set(("news", symbol, limit), 600, run)


def _stock_row(overview: dict[str, Any]) -> dict[str, Any]:
    """Map the comma-separated ``stockData`` line onto ``stockHeaders``."""
    headers = [h.get("unique_name") for h in overview.get("stockHeaders") or [] if isinstance(h, dict)]
    line = overview.get("stockData")
    if not headers or not isinstance(line, str):
        return {}
    values = [value.strip() for value in line.split(", ")]
    if len(values) != len(headers):  # a value contained ", ": positions are unreliable
        return {}
    return {name: coerce(value) for name, value in zip(headers, values, strict=True) if name}


def _get(mapping: Any, *path: str) -> Any:
    for key in path:
        mapping = mapping.get(key) if isinstance(mapping, dict) else None
    return mapping


def _metric_rows(rows: Any, label_keys: tuple[str, ...]) -> list[dict[str, Any]]:
    out = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        label = next((row[k] for k in label_keys if row.get(k)), None)
        if not label:
            continue
        out.append(
            {
                "label": str(label),
                "value": to_number(row.get("value")),
                "unit": row.get("unit") if row.get("unit") not in ("abs", "None") else None,
                "insight": row.get("st") if row.get("st") != "None" else None,
                "detail": row.get("lt") if row.get("lt") != "None" else None,
                "color": row.get("color") or None,
            }
        )
    return out


def insights_from_overview(symbol: str, overview: dict[str, Any]) -> dict[str, Any]:
    stock = _stock_row(overview)
    datav2 = overview.get("datav2") or {}
    broker = overview.get("brokerSummaryData") or {}
    sector = overview.get("sectorIndustryData") or {}
    recommendations = _get(overview, "forcasterData", "RECOMMENDATION")
    latest_reco = recommendations[-1] if isinstance(recommendations, list) and recommendations else {}

    def score(key: str) -> dict[str, Any]:
        return {
            "value": to_number(stock.get(f"{key}_value")),
            "label": stock.get(f"{key}_insight"),
            "color": stock.get(f"{key}_color"),
        }

    swot = {}
    for part in ("strengths", "weaknesses", "opportunities", "threats"):
        entries = _get(overview, "SWOTData", "tableData", part)
        entries = entries if isinstance(entries, list) else []
        titles = [str(e[1]) for e in entries if isinstance(e, list) and len(e) > 1 and e[1]]
        swot[part] = {"count": len(titles), "items": titles[:6]}

    pe_now = _get(overview, "pe_valuation_data", "PEchartData") or {}
    checklist = overview.get("investmentChecklist") or {}

    return {
        "source": "trendlyne",
        "symbol": symbol,
        "name": stock.get("get_full_name"),
        "nse_code": stock.get("NSEcode"),
        "bse_code": str(stock["BSEcode"]) if stock.get("BSEcode") else None,
        "isin": stock.get("ISIN"),
        "sector": sector.get("sectorName"),
        "industry": stock.get("industry_name") or sector.get("industryName"),
        "price": to_number(stock.get("current_p")),
        "day_change": to_number(stock.get("day_change")),
        "day_change_pct": to_number(stock.get("day_changeP")),
        "updated": overview.get("lastUpdated"),
        "scores": {
            "durability": score("d"),
            "valuation": score("v"),
            "momentum": score("m"),
            "classification": {
                "label": stock.get("DVM_classification_text"),
                "color": stock.get("DVM_classification_color"),
            },
        },
        "returns": [
            {"label": label, "pct": to_number(value)}
            for label, value in (
                ("1W", stock.get("week_changeP")),
                ("1M", stock.get("month_changeP")),
                ("1Y", stock.get("year_changeP")),
                ("2Y", datav2.get("changeP_2yr")),
                ("3Y", datav2.get("changeP_3yr")),
                ("5Y", datav2.get("changeP_5yr")),
                ("10Y", datav2.get("changeP_10yr")),
            )
            if to_number(value) is not None
        ],
        "ranges": [
            {"label": label, "low": to_number(datav2.get(f"{key}Low")), "high": to_number(datav2.get(f"{key}High"))}
            for label, key in (
                ("Day", "day"),
                ("Week", "week"),
                ("Month", "month"),
                ("52 weeks", "year"),
                ("3 years", "threeYear"),
                ("5 years", "fiveYear"),
                ("10 years", "tenYear"),
            )
            if to_number(datav2.get(f"{key}Low")) is not None and to_number(datav2.get(f"{key}High")) is not None
        ],
        "fundamentals": _metric_rows(overview.get("fundamentalData"), ("title", "name")),
        "technicals": _metric_rows(overview.get("technicalData"), ("name",)),
        "analysts": {
            "consensus": _get(latest_reco, "RECO_TEXT", "value"),
            "consensus_text": _get(latest_reco, "INSIGHT", "st"),
            "brokers": to_number(broker.get("broker_covered")),
            "avg_target": to_number(broker.get("broker_avg_target")),
            "upside_pct": to_number(broker.get("broker_average_upside")),
            "buy": to_number(broker.get("broker_buy_1Y")),
            "hold": to_number(broker.get("broker_hold_1Y")),
            "sell": to_number(broker.get("broker_sell_1Y")),
            "target_low": to_number(broker.get("broker_low_1Y")),
            "target_high": to_number(broker.get("broker_high_1Y")),
        },
        "checklist": {
            "label": checklist.get("insight"),
            "passed": to_number(checklist.get("positivesNum")),
            "total": to_number(checklist.get("total")),
            "color": checklist.get("insightColor"),
        },
        "valuation": {
            "label": pe_now.get("st"),
            "fair_price": to_number(pe_now.get("attr1_val")),
            "upside_pct": to_number(pe_now.get("attr2_val")),
            "insight": _get(overview, "pe_valuation_data", "PEInsight"),
        },
        "buy_sell_zone": {
            "label": _get(overview, "buySellData", "insight"),
            "color": _get(overview, "buySellData", "color"),
        },
        "price_insight": {"label": stock.get("price_insight"), "color": stock.get("price_insight_color")},
        "swot": swot,
        "description": _get(overview, "corporateInfo", "companyInfo"),
        "url": safe_http_url(_get(overview, "manualInsight", "url")),
    }
