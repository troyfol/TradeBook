"""Time-based report breakdowns: day-of-week, hour-of-day, hold-time."""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime
from typing import Optional

from analytics.metrics import compute_metrics
from analytics.reports import (
    Report, ReportColumn, _metrics_to_row, _standard_columns,
)

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _entry_dt(t: dict) -> Optional[datetime]:
    et = t.get("entry_time")
    if isinstance(et, datetime):
        return et
    return None


# ---- by day of week --------------------------------------------------------


def by_day_of_week(trades: list[dict]) -> Report:
    """Group by entry-time weekday. Mon–Fri only (matches calendar tab)."""
    by: dict[int, list[dict]] = defaultdict(list)
    for t in trades:
        dt = _entry_dt(t)
        if dt is None:
            continue
        wd = dt.weekday()
        if wd > 4:
            continue
        by[wd].append(t)

    rows = []
    for wd in range(5):
        if wd in by:
            rows.append(_metrics_to_row(DAY_NAMES[wd], compute_metrics(by[wd])))
    return Report(
        title="Performance by Day of Week",
        columns=_standard_columns("Day"),
        rows=rows,
        chart_x_key="label",
        chart_y_key="net_pnl",
        chart_kind="bar",
        chart_title="Net P&L by Day of Week",
        empty_message="No trades match the current filters.",
    )


# ---- by hour of day --------------------------------------------------------


def by_hour_of_day(trades: list[dict]) -> Report:
    """Group by entry-time hour (0–23, local). Empty hours are dropped."""
    by: dict[int, list[dict]] = defaultdict(list)
    for t in trades:
        dt = _entry_dt(t)
        if dt is None:
            continue
        by[dt.hour].append(t)

    rows = []
    for hour in sorted(by.keys()):
        rows.append(
            _metrics_to_row(f"{hour:02d}:00", compute_metrics(by[hour]))
        )
    return Report(
        title="Performance by Hour of Day",
        columns=_standard_columns("Entry Hour"),
        rows=rows,
        chart_x_key="label",
        chart_y_key="net_pnl",
        chart_kind="bar",
        chart_title="Net P&L by Entry Hour",
        empty_message="No trades match the current filters.",
    )


# ---- by hold time ----------------------------------------------------------

# Default bin edges in MINUTES — convenient for the user-facing dialog.
# Internally we convert to seconds for bucket math.
DEFAULT_HOLD_EDGES_MINUTES: list[float] = [
    0.0, 5.0, 30.0, 120.0, 480.0, float("inf"),
]


def _hold_unit(seconds: float) -> str:
    if seconds < 60:
        return "s"
    if seconds < 3600:
        return "min"
    if seconds < 86400:
        return "h"
    return "d"


def _hold_value_only(seconds: float) -> str:
    """Render only the numeric portion of a hold-time edge in its native unit."""
    if seconds < 60:
        return f"{int(seconds)}"
    if seconds < 3600:
        m = seconds / 60
        return f"{int(m)}" if m == int(m) else f"{m:g}"
    if seconds < 86400:
        h = seconds / 3600
        return f"{int(h)}" if h == int(h) else f"{h:g}"
    d = seconds / 86400
    return f"{int(d)}" if d == int(d) else f"{d:g}"


def _format_hold(seconds: float) -> str:
    """Render a hold-time edge with its native unit suffix."""
    return f"{_hold_value_only(seconds)} {_hold_unit(seconds)}"


def make_hold_buckets(
    edges_seconds: list[float],
) -> list[tuple[float, float, str]]:
    """Build (lo, hi, label) tuples from sorted edges expressed in seconds.

    When both endpoints share a unit (e.g. both in minutes) the lower
    endpoint's unit is elided for readability: `5 – 30 min` instead of
    `5 min – 30 min`.
    """
    out: list[tuple[float, float, str]] = []
    for i in range(len(edges_seconds) - 1):
        lo, hi = edges_seconds[i], edges_seconds[i + 1]
        if i == 0 and lo == 0 and not math.isinf(hi):
            label = f"< {_format_hold(hi)}"
        elif math.isinf(hi):
            label = f"> {_format_hold(lo)}"
        elif _hold_unit(lo) == _hold_unit(hi):
            label = f"{_hold_value_only(lo)} – {_format_hold(hi)}"
        else:
            label = f"{_format_hold(lo)} – {_format_hold(hi)}"
        out.append((lo, hi, label))
    return out


def _minutes_to_seconds(edges_minutes: list[float]) -> list[float]:
    return [
        float("inf") if math.isinf(m) else float(m) * 60.0
        for m in edges_minutes
    ]


# Backwards-compatible default bucket list (in seconds).
HOLD_BUCKETS: list[tuple[float, float, str]] = make_hold_buckets(
    _minutes_to_seconds(DEFAULT_HOLD_EDGES_MINUTES)
)


def by_hold_time(
    trades: list[dict],
    edges: Optional[list[float]] = None,
) -> Report:
    """Bucket closed trades by hold_duration_seconds.

    `edges` overrides the default bucket layout. **Edges are in seconds**;
    callers from the GUI must convert from minutes first. Last edge can
    be `float('inf')`.
    """
    buckets = make_hold_buckets(edges) if edges else HOLD_BUCKETS

    by: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        hd = t.get("hold_duration_seconds")
        if hd is None:
            continue
        secs = float(hd)
        for lo, hi, label in buckets:
            if lo <= secs < hi:
                by[label].append(t)
                break

    ordered = [label for _, _, label in buckets]
    rows = [
        _metrics_to_row(label, compute_metrics(by[label]))
        for label in ordered
        if label in by
    ]
    return Report(
        title="Performance by Hold Time",
        columns=_standard_columns("Hold"),
        rows=rows,
        chart_x_key="label",
        chart_y_key="net_pnl",
        chart_kind="bar",
        chart_title="Net P&L by Hold-Time Bucket",
        empty_message="No trades match the current filters.",
    )


# ---- by trade duration (intraday vs multiday) ----------------------------


def by_duration(trades: list[dict]) -> Report:
    """Bucket closed trades into Intraday vs Multiday.

    A trade is *intraday* when its entry and exit fall on the same
    calendar date; otherwise it's multiday. Open trades are skipped.
    """
    intraday: list[dict] = []
    multiday: list[dict] = []
    for t in trades:
        et = t.get("entry_time")
        xt = t.get("exit_time")
        if not isinstance(et, datetime) or not isinstance(xt, datetime):
            continue
        if et.date() == xt.date():
            intraday.append(t)
        else:
            multiday.append(t)

    rows = []
    for label, group in (("Intraday", intraday), ("Multiday", multiday)):
        if group:
            rows.append(_metrics_to_row(label, compute_metrics(group)))

    return Report(
        title="Performance by Duration",
        columns=_standard_columns("Duration"),
        rows=rows,
        chart_x_key="label",
        chart_y_key="net_pnl",
        chart_kind="bar",
        chart_title="Net P&L by Duration",
        empty_message="No closed trades match the current filters.",
    )
