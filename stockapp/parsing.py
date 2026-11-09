"""Turn MCP tool results into Python data.

Each provider answers in its own shape:

* INDmoney returns JSON text (its structured content wraps it as ``{"result": "<json>"}``).
* Kite returns JSON text, and plain-text messages for errors and login.
* Trendlyne returns a compact, indented text format with ``|``-separated tables,
  and JSON for errors.
"""

from __future__ import annotations

import html
import json
import re
from typing import Any

from mcp.types import CallToolResult


class ToolError(Exception):
    """The tool reported a failure (an ``is_error`` result or ``{"status": "error"}``)."""


# -- generic results ----------------------------------------------------------


def result_text(result: CallToolResult) -> str:
    """All text content of a tool result, joined."""
    parts = [getattr(item, "text", None) for item in result.content]
    return "\n".join(part for part in parts if isinstance(part, str))


def maybe_json(text: str) -> Any:
    """Parse ``text`` as JSON when it looks like JSON, else return it unchanged."""
    stripped = text.strip()
    if stripped[:1] in ("{", "["):
        try:
            return json.loads(stripped)
        except ValueError:
            pass
    return text


def decode_result(result: CallToolResult) -> Any:
    """Return a tool result as parsed JSON when possible, else as text.

    Raises ToolError when the tool reported an error.
    """
    text = result_text(result)
    if result.is_error:
        raise ToolError(text.strip() or "The tool reported an error.")
    structured = result.structured_content
    if isinstance(structured, dict):
        if set(structured) == {"result"}:
            inner = structured["result"]
            data = maybe_json(inner) if isinstance(inner, str) else inner
        else:
            data = structured
    else:
        data = maybe_json(text)
    if isinstance(data, dict) and str(data.get("status", "")).lower() == "error":
        raise ToolError(str(data.get("message") or data.get("error") or "The tool reported an error."))
    return data


_MARKDOWN_LINK_RE = re.compile(r"\]\((https?://[^)\s]+)\)")
_URL_RE = re.compile(r"https?://[^\s)\]>\"'<]+")


def find_url(text: str) -> str | None:
    """The first link in ``text``, preferring a markdown link's target."""
    match = _MARKDOWN_LINK_RE.search(text)
    if match:
        return match.group(1)
    match = _URL_RE.search(text)
    return match.group(0) if match else None


def safe_http_url(value: Any) -> str | None:
    """``value`` if it is an http(s) URL, else None (keeps javascript: etc. out of links)."""
    if isinstance(value, str) and re.match(r"^https?://", value.strip(), re.IGNORECASE):
        return value.strip()
    return None


def to_number(value: Any) -> float | None:
    """A finite float from numbers or numeric strings, else None."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number not in (float("inf"), float("-inf")) else None


def strip_html(text: Any) -> str:
    """Plain text from an HTML fragment."""
    if not isinstance(text, str):
        return ""
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


# -- Trendlyne's compact text format -------------------------------------------
#
#   status: success                      key: value
#   data:                                key: with an indented block below
#     name | nse_code | bse_code         a table: header row, then data rows
#     Infosys Ltd. | INFY | 500209
#   summaryData:
#     ["Type","Holding"], ["FII",27.44]  JSON arrays, comma separated
#   stockData:
#     Infosys Ltd., 630, Equity          free text

_KEY_RE = re.compile(r"^(?P<key>[\w][\w .&%()/'-]*?):(?:\s(?P<value>.*))?$")
_NUMBER_RE = re.compile(r"^-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?$")


def coerce(cell: str) -> Any:
    """Convert one scalar from the compact format to a Python value."""
    value = cell.strip()
    if value in ("", "None", "null"):
        return None
    if value in ("True", "true"):
        return True
    if value in ("False", "false"):
        return False
    if _NUMBER_RE.match(value):
        return float(value) if any(ch in value for ch in ".eE") else int(value)
    if value[:1] in ("{", "["):
        try:
            return json.loads(value)
        except ValueError:
            pass
    return value


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _cells(content: str) -> list[str]:
    # Split on " |" rather than " | ": a row ending in an empty cell ("a | b | ")
    # has lost its trailing space by the time it gets here.
    return [cell.strip() for cell in content.split(" |")]


def _line_kind(content: str) -> str:
    pipe, colon = content.find(" | "), content.find(": ")
    if pipe != -1 and (colon == -1 or pipe < colon):
        return "row"
    if _KEY_RE.match(content):
        return "key"
    if content.startswith("["):
        return "array"
    return "text"


def parse_compact(text: str) -> dict[str, Any]:
    """Parse Trendlyne's compact format into nested dicts and lists."""
    lines = [line.rstrip() for line in text.splitlines()]
    lines = [line for line in lines if line.strip()]
    result: dict[str, Any] = {}
    pos = 0
    while pos < len(lines):
        if _line_kind(lines[pos].strip()) == "key":
            block, pos = _parse_mapping(lines, pos, _indent(lines[pos]))
            result.update(block)
        else:
            pos += 1  # stray line at the top level: skip it
    return result


