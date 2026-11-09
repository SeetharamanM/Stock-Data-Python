from __future__ import annotations

import pytest

from stockapp.parsing import (
    ToolError,
    coerce,
    decode_result,
    find_url,
    parse_compact,
    parse_news,
    safe_http_url,
    strip_html,
    to_number,
)

from .conftest import text_result
from .fake_servers import fixture


def test_decode_unwraps_structured_result_holding_json():
    data = decode_result(text_result(fixture("indmoney_lookup_infosys.json")))
    assert data[0] == {"ind_key": "INDS00577", "name": "Infosys"}


def test_decode_plain_json_text_and_plain_text():
    assert decode_result(text_result('{"a": 1}', structured=False)) == {"a": 1}
    assert decode_result(text_result("hello", structured=False)) == "hello"


def test_decode_raises_for_error_results_and_status_error_json():
    with pytest.raises(ToolError, match="log in"):
        decode_result(text_result("Please log in first", error=True))
    with pytest.raises(ToolError, match="MCP Max"):
        decode_result(text_result(fixture("trendlyne_plan_error.json")))


def test_compact_format_tables_mappings_and_arrays():
    data = parse_compact(fixture("trendlyne_overview_infy.txt"))
    assert data["technicalData"][0]["name"] == "Relative Strength Index"
    assert data["technicalData"][0]["value"] == 58.6
    pe = data["fundamentalData"][1]
    assert (pe["title"], pe["value"], pe["unit"]) == ("PE TTM", 15.5, None)  # empty middle cell
    assert data["stockHeaders"][0]["unique_name"] == "get_full_name"  # row ending in an empty cell
    assert data["asmData"] == {"url": None, "lt": None, "st": None, "color": None, "text": None}
    assert data["summaryData"][1] == ["FII", 27.44, 263883046]
    assert len(data["SWOTData"]["tableData"]["strengths"]) == 18
    reco = data["forcasterData"]["RECOMMENDATION"][-1]
    assert reco["STRONG_BUY"] == {"value": 11.0, "class": "d_buy"}  # JSON cell
    assert data["lastUpdated"] == "09 Nov, 2026 12:41 PM (IST)"
    assert "Fairly Valued" in data["pe_valuation_data"]["pe_image_map"]  # key with a space
    assert data["stockData"].startswith("Infosys Ltd., 630, Equity, INFY")


def test_compact_search_table():
    data = parse_compact(fixture("trendlyne_search_infosys.txt"))
    assert data["status"] == "success"
    assert [row["nse_code"] for row in data["data"]] == ["INFY", "MINFY", "INFOBEAN"]


def test_news_records_with_multiline_html_and_json_cells():
    rows = parse_news(fixture("trendlyne_news_infy.txt"))
    assert len(rows) == 5
    assert all(isinstance(r["postId"], int) and r["url"].startswith("https://") for r in rows)
    assert rows[1]["title"].startswith("Infosys shares slide over 2%")  # its "related" JSON contains " | "
    assert rows[-1]["postType"] == "Rapid Results"  # its HTML description spans several lines
    assert rows[-1]["source"] == "Trendlyne"


def test_news_without_table_is_empty():
    assert parse_news("status: success\n") == []


def test_find_url_prefers_markdown_link():
    assert find_url(fixture("kite_login.txt")).startswith("https://kite.zerodha.com/connect/login?api_key=kitemcp")
    assert find_url("visit https://example.com/x now") == "https://example.com/x"
    assert find_url("no link") is None


@pytest.mark.parametrize(
    ("cell", "expected"),
    [("None", None), ("", None), ("True", True), ("12", 12), ("-0.83", -0.83), ('{"a":1}', {"a": 1}), ("NSE", "NSE")],
)
def test_coerce(cell, expected):
    assert coerce(cell) == expected


def test_small_helpers():
    assert strip_html("<p>A &amp; B<!-- x --></p>\n<b>C</b>") == "A & B C"
    assert safe_http_url("javascript:alert(1)") is None
    assert safe_http_url(" https://a.b/c ") == "https://a.b/c"
    assert to_number("12.5") == 12.5
    assert to_number("Infinity") is None
    assert to_number(True) is None
