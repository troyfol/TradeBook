"""Stack of P&L progress bars driven by the user's saved goals.

Pulls the daily / weekly / monthly / yearly targets from QSettings and
renders one bar per non-zero target. Negative progress (a losing
period) clamps the bar to 0 % and switches to a red label so the user
sees the gap rather than an empty bar with no context.
"""
from __future__ import annotations

import datetime as _dt
from typing import Iterable, Optional

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QLabel, QProgressBar, QWidget,
)

from gui.dialogs.goals import GOAL_KEYS, load_goals
from gui.widgets.chart_palette import get_palette, palette_hub


def _lerp_hex(a: str, b: str, t: float) -> str:
    """Linear blend between two hex colors. ``t`` clamped to [0, 1]."""
    t = max(0.0, min(1.0, t))
    ca, cb = QColor(a), QColor(b)
    r = ca.red() + (cb.red() - ca.red()) * t
    g = ca.green() + (cb.green() - ca.green()) * t
    b_ = ca.blue() + (cb.blue() - ca.blue()) * t
    return QColor(int(r), int(g), int(b_)).name()


def _today_period_bounds(period: str, today: _dt.date) -> tuple[_dt.date, _dt.date]:
    """Return (start, end) inclusive for the named period containing
    ``today``. Period is one of ``"daily" | "weekly" | "monthly" |
    "yearly"`` (matching the latter half of the GOAL_* settings keys).
    """
    if period == "daily":
        return today, today
    if period == "weekly":
        # ISO week: Monday → Sunday.
        start = today - _dt.timedelta(days=today.weekday())
        return start, start + _dt.timedelta(days=6)
    if period == "monthly":
        start = today.replace(day=1)
        if today.month == 12:
            end = today.replace(year=today.year + 1, month=1, day=1)
        else:
            end = today.replace(month=today.month + 1, day=1)
        return start, end - _dt.timedelta(days=1)
    if period == "yearly":
        return today.replace(month=1, day=1), today.replace(month=12, day=31)
    return today, today


def _period_pnl(trades: Iterable[dict], lo: _dt.date, hi: _dt.date) -> float:
    total = 0.0
    for t in trades:
        et = t.get("exit_time")
        if isinstance(et, _dt.datetime):
            d = et.date()
        elif isinstance(et, _dt.date):
            d = et
        else:
            continue
        if lo <= d <= hi:
            try:
                total += float(t.get("net_pnl") or 0.0)
            except (TypeError, ValueError):
                continue
    return total


class GoalBarsWidget(QFrame):
    """One row per active goal: label · progress bar · current/target."""

    def __init__(
        self,
        settings: Optional[QSettings] = None,
        parent=None,
    ):
        super().__init__(parent)
        self.setObjectName("GoalBars")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self._settings = settings
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(4, 4, 4, 4)
        self._grid.setHorizontalSpacing(10)
        self._grid.setVerticalSpacing(4)
        self._row_widgets: list[tuple[QLabel, QProgressBar, QLabel]] = []
        # Cache the trades passed to the last refresh so a palette
        # change can re-render bars/text in the new colors without
        # the dashboard having to re-feed data.
        self._last_trades: list[dict] = []
        palette_hub().palette_changed.connect(
            lambda: self.refresh(self._last_trades)
        )
        self.refresh([])

    def refresh(self, closed_trades: list[dict]) -> None:
        """Recompute progress against today's date for each active goal."""
        self._last_trades = list(closed_trades)
        # Tear down previous rows.
        for label, bar, status in self._row_widgets:
            for w in (label, bar, status):
                self._grid.removeWidget(w)
                w.deleteLater()
        self._row_widgets = []

        goals = load_goals(self._settings)
        today = _dt.date.today()
        pal = get_palette()

        any_active = False
        row = 0
        for label_text, key in GOAL_KEYS:
            target = goals.get(key, 0.0)
            if target <= 0:
                continue
            any_active = True
            period_name = key.split("/")[-1].replace("_pnl", "")
            lo, hi = _today_period_bounds(period_name, today)
            actual = _period_pnl(closed_trades, lo, hi)
            pct = max(0.0, min(actual / target, 1.0)) * 100.0 if target else 0.0

            # Bar + text fade from ``negative`` (empty) → ``positive``
            # (full) in lockstep so the row reads as a single signal.
            t = pct / 100.0
            mix = _lerp_hex(pal.negative, pal.positive, t)

            name_lbl = QLabel(f"{label_text} goal", self)
            name_lbl.setMinimumWidth(110)
            name_lbl.setStyleSheet(f"color: {mix};")

            bar = QProgressBar(self)
            bar.setRange(0, 100)
            bar.setValue(int(pct))
            bar.setTextVisible(False)
            bar.setFixedHeight(14)
            bar.setStyleSheet(
                "QProgressBar { border: none; background: #2d2d2d; "
                "border-radius: 2px; } "
                f"QProgressBar::chunk {{ background: {mix}; "
                "border-radius: 2px; }}"
            )

            status_lbl = QLabel(
                f"${actual:,.0f} / ${target:,.0f}  ({pct:.0f}%)",
                self,
            )
            status_lbl.setAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
            status_lbl.setMinimumWidth(180)
            # Negative actual stays loud-red so a losing period is
            # visible at a glance regardless of the gradient.
            if actual < 0:
                status_lbl.setStyleSheet(f"color: {pal.negative}; font-weight: bold;")
            else:
                status_lbl.setStyleSheet(f"color: {mix};")

            self._grid.addWidget(name_lbl, row, 0)
            self._grid.addWidget(bar, row, 1)
            self._grid.addWidget(status_lbl, row, 2)
            self._row_widgets.append((name_lbl, bar, status_lbl))
            row += 1

        self._grid.setColumnStretch(1, 1)
        # Hide the entire widget when no goals are configured — keeps
        # the Dashboard quiet for users who don't care about goals.
        self.setVisible(any_active)