def _parse_block(lines: list[str], pos: int, indent: int) -> tuple[Any, int]:
    kind = _line_kind(lines[pos].strip())
    if kind == "key":
        return _parse_mapping(lines, pos, indent)
    if kind == "row":
        return _parse_table(lines, pos, indent)
    if kind == "array":
        return _parse_arrays(lines, pos, indent)
    texts = []
    while pos < len(lines) and _indent(lines[pos]) >= indent and _line_kind(lines[pos].strip()) == "text":
        texts.append(lines[pos].strip())
        pos += 1
    return "\n".join(texts), pos


def _parse_mapping(lines: list[str], pos: int, indent: int) -> tuple[dict[str, Any], int]:
    mapping: dict[str, Any] = {}
    while pos < len(lines) and _indent(lines[pos]) == indent:
        match = _KEY_RE.match(lines[pos].strip())
        if not match:
            break
        key, value = match.group("key"), match.group("value")
        pos += 1
        if value is not None and value.strip():
            mapping[key] = coerce(value)
        elif pos < len(lines) and _indent(lines[pos]) > indent:
            mapping[key], pos = _parse_block(lines, pos, _indent(lines[pos]))
        else:
            mapping[key] = None
    return mapping, pos


def _parse_table(lines: list[str], pos: int, indent: int) -> tuple[list[dict[str, Any]], int]:
    header = _cells(lines[pos].strip())
    pos += 1
    rows: list[dict[str, Any]] = []
    while pos < len(lines) and _indent(lines[pos]) == indent and _line_kind(lines[pos].strip()) == "row":
        cells = _cells(lines[pos].strip())
        if len(cells) > len(header):  # a cell contained " | ": keep the overflow in the last column
            cells = cells[: len(header) - 1] + [" | ".join(cells[len(header) - 1 :])]
        cells += [""] * (len(header) - len(cells))
        rows.append({name: coerce(cell) for name, cell in zip(header, cells, strict=True)})
        pos += 1
    return rows, pos


def _parse_arrays(lines: list[str], pos: int, indent: int) -> tuple[Any, int]:
    chunks = []
    while pos < len(lines) and _indent(lines[pos]) == indent and lines[pos].strip().startswith("["):
        chunks.append(lines[pos].strip())
        pos += 1
    joined = ", ".join(chunks)
    try:
        return json.loads(f"[{joined}]"), pos
    except ValueError:
        return joined, pos


def table_header(text: str) -> list[str]:
    """Header names of the first table in ``text`` (used for Trendlyne's stockData row)."""
    for line in text.splitlines():
        if _line_kind(line.strip()) == "row":
            return _cells(line.strip())
    return []


# -- Trendlyne news ------------------------------------------------------------
#
# The news table cannot be parsed line by line: descriptionHTML spans several
# lines, and JSON cells ("insight", "related") can contain " | " themselves.


def _is_news_record_start(line: str) -> bool:
    return line.startswith("  ") and not line.startswith("   ") and line.count(" | ") >= 12


def _split_prefix(text: str, count: int) -> tuple[list[str], str]:
    """Split ``count`` cells off the front of ``text``, keeping JSON cells whole."""
    decoder = json.JSONDecoder()
    cells: list[str] = []
    pos = 0
    while len(cells) < count:
        if text.startswith(("{", "["), pos):
            try:
                _, end = decoder.raw_decode(text, pos)
            except ValueError:
                end = -1
            if end != -1 and text.startswith(" | ", end):
                cells.append(text[pos:end])
                pos = end + 3
                continue
        nxt = text.find(" | ", pos)
        if nxt == -1:
            break
        cells.append(text[pos:nxt])
        pos = nxt + 3
    return cells, text[pos:]


def _parse_news_record(record: str, header: list[str]) -> dict[str, Any] | None:
    try:
        title_at, html_at = header.index("title"), header.index("descriptionHTML")
    except ValueError:
        return None
    prefix, rest = _split_prefix(record, title_at)
    if len(prefix) < title_at:
        return None
    tail_count = len(header) - html_at - 1
    tail = rest.rsplit(" | ", tail_count)
    if len(tail) != tail_count + 1:
        return None
    middle, tail = tail[0], tail[1:]
    title, _, after_title = middle.partition(" | ")
    description, _, _ = after_title.partition(" | ")
    values = dict(zip(header, prefix, strict=False))
    values.update(zip(header[html_at + 1 :], tail, strict=True))
    values["title"], values["description"] = title, description
    return {name: coerce(value) if name not in ("title", "description") else value for name, value in values.items()}


def parse_news(text: str) -> list[dict[str, Any]]:
    """Rows of Trendlyne's ``newsList`` table as dicts keyed by its header."""
    lines = text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == "newsList:")
    except StopIteration:
        return []
    if start + 1 >= len(lines):
        return []
    header = [cell.strip() for cell in lines[start + 1].strip().split(" | ")]
    records: list[list[str]] = []
    for line in lines[start + 2 :]:
        if _is_news_record_start(line):
            records.append([line[2:]])
        elif records:
            records[-1].append(line)
    rows = (_parse_news_record("\n".join(chunks), header) for chunks in records)
    return [row for row in rows if row]
