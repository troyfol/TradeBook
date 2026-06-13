"""Compact horizontal calendar strip — last N weekdays at a glance.

Each cell shows ``DD WeekdayAbbrev / $P&L / N trades`` for one
trading day. Weekends (Sat / Sun) are skipped entirely so the strip
always shows N consecutive *trading* days, not calendar days.

Designed to sit at the very top of the Dashboard as a "right now"
glance — you can see the last week+ of trading without scrolling.
"""
from __future__ import annotations

import datetime as _dt
from typing import Iterable

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget,
)


_DEFAULT_LOOKBACK_DAYS = 7  # number of weekdays to show

_WEEKDAY_ABBREV = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri"}

_COLOR_WIN = "#00C853"
_COLOR_LOSS = "#FF1744"
_COLOR_NONE = "#606060"


def _is_weekday(d: _dt.date) -> bool:
    return d.weekday() < 5


def _last_n_weekdays(n: int, *, today: _dt.date) -> list[_dt.date]:
    """Return the last ``n`` weekdays ending at (and including) ``today``
    if today is a weekday; otherwise the most recent weekday before
    today is the latest cell."""
    out: list[_dt.date] = []
    cursor = today
    while len(out) < n:
        if _is_weekday(cursor):
            out.append(cursor)
        cursor -= _dt.timedelta(days=1)
    return list(reversed(out))


def _aggregate_pnl_by_date(
    trades: Iterable[dict],
) -> dict[_dt.date, tuple[float, int]]:
    """Sum P&L and trade count per exit date."""
    out: dict[_dt.date, list[float]] = {}
    for t in trades:
        et = t.get("exit_time")
        if isinstance(et, _dt.datetime):
            d = et.date()
        elif isinstance(et, _dt.date):
            d = et
        else:
            continue
        try:
            pnl = float(t.get("net_pnl") or 0.0)
        except (TypeError, ValueError):
            continue
        out.setdefault(d, []).append(pnl)
    return {d: (sum(vals), len(vals)) for d, vals in out.items()}


class _DayCell(QFrame):
    """One day cell in the strip."""

    def __init__(
        self,
        day: _dt.date,
        pnl: float,
        n_trades: int,
        parent=None,
    ):
        super().__init__(parent)
        self.setObjectName("WeekStripCell")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed,
        )
        self.setMinimumHeight(80)

        # Header row: big day number + weekday abbrev to its right.
        num_lbl = QLabel(str(day.day), self)
        num_lbl.setStyleSheet(
            "color: #e0e0e0; font-size: 16pt; font-weight: bold;"
        )

        wd_lbl = QLabel(_WEEKDAY_ABBREV.get(day.weekday(), ""), self)
        wd_lbl.setStyleSheet("color: #909090; font-size: 9pt;")
        wd_lbl.setAlignment(Qt.AlignmentFlag.AlignBottom)

        header = QHBoxLayout()
        header.setSpacing(4)
        header.addWidget(num_lbl)
        header.addWidget(wd_lbl)
        header.addStretch()

        # P&L line.
        if n_trades == 0:
            pnl_text = "$0"
            pnl_color = _COLOR_NONE
        else:
            sign = "-" if pnl < 0 else ""
            pnl_text = f"{sign}${abs(pnl):,.2f}"
            pnl_color = _COLOR_WIN if pnl >= 0 else _COLOR_LOSS
        pnl_lbl = QLabel(pnl_text, self)
        pnl_lbl.setStyleSheet(
            f"color: {pnl_color}; font-size: 12pt; font-weight: 500;"
        )

        count_lbl = QLabel(
            f"{n_trades} trade" if n_trades == 1
            else f"{n_trades} trades",
            self,
        )
        count_lbl.setStyleSheet("color: #808080; font-size: 9pt;")

        body = QVBoxLayout(self)
        body.setContentsMargins(8, 6, 8, 6)
        body.setSpacing(2)
        body.addLayout(header)
        body.addStretch()
        body.addWidget(pnl_lbl)
        body.addWidget(count_lbl)


class WeekStripWidget(QFrame):
    """Horizontal strip of recent weekdays with per-day P&L summary."""

    def __init__(
        self,
        parent=None,
        *,
        lookback: int = _DEFAULT_LOOKBACK_DAYS,
    ):
        super().__init__(parent)
        self.setObjectName("WeekStrip")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self._lookback = max(1, lookback)
        # Title row at the top — month label.
        self._title = QLabel("", self)
        self._title.setStyleSheet(
            "color: #e0e0e0; font-size: 12pt; font-weight: bold;"
        )

        self._cells_row = QHBoxLayout()
        self._cells_row.setContentsMargins(0, 0, 0, 0)
        self._cells_row.setSpacing(6)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 4, 8, 4)
        outer.setSpacing(6)
        outer.addWidget(self._title)
        outer.addLayout(self._cells_row)

        self.refresh([])

    def refresh(self, closed_trades: list[dict]) -> None:
        # Clear existing cells.
        while self._cells_row.count():
            item = self._cells_row.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)

        days = _last_n_weekdays(self._lookback, today=_dt.date.today())
        per_day = _aggregate_pnl_by_date(closed_trades)

        # Title is the most recent cell's month + year (with prefix
        # if the strip spans two months).
        if days:
            last = days[-1]
            first = days[0]
            if (first.year, first.month) == (last.year, last.month):
                self._title.setText(last.strftime("%b %Y"))
            else:
                self._title.setText(
                    f"{first.strftime('%b')} – {last.strftime('%b %Y')}"
                )
        else:
            self._title.setText("")

        for d in days:
            pnl, n = per_day.get(d, (0.0, 0))
            self._cells_row.addWidget(_DayCell(d, pnl, n))
