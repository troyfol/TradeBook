"""Report data layer — filters + per-grouping breakdowns.

Each report function takes a list of trade dicts (already pre-filtered)
and returns a `Report` object the GUI knows how to render via
`ReportTableModel` + `CategoryBarChart`.

Reports defined here:
    - by_symbol           — per-ticker rollup
    - by_direction        — Long vs Short
    - by_ticker_price     — entry-price buckets

The remaining time / streak / drawdown reports live in their own modules
to keep this file focused.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterable, Optional

from analytics.metrics import TradeMetrics, compute_metrics


# ---- shared types ---------------------------------------------------------


@dataclass(frozen=True)
class ReportColumn:
    key: str
    label: str
    fmt: str = "str"           # "str" | "currency" | "pct" | "int" | "float" | "duration"
    color_by_sign: bool = False
    align: str = "left"        # "left" | "right" | "center"


@dataclass
class Report:
    title: str
    columns: list[ReportColumn]
    rows: list[dict] = field(default_factory=list)
    chart_x_key: Optional[str] = None
    chart_y_key: Optional[str] = None
    chart_kind: str = "none"        # "none" | "bar"
    chart_title: str = ""
    empty_message: str = "No data."


# ---- filter spec ----------------------------------------------------------


@dataclass
class FilterSpec:
    start: Optional[date] = None
    end: Optional[date] = None
    symbols: Optional[list[str]] = None    # uppercased
    direction: Optional[str] = None         # "Long" / "Short" / None
    result: Optional[str] = None            # "win" / "loss" / None
    min_hold_seconds: int = 0               # 0 = no lower bound
    max_hold_seconds: int = 0               # 0 = no upper bound


def _exit_date(t: dict) -> Optional[date]:
    et = t.get("exit_time")
    if et is None:
        return None
    return et.date() if isinstance(et, datetime) else et


def apply_filters(
    trades: Iterable[dict],
    spec: FilterSpec,
) -> list[dict]:
    """Apply a `FilterSpec` to a list of (closed) trade dicts.

    Open trades (no exit_time) are silently excluded — every report in
    Phase 6 operates on closed trades only.
    """
    sym_set = (
        set(s.upper() for s in spec.symbols) if spec.symbols else None
    )
    out: list[dict] = []
    for t in trades:
        d = _exit_date(t)
        if d is None:
            continue
        if spec.start and d < spec.start:
            continue
        if spec.end and d > spec.end:
            continue
        if sym_set is not None and t.get("symbol", "").upper() not in sym_set:
            continue
        if spec.direction and t.get("direction") != spec.direction:
            continue
        if spec.result is not None:
            net = t.get("net_pnl")
            if net is None:
                continue
            is_win = float(net) >= 0.0
            if spec.result == "win" and not is_win:
                continue
            if spec.result == "loss" and is_win:
                continue
        if spec.min_hold_seconds:
            hd = t.get("hold_duration_seconds") or 0
            if hd < spec.min_hold_seconds:
                continue
        if spec.max_hold_seconds:
            hd = t.get("hold_duration_seconds") or 0
            if hd > spec.max_hold_seconds:
                continue
        out.append(t)
    return out


# ---- shared row builder ---------------------------------------------------


def _metrics_to_row(label: str, metrics: TradeMetrics) -> dict:
    """Flatten a TradeMetrics into the row schema used by every breakdown."""
    return {
        "label": label,
        "trades": metrics.total_trades,
        "net_pnl": metrics.total_pnl,
        "win_rate": metrics.win_rate,
        "avg_winner": metrics.avg_winner,
        "avg_loser": metrics.avg_loser,
        "profit_factor": metrics.profit_factor,
        "expectancy": metrics.expectancy,
    }


# Standard column set used by symbol / direction / day-of-week / etc.
def _standard_columns(label_text: str) -> list[ReportColumn]:
    return [
        ReportColumn("label", label_text, "str", align="left"),
        ReportColumn("trades", "Trades", "int", align="right"),
        ReportColumn("net_pnl", "Net P&L", "currency", color_by_sign=True, align="right"),
        ReportColumn("win_rate", "Win Rate", "pct", align="right"),
        ReportColumn("expectancy", "Expectancy", "currency", color_by_sign=True, align="right"),
        ReportColumn("avg_winner", "Avg Winner", "currency", color_by_sign=True, align="right"),
        ReportColumn("avg_loser", "Avg Loser", "currency", color_by_sign=True, align="right"),
        ReportColumn("profit_factor", "PF", "float", align="right"),
    ]


# ---- report: by symbol ----------------------------------------------------


def by_symbol(trades: list[dict]) -> Report:
    by: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        sym = t.get("symbol") or "?"
        by[sym].append(t)

    rows = [
        _metrics_to_row(sym, compute_metrics(group))
        for sym, group in by.items()
    ]
    rows.sort(key=lambda r: r["net_pnl"], reverse=True)

    return Report(
        title="Performance by Symbol",
        columns=_standard_columns("Symbol"),
        rows=rows,
        chart_x_key="label",
        chart_y_key="net_pnl",
        chart_kind="bar",
        chart_title="Net P&L by Symbol",
        empty_message="No trades match the current filters.",
    )


# ---- report: by direction -------------------------------------------------


def by_direction(trades: list[dict]) -> Report:
    longs = [t for t in trades if t.get("direction") == "Long"]
    shorts = [t for t in trades if t.get("direction") == "Short"]
    rows: list[dict] = []
    if longs:
        rows.append(_metrics_to_row("Long", compute_metrics(longs)))
    if shorts:
        rows.append(_metrics_to_row("Short", compute_metrics(shorts)))
    return Report(
        title="Performance by Direction",
        columns=_standard_columns("Direction"),
        rows=rows,
        chart_x_key="label",
        chart_y_key="net_pnl",
        chart_kind="bar",
        chart_title="Net P&L: Long vs Short",
        empty_message="No trades match the current filters.",
    )


# ---- report: by ticker price ----------------------------------------------

# Default entry-price bin edges (dollars). User-overridable from the GUI.
DEFAULT_PRICE_EDGES: list[float] = [
    0.0, 1.0, 2.0, 5.0, 10.0, 20.0, 50.0, float("inf"),
]


def _format_price(v: float) -> str:
    """Render a price edge for a bucket label. Drops .0 on whole numbers."""
    if v == int(v):
        return str(int(v))
    return f"{v:g}"


def make_price_buckets(
    edges: list[float],
) -> list[tuple[float, float, str]]:
    """Build (lo, hi, label) tuples from a sorted list of edges."""
    out: list[tuple[float, float, str]] = []
    for i in range(len(edges) - 1):
        lo, hi = edges[i], edges[i + 1]
        if i == 0 and lo == 0.0 and not math.isinf(hi):
            label = f"< ${_format_price(hi)}"
        elif math.isinf(hi):
            label = f"${_format_price(lo)}+"
        else:
            label = f"${_format_price(lo)} – ${_format_price(hi)}"
        out.append((lo, hi, label))
    return out


# Backwards-compatible name kept for any external imports / tests.
PRICE_BUCKETS: list[tuple[float, float, str]] = make_price_buckets(
    DEFAULT_PRICE_EDGES
)


def by_ticker_price(
    trades: list[dict],
    edges: Optional[list[float]] = None,
) -> Report:
    """Bucket closed trades by avg_entry_price.

    `edges` overrides the default bucket layout. Must be a strictly
    increasing list of floats; the last edge can be `float('inf')`.
    """
    buckets = make_price_buckets(edges) if edges else PRICE_BUCKETS

    by: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        price = t.get("avg_entry_price")
        if price is None:
            continue
        try:
            p = float(price)
        except (TypeError, ValueError):
            continue
        for lo, hi, label in buckets:
            if lo <= p < hi:
                by[label].append(t)
                break

    # Preserve canonical bucket order even if some buckets are empty.
    ordered_labels = [b[2] for b in buckets]
    rows = [
        _metrics_to_row(label, compute_metrics(by[label]))
        for label in ordered_labels
        if label in by
    ]

    return Report(
        title="Performance by Entry Price",
        columns=_standard_columns("Entry Price"),
        rows=rows,
        chart_x_key="label",
        chart_y_key="net_pnl",
        chart_kind="bar",
        chart_title="Net P&L by Entry-Price Bucket",
        empty_message="No trades match the current filters.",
    )
