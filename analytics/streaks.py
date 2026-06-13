"""Streak analysis — longest/current win and loss runs.

Walks closed trades in chronological order (by exit_time) and reports:
    - longest winning streak (count + total P&L)
    - longest losing streak (count + total P&L)
    - current streak (count + sign + total P&L)
    - average winning / losing streak length
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
class StreakRun:
    kind: str          # "win" | "loss"
    count: int
    total_pnl: float
    start: Optional[datetime] = None
    end: Optional[datetime] = None


def compute_streaks(trades: list[dict]) -> dict:
    """Walk trades chronologically; return aggregate streak stats.

    Breakeven trades (net_pnl == 0) count as wins to match the metrics layer.
    Open trades are skipped.
    """
    closed = [
        t for t in trades
        if t.get("net_pnl") is not None and _exit_dt(t) is not None
    ]
    closed.sort(key=lambda t: _exit_dt(t))

    runs: list[StreakRun] = []
    cur: Optional[StreakRun] = None
    for t in closed:
        pnl = float(t["net_pnl"])
        kind = "win" if pnl >= 0.0 else "loss"
        et = _exit_dt(t)
        if cur is None or cur.kind != kind:
            if cur is not None:
                runs.append(cur)
            cur = StreakRun(kind=kind, count=1, total_pnl=pnl, start=et, end=et)
        else:
            cur.count += 1
            cur.total_pnl += pnl
            cur.end = et
    if cur is not None:
        runs.append(cur)

    win_runs = [r for r in runs if r.kind == "win"]
    loss_runs = [r for r in runs if r.kind == "loss"]

    longest_win = max(win_runs, key=lambda r: r.count, default=None)
    longest_loss = max(loss_runs, key=lambda r: r.count, default=None)
    current = runs[-1] if runs else None

    avg_win_len = (
        sum(r.count for r in win_runs) / len(win_runs) if win_runs else 0.0
    )
    avg_loss_len = (
        sum(r.count for r in loss_runs) / len(loss_runs) if loss_runs else 0.0
    )

    return {
        "total_runs": len(runs),
        "win_runs": len(win_runs),
        "loss_runs": len(loss_runs),
        "longest_win": longest_win,
        "longest_loss": longest_loss,
        "current": current,
        "avg_win_len": avg_win_len,
        "avg_loss_len": avg_loss_len,
    }


def _fmt_run(run: Optional[StreakRun]) -> tuple[str, float]:
    if run is None or run.count == 0:
        return "—", 0.0
    label = f"{run.count} trade" + ("" if run.count == 1 else "s")
    return label, run.total_pnl


def by_streaks(trades: list[dict]) -> Report:
    """Stat-list report. Custom 3-column layout: stat label / count / P&L."""
    s = compute_streaks(trades)

    rows: list[dict] = []

    def _row(label: str, count_text: str, pnl: float) -> dict:
        return {"label": label, "count_text": count_text, "net_pnl": pnl}

    lw = s["longest_win"]
    ll = s["longest_loss"]
    cur = s["current"]

    lw_text, lw_pnl = _fmt_run(lw)
    ll_text, ll_pnl = _fmt_run(ll)

    rows.append(_row("Longest winning streak", lw_text, lw_pnl))
    rows.append(_row("Longest losing streak", ll_text, ll_pnl))

    if cur is not None and cur.count > 0:
        sign = "winning" if cur.kind == "win" else "losing"
        cur_label = f"Current streak ({sign})"
        cur_text, cur_pnl = _fmt_run(cur)
    else:
        cur_label = "Current streak"
        cur_text, cur_pnl = "—", 0.0
    rows.append(_row(cur_label, cur_text, cur_pnl))

    rows.append(_row(
        "Avg winning streak length",
        f"{s['avg_win_len']:.2f} trades" if s["win_runs"] else "—",
        0.0,
    ))
    rows.append(_row(
        "Avg losing streak length",
        f"{s['avg_loss_len']:.2f} trades" if s["loss_runs"] else "—",
        0.0,
    ))
    rows.append(_row(
        "Total winning runs", f"{s['win_runs']}", 0.0,
    ))
    rows.append(_row(
        "Total losing runs", f"{s['loss_runs']}", 0.0,
    ))

    columns = [
        ReportColumn("label", "Stat", "str", align="left"),
        ReportColumn("count_text", "Length", "str", align="right"),
        ReportColumn("net_pnl", "Net P&L", "currency",
                     color_by_sign=True, align="right"),
    ]

    return Report(
        title="Streaks",
        columns=columns,
        rows=rows,
        chart_kind="none",
        empty_message="No trades match the current filters.",
    )
