"""Month-grid calendar data layer.

Pure data — no Qt, no painting. Builds a Mon–Fri-only grid of `DayCell`
objects from a list of trade dicts, organized into weeks.

Phase 5 user decisions baked in:
    - Mon-start week
    - Weekends skipped entirely (5 weekday columns)
    - Weekly P&L total exposed alongside the row of cells
    - Empty days carry trade_count == 0 (renderer shows an em dash)
    - Color intensity normalized per visible month (max abs net P&L)
"""
from __future__ import annotations

import calendar
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Iterable, Optional


@dataclass(frozen=True)
class DayCell:
    """Aggregated stats for a single weekday cell on the calendar."""
    day: date
    net_pnl: float = 0.0
    trade_count: int = 0
    win_count: int = 0
    loss_count: int = 0
    in_month: bool = True   # False for filler weekdays in adjacent months

    @property
    def is_empty(self) -> bool:
        return self.trade_count == 0


@dataclass
class WeekRow:
    """One Mon–Fri row of the calendar grid plus its weekly net total."""
    cells: list[DayCell] = field(default_factory=list)  # always length 5
    weekly_net: float = 0.0
    weekly_trade_count: int = 0


@dataclass
class CalendarMonth:
    year: int
    month: int
    rows: list[WeekRow] = field(default_factory=list)
    month_net: float = 0.0
    month_trade_count: int = 0
    max_abs_pnl: float = 0.0   # max(|cell.net_pnl|) across in-month cells; 0 if none


def _aggregate_by_day(trades: Iterable[dict]) -> dict[date, dict]:
    """Group closed trades by exit date → totals."""
    by_day: dict[date, dict] = defaultdict(
        lambda: {"net": 0.0, "n": 0, "wins": 0, "losses": 0}
    )
    for t in trades:
        et = t.get("exit_time")
        net = t.get("net_pnl")
        if et is None or net is None:
            continue
        d = et.date() if isinstance(et, datetime) else et
        bucket = by_day[d]
        net_f = float(net)
        bucket["net"] += net_f
        bucket["n"] += 1
        # Breakeven counts as a win, matching analytics.metrics.compute_metrics.
        if net_f >= 0.0:
            bucket["wins"] += 1
        else:
            bucket["losses"] += 1
    return by_day


def _weekday_iter(start: date, end: date):
    """Yield each Mon–Fri date in [start, end] inclusive."""
    cur = start
    one_day = timedelta(days=1)
    while cur <= end:
        if cur.weekday() < 5:  # 0=Mon..4=Fri
            yield cur
        cur += one_day


def build_month(
    trades: Iterable[dict],
    year: int,
    month: int,
) -> CalendarMonth:
    """Construct a `CalendarMonth` for the given (year, month).

    The grid spans from the Monday of the week containing the 1st through
    the Friday of the week containing the last day of the month, so weeks
    that straddle into adjacent months are still complete rows.
    """
    if not (1 <= month <= 12):
        raise ValueError(f"month out of range: {month}")

    first = date(year, month, 1)
    last = date(year, month, calendar.monthrange(year, month)[1])

    # Walk back to Monday of week 1, forward to Friday of last week.
    grid_start = first - timedelta(days=first.weekday())
    grid_end = last + timedelta(days=(4 - last.weekday()) % 7)
    if grid_end < last:
        grid_end = last + timedelta(days=4 - last.weekday())

    by_day = _aggregate_by_day(trades)

    cm = CalendarMonth(year=year, month=month)
    week: WeekRow = WeekRow()
    week_count = 0
    max_abs = 0.0

    for d in _weekday_iter(grid_start, grid_end):
        bucket = by_day.get(d)
        in_month = (d.month == month and d.year == year)
        if bucket is None:
            cell = DayCell(day=d, in_month=in_month)
        else:
            cell = DayCell(
                day=d,
                net_pnl=bucket["net"],
                trade_count=bucket["n"],
                win_count=bucket["wins"],
                loss_count=bucket["losses"],
                in_month=in_month,
            )
        week.cells.append(cell)
        week.weekly_net += cell.net_pnl if in_month else 0.0
        week.weekly_trade_count += cell.trade_count if in_month else 0

        if in_month and cell.trade_count > 0:
            cm.month_net += cell.net_pnl
            cm.month_trade_count += cell.trade_count
            if abs(cell.net_pnl) > max_abs:
                max_abs = abs(cell.net_pnl)

        week_count += 1
        if week_count == 5:
            cm.rows.append(week)
            week = WeekRow()
            week_count = 0

    if week.cells:
        # Should not happen with proper grid bounds, but be defensive.
        cm.rows.append(week)

    cm.max_abs_pnl = max_abs
    return cm


def shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    """Return (year, month) shifted by `delta` months (positive or negative)."""
    idx = (year * 12 + (month - 1)) + delta
    return idx // 12, (idx % 12) + 1


def trades_on_day(trades: Iterable[dict], d: date) -> list[dict]:
    """Return all closed trades whose exit_time falls on the given date."""
    out: list[dict] = []
    for t in trades:
        et = t.get("exit_time")
        if et is None:
            continue
        ed = et.date() if isinstance(et, datetime) else et
        if ed == d:
            out.append(t)
    return out


def month_extents(trades: Iterable[dict]) -> Optional[tuple[date, date]]:
    """Earliest / latest exit date among closed trades, or None if none."""
    earliest: Optional[date] = None
    latest: Optional[date] = None
    for t in trades:
        et = t.get("exit_time")
        if et is None:
            continue
        d = et.date() if isinstance(et, datetime) else et
        if earliest is None or d < earliest:
            earliest = d
        if latest is None or d > latest:
            latest = d
    if earliest is None:
        return None
    return earliest, latest
