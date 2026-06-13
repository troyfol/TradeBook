"""Core trade metrics and chart data helpers.

All metrics operate on net P&L (after commissions). Per user decision,
breakeven trades (net_pnl == 0) count as wins, not losses.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable, Iterable, Optional


@dataclass
class TradeMetrics:
    total_trades: int = 0
    total_pnl: float = 0.0
    win_count: int = 0
    loss_count: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0   # math.inf if losses == 0 but winnings > 0
    expectancy: float = 0.0      # avg net P&L per trade
    avg_winner: float = 0.0
    avg_loser: float = 0.0       # negative (or zero if no losers)
    largest_winner: float = 0.0
    largest_loser: float = 0.0   # negative (or zero if no losers)
    total_commission: float = 0.0
    # Hold-time stats (in seconds; None when no closed trades have a hold).
    avg_hold_seconds: Optional[float] = None
    max_hold_seconds: Optional[int] = None
    most_profitable_hold_seconds: Optional[int] = None
    # Hold-time split by outcome (Phase 13 — feeds the dashboard's
    # "Hold time: winners vs losers" comparison bar). None when the
    # corresponding bucket has no trades.
    avg_hold_seconds_winners: Optional[float] = None
    avg_hold_seconds_losers: Optional[float] = None


def compute_metrics(trades: Iterable[dict]) -> TradeMetrics:
    """Summarize closed trades into a TradeMetrics object.

    Open trades (net_pnl is None) are silently skipped.
    Breakeven trades (net_pnl == 0) count as winners.
    """
    closed = [t for t in trades if t.get("net_pnl") is not None]
    n = len(closed)
    if n == 0:
        return TradeMetrics()

    pnls = [float(t["net_pnl"]) for t in closed]
    total_pnl = sum(pnls)
    total_commission = sum(float(t.get("total_commission") or 0.0) for t in closed)

    winners = [p for p in pnls if p >= 0.0]
    losers = [p for p in pnls if p < 0.0]

    win_count = len(winners)
    loss_count = len(losers)
    win_rate = win_count / n

    total_winnings = sum(winners)
    total_losses = -sum(losers)  # positive magnitude

    if total_losses > 0:
        profit_factor = total_winnings / total_losses
    elif total_winnings > 0:
        profit_factor = math.inf
    else:
        profit_factor = 0.0

    avg_winner = total_winnings / win_count if win_count else 0.0
    avg_loser = sum(losers) / loss_count if loss_count else 0.0
    expectancy = total_pnl / n  # equivalent to WR*avg_win + LR*avg_loss

    largest_winner = max(winners) if winners else 0.0
    largest_loser = min(losers) if losers else 0.0

    # Hold-time metrics (skip trades without a hold_duration_seconds value).
    holds = [
        (int(t["hold_duration_seconds"]), float(t["net_pnl"]))
        for t in closed
        if t.get("hold_duration_seconds") is not None
    ]
    if holds:
        hold_secs = [h for h, _ in holds]
        avg_hold_seconds = sum(hold_secs) / len(hold_secs)
        max_hold_seconds = max(hold_secs)
        # Hold of the single most-profitable trade. Tie-breaker: longest hold,
        # so the choice is deterministic for synthetic / zero-pnl test data.
        best_hold, _ = max(holds, key=lambda hp: (hp[1], hp[0]))
        most_profitable_hold_seconds = best_hold
        # Split-by-outcome averages.
        win_holds = [h for h, p in holds if p >= 0]
        loss_holds = [h for h, p in holds if p < 0]
        avg_hold_seconds_winners = (
            sum(win_holds) / len(win_holds) if win_holds else None
        )
        avg_hold_seconds_losers = (
            sum(loss_holds) / len(loss_holds) if loss_holds else None
        )
    else:
        avg_hold_seconds = None
        max_hold_seconds = None
        most_profitable_hold_seconds = None
        avg_hold_seconds_winners = None
        avg_hold_seconds_losers = None

    return TradeMetrics(
        total_trades=n,
        total_pnl=total_pnl,
        win_count=win_count,
        loss_count=loss_count,
        win_rate=win_rate,
        profit_factor=profit_factor,
        expectancy=expectancy,
        avg_winner=avg_winner,
        avg_loser=avg_loser,
        largest_winner=largest_winner,
        largest_loser=largest_loser,
        total_commission=total_commission,
        avg_hold_seconds=avg_hold_seconds,
        max_hold_seconds=max_hold_seconds,
        most_profitable_hold_seconds=most_profitable_hold_seconds,
        avg_hold_seconds_winners=avg_hold_seconds_winners,
        avg_hold_seconds_losers=avg_hold_seconds_losers,
    )


def filter_by_exit_date(
    trades: Iterable[dict],
    start: date,
    end: date,
) -> list[dict]:
    """Return trades whose exit_time date falls in [start, end] inclusive.

    Open trades (no exit_time) are excluded.
    """
    result: list[dict] = []
    for t in trades:
        et = t.get("exit_time")
        if et is None:
            continue
        d = et.date() if isinstance(et, datetime) else et
        if start <= d <= end:
            result.append(t)
    return result


def equity_curve_points(trades: Iterable[dict]) -> tuple[list[datetime], list[float]]:
    """Return (timestamps, cumulative_net_pnl) for plotting.

    Sorted chronologically by exit_time. Open trades are excluded.
    """
    closed = [
        t for t in trades
        if t.get("net_pnl") is not None and t.get("exit_time") is not None
    ]
    closed.sort(key=lambda t: t["exit_time"])
    times: list[datetime] = []
    cum: list[float] = []
    running = 0.0
    for t in closed:
        running += float(t["net_pnl"])
        times.append(t["exit_time"])
        cum.append(running)
    return times, cum


def daily_pnl_bars(trades: Iterable[dict]) -> list[tuple[date, float]]:
    """Return (date, net_pnl) pairs, one per trading day, sorted ascending.

    Only days with at least one closed trade are returned (no weekend gaps).
    """
    by_day: dict[date, float] = defaultdict(float)
    for t in trades:
        et = t.get("exit_time")
        pnl = t.get("net_pnl")
        if et is None or pnl is None:
            continue
        d = et.date() if isinstance(et, datetime) else et
        by_day[d] += float(pnl)
    return sorted(by_day.items(), key=lambda p: p[0])


# --- metric registry --------------------------------------------------------

@dataclass(frozen=True)
class MetricDef:
    key: str
    label: str
    fmt: Callable[[TradeMetrics], str]
    color: Optional[Callable[[TradeMetrics], Optional[str]]] = None


def _fmt_currency(v: float) -> str:
    if v == 0:
        return "$0.00"
    sign = "-" if v < 0 else ""
    return f"{sign}${abs(v):,.2f}"


def _fmt_pct(v: float) -> str:
    return f"{v * 100:.1f}%"


def _fmt_profit_factor(v: float) -> str:
    if math.isinf(v):
        return "∞"
    return f"{v:.2f}x"


def _color_pnl(v: float) -> Optional[str]:
    if v > 0:
        return "#00C853"
    if v < 0:
        return "#FF1744"
    return None


def _fmt_hold(seconds: Optional[float]) -> str:
    """Render a hold duration as `47s` / `12m 30s` / `2h 14m`."""
    if seconds is None:
        return "—"
    s = int(round(float(seconds)))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        m, sec = divmod(s, 60)
        return f"{m}m {sec}s"
    if s < 86400:
        h, rem = divmod(s, 3600)
        m, _ = divmod(rem, 60)
        return f"{h}h {m}m"
    d, rem = divmod(s, 86400)
    h, _ = divmod(rem, 3600)
    return f"{d}d {h}h"


METRIC_REGISTRY: list[MetricDef] = [
    MetricDef(
        "total_pnl", "Total P&L",
        lambda m: _fmt_currency(m.total_pnl),
        lambda m: _color_pnl(m.total_pnl),
    ),
    MetricDef(
        "win_rate", "Win Rate",
        lambda m: _fmt_pct(m.win_rate),
    ),
    MetricDef(
        "profit_factor", "Profit Factor",
        lambda m: _fmt_profit_factor(m.profit_factor),
    ),
    MetricDef(
        "expectancy", "Expectancy",
        lambda m: _fmt_currency(m.expectancy),
        lambda m: _color_pnl(m.expectancy),
    ),
    MetricDef(
        "avg_winner", "Avg Winner",
        lambda m: _fmt_currency(m.avg_winner),
        lambda m: _color_pnl(m.avg_winner),
    ),
    MetricDef(
        "avg_loser", "Avg Loser",
        lambda m: _fmt_currency(m.avg_loser),
        lambda m: _color_pnl(m.avg_loser),
    ),
    MetricDef(
        "largest_winner", "Largest Winner",
        lambda m: _fmt_currency(m.largest_winner),
        lambda m: _color_pnl(m.largest_winner),
    ),
    MetricDef(
        "largest_loser", "Largest Loser",
        lambda m: _fmt_currency(m.largest_loser),
        lambda m: _color_pnl(m.largest_loser),
    ),
    MetricDef(
        "total_trades", "Total Trades",
        lambda m: f"{m.total_trades:,}",
    ),
    MetricDef(
        "avg_hold", "Average Hold",
        lambda m: _fmt_hold(m.avg_hold_seconds),
    ),
    MetricDef(
        "max_hold", "Max Hold",
        lambda m: _fmt_hold(m.max_hold_seconds),
    ),
    MetricDef(
        "most_profitable_hold", "Most Profitable Hold",
        lambda m: _fmt_hold(m.most_profitable_hold_seconds),
    ),
]

# Quick lookup by key.
METRIC_BY_KEY: dict[str, MetricDef] = {m.key: m for m in METRIC_REGISTRY}

# Default 4×3 dashboard layout (row-major). The hold-time column lives on
# the right (positions 3, 7, 11). Used when no per-user order is saved.
DEFAULT_METRIC_ORDER: list[str] = [
    "total_pnl",      "win_rate",       "profit_factor", "avg_hold",
    "expectancy",     "avg_winner",     "avg_loser",     "max_hold",
    "largest_winner", "largest_loser",  "total_trades",  "most_profitable_hold",
]
