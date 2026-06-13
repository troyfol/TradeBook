"""R-multiple analytics — measure each trade's outcome in units of its
planned risk.

A trade's R is its realized P&L divided by the dollars it had at risk
when entered. Risk per share is ``|entry − stop|``, multiplied by the
share count. So R = net_pnl / risk_dollars. A losing trade that hit
its stop exactly is -1R; a winner that achieved 3× the planned risk
is +3R.

Trades without a recorded ``stop_loss_price`` are excluded — there's
no risk denominator to divide by.

Per-trade helpers and aggregate metrics live here; the report layer
(``analytics.reports``) calls into ``by_r_bucket`` to render the "By
R-Multiple" report.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Optional

from analytics.reports import Report, ReportColumn


# Default R bucket edges. ``-inf`` and ``+inf`` cap the tails so any
# extreme outlier still lands somewhere meaningful.
DEFAULT_R_EDGES: list[float] = [
    -math.inf, -3.0, -2.0, -1.0, -0.5, 0.0,
    0.5, 1.0, 2.0, 3.0, math.inf,
]


@dataclass
class RStats:
    """Aggregate R-multiple statistics across a set of trades."""
    trades_with_stop: int = 0
    total_r: float = 0.0
    avg_r: float = 0.0
    expectancy_r: float = 0.0   # avg R per trade — same as avg_r here
    best_r: Optional[float] = None
    worst_r: Optional[float] = None


def trade_risk_dollars(
    trade: dict, *, default_risk: Optional[float] = None,
) -> Optional[float]:
    """Dollars at risk when the trade was entered.

    Preference order:
        1. ``stop_loss_price`` on the trade → ``|entry − stop| × shares``
        2. ``default_risk`` fallback (typically the user's account-risk
           setting — e.g. "I always risk $100 per trade")
        3. None, when neither is available

    A stop equal to entry collapses to zero risk and falls through to
    the default as well.
    """
    stop = trade.get("stop_loss_price")
    entry = trade.get("avg_entry_price")
    shares = trade.get("total_shares")
    if stop is not None and entry is not None and shares is not None:
        try:
            risk_per_share = abs(float(entry) - float(stop))
            risk = risk_per_share * float(shares)
            if math.isfinite(risk) and risk > 0:
                return risk
        except (TypeError, ValueError):
            pass
    if default_risk is not None and default_risk > 0:
        return float(default_risk)
    return None


def trade_r(
    trade: dict, *, default_risk: Optional[float] = None,
) -> Optional[float]:
    """R-multiple for a single trade, or None when not computable.

    Open trades (``net_pnl is None``) always return None. A missing
    stop price falls back to ``default_risk`` when supplied — that's
    how the "default $R per trade" setting turns unstopped trades into
    reportable R values.
    """
    risk = trade_risk_dollars(trade, default_risk=default_risk)
    if risk is None:
        return None
    net = trade.get("net_pnl")
    if net is None:
        return None
    try:
        return float(net) / risk
    except (TypeError, ValueError):
        return None


def compute_r_stats(
    trades: Iterable[dict], *, default_risk: Optional[float] = None,
) -> RStats:
    """Roll up R across the given trades, using ``default_risk`` as the
    fallback denominator for trades without an explicit stop."""
    rs = [trade_r(t, default_risk=default_risk) for t in trades]
    rs = [r for r in rs if r is not None]
    if not rs:
        return RStats()
    return RStats(
        trades_with_stop=len(rs),
        total_r=sum(rs),
        avg_r=sum(rs) / len(rs),
        expectancy_r=sum(rs) / len(rs),
        best_r=max(rs),
        worst_r=min(rs),
    )


# ---- bucketed report ------------------------------------------------------


def _format_edge(v: float) -> str:
    if math.isinf(v):
        return "∞" if v > 0 else "-∞"
    return f"{v:g}R"


def _bucket_label(lo: float, hi: float) -> str:
    if math.isinf(lo):
        return f"≤ {_format_edge(hi)}"
    if math.isinf(hi):
        return f"> {_format_edge(lo)}"
    return f"{_format_edge(lo)} → {_format_edge(hi)}"


def by_r_bucket(
    trades: list[dict],
    edges: Optional[list[float]] = None,
    *,
    default_risk: Optional[float] = None,
) -> Report:
    """Group trades by R-bucket and tally count + win-rate + total R."""
    edge_list = edges if edges else DEFAULT_R_EDGES
    buckets: list[tuple[float, float, str]] = [
        (edge_list[i], edge_list[i + 1],
         _bucket_label(edge_list[i], edge_list[i + 1]))
        for i in range(len(edge_list) - 1)
    ]

    by: dict[str, list[float]] = defaultdict(list)
    by_pnl: dict[str, float] = defaultdict(float)
    by_count: dict[str, int] = defaultdict(int)
    by_wins: dict[str, int] = defaultdict(int)

    for t in trades:
        r = trade_r(t, default_risk=default_risk)
        if r is None:
            continue
        for lo, hi, label in buckets:
            in_range = lo <= r < hi if not math.isinf(hi) else lo <= r
            if in_range:
                by[label].append(r)
                pnl = t.get("net_pnl") or 0.0
                by_pnl[label] += float(pnl)
                by_count[label] += 1
                if float(pnl) >= 0:
                    by_wins[label] += 1
                break

    rows: list[dict] = []
    for _lo, _hi, label in buckets:
        n = by_count[label]
        if n == 0:
            continue
        total_r = sum(by[label])
        rows.append({
            "label": label,
            "trades": n,
            "win_rate": (by_wins[label] / n) if n else 0.0,
            "total_r": total_r,
            "avg_r": total_r / n,
            "net_pnl": by_pnl[label],
        })

    return Report(
        title="Performance by R-Multiple",
        columns=[
            ReportColumn("label", "R bucket", "str", align="left"),
            ReportColumn("trades", "Trades", "int", align="right"),
            ReportColumn("win_rate", "Win Rate", "pct", align="right"),
            ReportColumn(
                "avg_r", "Avg R", "float",
                color_by_sign=True, align="right",
            ),
            ReportColumn(
                "total_r", "Total R", "float",
                color_by_sign=True, align="right",
            ),
            ReportColumn(
                "net_pnl", "Net P&L", "currency",
                color_by_sign=True, align="right",
            ),
        ],
        rows=rows,
        chart_x_key="label",
        chart_y_key="total_r",
        chart_kind="bar",
        chart_title="Total R by Bucket",
        empty_message=(
            "No trades with a stop loss (or default R) match the "
            "current filters. Set a stop via right-click → Set stop "
            "loss…, or a default $R under File → R-multiple settings…"
        ),
    )
