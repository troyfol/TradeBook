"""Drawdown analysis on the equity curve.

Walks closed trades chronologically (by exit_time), reconstructs the
cumulative net-P&L curve, then reports:
    - max drawdown ($ + % from peak)
    - longest drawdown duration (in days, peak → trough)
    - peak / trough timestamps
    - current drawdown vs all-time peak
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from analytics.reports import Report, ReportColumn


def _exit_dt(t: dict) -> Optional[datetime]:
    et = t.get("exit_time")
    return et if isinstance(et, datetime) else None


@dataclass
class DrawdownStats:
    max_dd_dollars: float = 0.0          # most negative excursion ($, ≤ 0)
    max_dd_pct: float = 0.0              # most negative excursion (% of peak, ≤ 0)
    max_dd_peak: Optional[datetime] = None
    max_dd_trough: Optional[datetime] = None
    max_dd_duration_days: float = 0.0
    longest_dd_days: float = 0.0         # longest unrecovered run, even if shallow
    current_dd_dollars: float = 0.0      # ≤ 0
    current_dd_pct: float = 0.0          # ≤ 0
    peak_pnl: float = 0.0
    final_pnl: float = 0.0


def compute_drawdown(trades: list[dict]) -> DrawdownStats:
    """Reconstruct equity curve and find drawdown extrema.

    Drawdowns are measured against the running peak of cumulative net P&L.
    Percentage drawdown uses the peak value as the denominator and is
    only meaningful when the peak is positive; otherwise reported as 0.
    """
    closed = [
        t for t in trades
        if t.get("net_pnl") is not None and _exit_dt(t) is not None
    ]
    closed.sort(key=lambda t: _exit_dt(t))
    if not closed:
        return DrawdownStats()

    running = 0.0
    peak = 0.0
    peak_time: Optional[datetime] = _exit_dt(closed[0])
    max_dd = 0.0           # ≤ 0 (dollar)
    max_dd_pct = 0.0
    max_dd_peak: Optional[datetime] = None
    max_dd_trough: Optional[datetime] = None

    longest_run_days = 0.0
    cur_run_start: Optional[datetime] = None

    for t in closed:
        running += float(t["net_pnl"])
        et = _exit_dt(t)
        if running >= peak:
            peak = running
            peak_time = et
            cur_run_start = None
        else:
            if cur_run_start is None:
                cur_run_start = peak_time
            dd = running - peak
            if dd < max_dd:
                max_dd = dd
                max_dd_peak = peak_time
                max_dd_trough = et
                if peak > 0:
                    max_dd_pct = dd / peak
                else:
                    max_dd_pct = 0.0
            if cur_run_start is not None and et is not None:
                run_days = (et - cur_run_start).total_seconds() / 86400.0
                if run_days > longest_run_days:
                    longest_run_days = run_days

    # Duration of the worst-drawdown excursion (peak → trough).
    if max_dd_peak is not None and max_dd_trough is not None:
        max_dd_duration_days = (
            (max_dd_trough - max_dd_peak).total_seconds() / 86400.0
        )
    else:
        max_dd_duration_days = 0.0

    cur_dd = running - peak
    cur_dd_pct = (cur_dd / peak) if peak > 0 else 0.0

    return DrawdownStats(
        max_dd_dollars=max_dd,
        max_dd_pct=max_dd_pct,
        max_dd_peak=max_dd_peak,
        max_dd_trough=max_dd_trough,
        max_dd_duration_days=max_dd_duration_days,
        longest_dd_days=longest_run_days,
        current_dd_dollars=cur_dd,
        current_dd_pct=cur_dd_pct,
        peak_pnl=peak,
        final_pnl=running,
    )


def _fmt_dt(dt: Optional[datetime]) -> str:
    return dt.strftime("%Y-%m-%d") if dt else "—"


def _fmt_days(d: float) -> str:
    if d <= 0:
        return "—"
    if d < 1:
        return f"{d * 24:.1f} h"
    return f"{d:.1f} days"


def by_drawdown(trades: list[dict]) -> Report:
    """Stat-list report. Custom 2-column layout: stat label / value."""
    s = compute_drawdown(trades)

    rows: list[dict] = []

    def _row(label: str, value: str, pnl: Optional[float] = None) -> dict:
        return {"label": label, "value": value, "net_pnl": pnl or 0.0}

    rows.append(_row(
        "Max drawdown ($)",
        _fmt_currency(s.max_dd_dollars) if s.max_dd_dollars < 0 else "—",
        s.max_dd_dollars,
    ))
    rows.append(_row(
        "Max drawdown (%)",
        _fmt_pct(s.max_dd_pct) if s.max_dd_pct < 0 else "—",
    ))
    rows.append(_row("Peak before drawdown", _fmt_dt(s.max_dd_peak)))
    rows.append(_row("Trough", _fmt_dt(s.max_dd_trough)))
    rows.append(_row(
        "Max drawdown duration", _fmt_days(s.max_dd_duration_days),
    ))
    rows.append(_row(
        "Longest underwater run", _fmt_days(s.longest_dd_days),
    ))
    rows.append(_row(
        "Current drawdown ($)",
        _fmt_currency(s.current_dd_dollars) if s.current_dd_dollars < 0 else "$0.00",
        s.current_dd_dollars,
    ))
    rows.append(_row(
        "Current drawdown (%)",
        _fmt_pct(s.current_dd_pct) if s.current_dd_pct < 0 else "0.0%",
    ))
    rows.append(_row("All-time peak P&L", _fmt_currency(s.peak_pnl), s.peak_pnl))

    columns = [
        ReportColumn("label", "Stat", "str", align="left"),
        ReportColumn("value", "Value", "str", align="right"),
    ]

    return Report(
        title="Drawdown",
        columns=columns,
        rows=rows,
        chart_kind="none",
        empty_message="No trades match the current filters.",
    )


def _fmt_currency(v: float) -> str:
    if v == 0:
        return "$0.00"
    sign = "-" if v < 0 else ""
    return f"{sign}${abs(v):,.2f}"


def _fmt_pct(v: float) -> str:
    return f"{v * 100:.1f}%"
