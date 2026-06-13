"""Parser for TradeStation sequential order exports (paste or CSV).

Input format: tab-delimited, 12 columns:
    Entered | Type | Symbol | Order Status | Stop | Limit | Filled Price |
    Quantity | Filled/Canceled | Qty Filled | Qty Left | Commission

Tolerant of:
    - leading/trailing blank lines
    - leading empty columns on a row
    - trailing empty columns on a row
    - optional header row (detected by 'Entered' in the first non-empty cell)
    - mixed filled / rejected / canceled rows (non-filled rows are filtered out)

Returns rich dicts keyed by canonical field names. Also computes `import_hash`
per row for dedup, using a SHA1 of entered_at|symbol|type|filled_price|quantity|
filled_canceled_at to disambiguate identical partial fills.
"""
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from config import TS_DATETIME_FORMAT, STATUS_FILLED

# Symbol parsing: ROOT | ROOT.X | ROOT(XX)
_SYMBOL_RE = re.compile(r"^([A-Z]+)(?:\.([A-Z])|\(([A-Z]+)\))?$")

EXPECTED_COLS = 12


@dataclass
class ParseError:
    line_no: int
    raw: str
    error: str


def parse_symbol(raw: str) -> tuple[str, Optional[str], Optional[str]]:
    """Return (root, extension, extension_type).

    extension_type is 'dot', 'parenthetical', or None.
    If the raw symbol doesn't match the expected pattern, it is returned
    as-is with no extension metadata.
    """
    if not raw:
        return raw, None, None
    m = _SYMBOL_RE.match(raw.strip())
    if not m:
        return raw, None, None
    root, dot_ext, paren_ext = m.groups()
    if dot_ext:
        return root, dot_ext, "dot"
    if paren_ext:
        return root, paren_ext, "parenthetical"
    return root, None, None


def _parse_float(s: str) -> Optional[float]:
    s = s.strip()
    if not s:
        return None
    val = float(s.replace(",", ""))
    if not math.isfinite(val):
        raise ValueError(f"non-finite value: {s!r}")
    return val


def _parse_optional_price(s: str) -> Optional[float]:
    """Parse a price column that may legitimately contain non-numeric
    labels instead of a number.

    TradeStation puts the literal string ``"Market"`` in the Limit
    column for a market order (and ``"Stop"`` / similar in the Stop
    column for stop orders). Those aren't prices — they're the
    order-type marker — so return ``None`` instead of raising.
    Empty strings also yield ``None``.
    """
    s = s.strip()
    if not s:
        return None
    cleaned = s.lstrip("$").replace(",", "")
    try:
        val = float(cleaned)
    except ValueError:
        return None
    if not math.isfinite(val):
        return None
    return val


def _parse_int(s: str) -> int:
    s = s.strip()
    if not s:
        return 0
    return int(s.replace(",", ""))


def _parse_money(s: str) -> float:
    """Parse currency like '$1.00' or '-$0.50'. Empty → 0.0."""
    s = s.strip()
    if not s:
        return 0.0
    negative = s.startswith("-")
    s = s.lstrip("-").lstrip("$")
    val = float(s.replace(",", ""))
    if not math.isfinite(val):
        raise ValueError(f"non-finite currency value: {s!r}")
    return -val if negative else val


def _parse_datetime(s: str) -> Optional[datetime]:
    s = s.strip()
    if not s:
        return None
    return datetime.strptime(s, TS_DATETIME_FORMAT)


def compute_import_hash(row: dict) -> str:
    """SHA1 over fields that identify a unique fill.

    Includes filled_canceled_at so two identical partial fills at the same
    entered_at/price/qty don't collide when they differ on fill timestamp.

    Public so callers that edit an execution after parsing (e.g. the
    New Trade preview's inline editor) can refresh the hash before
    import-time dedup runs.
    """
    entered = row["entered_at"].isoformat() if row["entered_at"] else ""
    fc = row["filled_canceled_at"].isoformat() if row["filled_canceled_at"] else ""
    key = "|".join([
        entered,
        row["symbol"] or "",
        row["type"] or "",
        f"{row['filled_price']:.6f}" if row["filled_price"] is not None else "",
        str(row["quantity"]),
        fc,
    ])
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


# Backwards-compat alias — internal callers still reference the
# underscore name.
_compute_import_hash = compute_import_hash


def _normalize_cells(line: str) -> list[str]:
    """Split on tab, strip each cell, and trim leading/trailing empties."""
    cells = [c.strip() for c in line.split("\t")]
    # trim trailing empties
    while cells and cells[-1] == "":
        cells.pop()
    # trim leading empties
    while cells and cells[0] == "":
        cells.pop(0)
    return cells


def _is_header_row(cells: list[str]) -> bool:
    return bool(cells) and cells[0].lower() == "entered"


def _parse_row(cells: list[str]) -> dict:
    if len(cells) < EXPECTED_COLS:
        raise ValueError(f"expected {EXPECTED_COLS} columns, got {len(cells)}")
    # if more than expected, trim to first EXPECTED_COLS
    cells = cells[:EXPECTED_COLS]
    (entered_s, type_, raw_sym, status, stop_s, limit_s,
     fp_s, qty_s, fc_s, qf_s, ql_s, comm_s) = cells

    entered_at = _parse_datetime(entered_s)
    if entered_at is None:
        raise ValueError("missing Entered timestamp")

    symbol, ext, ext_type = parse_symbol(raw_sym)
    filled_price = _parse_float(fp_s)

    row = {
        "entered_at": entered_at,
        "type": type_,
        "symbol": symbol,
        "raw_symbol": raw_sym,
        "symbol_extension": ext,
        "extension_type": ext_type,
        "order_status": status,
        # Stop / Limit tolerate TradeStation's order-type labels
        # ("Market", "Stop", etc.) by returning None — those columns
        # aren't always numeric.
        "stop_price": _parse_optional_price(stop_s),
        "limit_price": _parse_optional_price(limit_s),
        "filled_price": filled_price,
        "quantity": _parse_int(qty_s),
        "filled_canceled_at": _parse_datetime(fc_s),
        "qty_filled": _parse_int(qf_s),
        "qty_left": _parse_int(ql_s),
        "commission": _parse_money(comm_s),
    }
    row["import_hash"] = _compute_import_hash(row)
    return row


def parse_paste(text: str) -> tuple[list[dict], list[ParseError]]:
    """Parse a TradeStation paste/CSV blob.

    Returns (filled_executions, errors). Non-filled rows are silently
    filtered out (they're expected). Errors are only for rows that look
    like data but fail to parse.
    """
    executions: list[dict] = []
    errors: list[ParseError] = []

    for line_no, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        cells = _normalize_cells(line)
        if not cells:
            continue
        if _is_header_row(cells):
            continue

        try:
            row = _parse_row(cells)
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            errors.append(ParseError(line_no=line_no, raw=line, error=str(e)))
            continue

        # Row counts as an execution if it either (a) has status
        # Filled, or (b) reports a real partial fill (qty_filled > 0
        # and a filled_price) regardless of status. UROut / Rejected
        # rows can legitimately carry a partial fill when the router
        # bailed mid-order — the filled portion is still real money.
        is_filled = row["order_status"] == STATUS_FILLED
        has_partial_fill = (
            row["qty_filled"] > 0 and row["filled_price"] is not None
        )
        if not is_filled and not has_partial_fill:
            continue
        if row["filled_price"] is None:
            errors.append(ParseError(
                line_no=line_no, raw=line,
                error=f"{row['order_status']} row has qty_filled but no Filled Price",
            ))
            continue
        executions.append(row)

    return executions, errors
