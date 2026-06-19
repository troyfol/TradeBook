"""Registry of dashboard chart cards.

Each entry pairs a stable string key (used in QSettings persistence)
with a human label (shown in the configure dialog and as the card
title on the Dashboard).

Adding a new chart in the future:
    1. Build its widget in ``composed_charts`` or ``painter_charts``
    2. Add it to ``CHART_CARDS`` here with a fresh ``key``
    3. Wire its construction + refresh in ``DashboardTab``
       (``_build_chart_cards`` keys on the same string)
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChartCardDef:
    key: str
    label: str
    description: str = ""
    # Initial size class for new dashboards (or any time a key shows
    # up without saved state — e.g. just-added charts after an
    # update). Must be one of the ``SIZE_*`` strings from
    # ``composed_charts``.
    default_size: str = "small"


CHART_CARDS: list[ChartCardDef] = [
    ChartCardDef(
        "winloss", "Winning vs Losing Trades",
        "Donut showing the win-rate by count.",
    ),
    ChartCardDef(
        "hold_wl", "Hold Time — Winners vs Losers",
        "Bar comparison of average hold time, winners vs losers.",
    ),
    ChartCardDef(
        "avg_wl", "Average Winning Trade vs Losing Trade",
        "Bar comparison of avg P&L per winner vs avg per loser.",
    ),
    ChartCardDef(
        "largest", "Largest Gain vs Largest Loss",
        "Half-circle gauge — green for the largest single-trade gain, "
        "red for the largest loss.",
    ),
    ChartCardDef(
        "dow", "Performance By Day Of Week",
        "Net P&L bucketed by entry weekday (Mon–Fri).",
    ),
    ChartCardDef(
        "duration", "Performance By Duration",
        "Intraday vs multi-day trade outcomes.",
    ),
    ChartCardDef(
        "price", "Performance By Price",
        "Net P&L bucketed by entry price band.",
    ),
    ChartCardDef(
        "hour", "Performance By Hour Of Day",
        "Net P&L bucketed by entry hour.",
    ),
    ChartCardDef(
        "fees", "Total Fees",
        "Sum of commissions across the selected range.",
    ),
    ChartCardDef(
        "pf", "Profit Factor",
        "Half-circle gauge — gross profits / gross losses, capped at 3.0.",
    ),
    ChartCardDef(
        "tags", "Tag Breakdown",
        "Per-tag P&L with % shown against the win or loss pool.",
    ),
    ChartCardDef(
        "equity_curve", "Equity Curve",
        "Cumulative net P&L over the selected date range — green "
        "above zero, red below.",
        default_size="wide",
    ),
    ChartCardDef(
        "daily_pnl", "Daily P&L",
        "Per-day net P&L bars across the selected date range.",
        default_size="wide",
    ),
    ChartCardDef(
        "open_positions", "Open Positions",
        "Currently-open trades — count, total capital deployed, and a "
        "per-symbol breakdown. Independent of the date filter.",
        default_size="tall",
    ),
    ChartCardDef(
        "adjusted_sortino", "Adjusted Sortino Ratio",
        "Risk-adjusted return using downside deviation only (target / "
        "MAR = 0), scaled by 1/√2 for direct comparison to a Sharpe "
        "ratio. Higher is better.",
    ),
    ChartCardDef(
        "gain_to_pain", "Gain-to-Pain Ratio",
        "Sum of all net P&L divided by the total of losing P&L "
        "(Schwager). Higher is better.",
    ),
]

DEFAULT_CHART_ORDER: list[str] = [c.key for c in CHART_CARDS]

CHART_BY_KEY: dict[str, ChartCardDef] = {c.key: c for c in CHART_CARDS}
