"""Custom-painted Mon–Fri calendar grid with weekly-total column.

Renders a `CalendarMonth` data structure (from `analytics.calendar_data`)
as a 5-weekday + 1-total grid. Cell color intensity is normalized per
visible month against `CalendarMonth.max_abs_pnl`.

Cell content modes (selectable from outside via `set_cell_mode`):
    MODE_PNL_ONLY      — just net P&L
    MODE_PNL_COUNT     — net P&L + trade count
    MODE_PNL_COUNT_WL  — net P&L + trade count + W/L split

Signals:
    day_clicked(date)         — single click on an in-month cell
    day_double_clicked(date)  — double click on an in-month cell
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from PySide6.QtCore import QRect, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor, QFont, QFontMetrics, QMouseEvent, QPainter, QPen,
)
from PySide6.QtWidgets import QWidget

from analytics.calendar_data import CalendarMonth, DayCell, WeekRow
from gui.widgets.chart_palette import get_palette, palette_hub

# Cell content modes
MODE_PNL_ONLY = "pnl"
MODE_PNL_COUNT = "pnl_count"
MODE_PNL_COUNT_WL = "pnl_count_wl"

# Theme
COLOR_BG = QColor("#1e1e1e")
COLOR_CELL_EMPTY = QColor("#2a2a2a")
COLOR_CELL_OUT = QColor("#1a1a1a")     # filler weekdays from adjacent months
COLOR_CELL_NEUTRAL = QColor("#2d2d2d")  # in-month, no trades
COLOR_GRID = QColor("#3c3c3c")
COLOR_FG = QColor("#e0e0e0")
COLOR_MUTED = QColor("#808080")
COLOR_DIM = QColor("#5a5a5a")
COLOR_TODAY = QColor("#00A3FF")
COLOR_SEL = QColor("#FFD740")          # amber selection ring
# Bright accents kept for chart parity but no longer used as cell fills.
COLOR_WIN_BRIGHT = QColor("#00C853")
COLOR_LOSS_BRIGHT = QColor("#FF1744")
# Dulled pastel targets used for day-cell backgrounds — chosen so black
# text is readable across the entire intensity range.
COLOR_WIN_BASE = QColor("#81C784")     # Material green 300
COLOR_LOSS_BASE = QColor("#E57373")    # Material red 300
COLOR_LERP_START = QColor("#3a3a3a")   # slightly lighter than neutral so the
                                       # smallest-magnitude day still tints clearly
COLOR_CELL_TEXT = QColor("#000000")    # black text on any colored day cell
COLOR_TOTAL_BG = QColor("#252525")

WEEKDAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri"]
TOTAL_LABEL = "Total"

CELL_MIN_W = 110
CELL_MIN_H = 78
HEADER_H = 26


def _lerp_color(base: QColor, target: QColor, t: float) -> QColor:
    """Linear interpolate between two colors. t in [0, 1]."""
    t = max(0.0, min(1.0, t))
    r = base.red() + (target.red() - base.red()) * t
    g = base.green() + (target.green() - base.green()) * t
    b = base.blue() + (target.blue() - base.blue()) * t
    return QColor(int(r), int(g), int(b))


def _fmt_currency(v: float) -> str:
    if v == 0:
        return "$0.00"
    sign = "-" if v < 0 else ""
    return f"{sign}${abs(v):,.2f}"


class CalendarGrid(QWidget):
    """Stateless renderer of a single CalendarMonth.

    Pass a CalendarMonth via `set_month`. Cell content mode toggled via
    `set_cell_mode`. Selection state stored locally; cleared when the
    visible month changes.
    """

    day_clicked = Signal(object)         # date
    day_double_clicked = Signal(object)  # date

    def __init__(self, parent=None):
        super().__init__(parent)
        self._month: Optional[CalendarMonth] = None
        self._cell_mode: str = MODE_PNL_ONLY
        self._selected: Optional[date] = None

        # Min size: 6 columns (5 weekdays + total) by ~6 rows (header + up to 5 weeks)
        self.setMinimumSize(
            CELL_MIN_W * 6 + 12,
            HEADER_H + CELL_MIN_H * 6 + 12,
        )
        self.setMouseTracking(False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        # Repaint when the user changes the chart palette so the
        # today-cell border tracks the new "Labels & ticks" color.
        palette_hub().palette_changed.connect(self.update)

    # ---- public API --------------------------------------------------------

    def set_month(self, month: Optional[CalendarMonth]) -> None:
        self._month = month
        self._selected = None
        self.update()

    def set_cell_mode(self, mode: str) -> None:
        if mode not in (MODE_PNL_ONLY, MODE_PNL_COUNT, MODE_PNL_COUNT_WL):
            return
        self._cell_mode = mode
        self.update()

    def cell_mode(self) -> str:
        return self._cell_mode

    def selected_day(self) -> Optional[date]:
        return self._selected

    def set_selected_day(self, d: Optional[date]) -> None:
        self._selected = d
        self.update()

    # ---- size hint ---------------------------------------------------------

    def sizeHint(self) -> QSize:  # noqa: N802 (Qt API)
        return QSize(CELL_MIN_W * 6 + 12, HEADER_H + CELL_MIN_H * 6 + 12)

    # ---- layout ------------------------------------------------------------

    def _grid_geometry(self) -> tuple[int, int, int, int, int, int]:
        """Return (x0, y0, col_w, row_h, n_cols, n_rows).

        n_cols = 6 (5 weekdays + total). n_rows = number of weeks in month.
        """
        n_cols = 6
        n_rows = len(self._month.rows) if self._month else 1
        n_rows = max(n_rows, 1)
        margin = 6
        avail_w = self.width() - margin * 2
        avail_h = self.height() - margin * 2 - HEADER_H
        col_w = max(CELL_MIN_W, avail_w // n_cols)
        row_h = max(CELL_MIN_H, avail_h // n_rows)
        x0 = margin
        y0 = margin + HEADER_H
        return x0, y0, col_w, row_h, n_cols, n_rows

    def _cell_rect(self, row: int, col: int) -> QRect:
        x0, y0, col_w, row_h, _, _ = self._grid_geometry()
        return QRect(x0 + col * col_w, y0 + row * row_h, col_w, row_h)

    def _hit_test(self, x, y) -> Optional[tuple[int, int, DayCell]]:
        if self._month is None:
            return None
        x = int(x)
        y = int(y)
        x0, y0, col_w, row_h, n_cols, n_rows = self._grid_geometry()
        if x < x0 or y < y0:
            return None
        col = (x - x0) // col_w
        row = (y - y0) // row_h
        if col >= 5 or row >= len(self._month.rows):  # ignore total column
            return None
        try:
            cell = self._month.rows[row].cells[col]
        except (IndexError, TypeError):
            return None
        return int(row), int(col), cell

    # ---- mouse events ------------------------------------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        hit = self._hit_test(event.position().x(), event.position().y())
        if hit is None:
            return
        _, _, cell = hit
        if not cell.in_month:
            return
        self._selected = cell.day
        self.update()
        self.day_clicked.emit(cell.day)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        hit = self._hit_test(event.position().x(), event.position().y())
        if hit is None:
            return
        _, _, cell = hit
        if not cell.in_month:
            return
        self._selected = cell.day
        self.update()
        self.day_double_clicked.emit(cell.day)

    # ---- painting ----------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), COLOR_BG)

        if self._month is None or not self._month.rows:
            painter.setPen(COLOR_MUTED)
            f = painter.font()
            f.setPointSizeF(13)
            f.setItalic(True)
            painter.setFont(f)
            painter.drawText(
                self.rect(),
                Qt.AlignmentFlag.AlignCenter,
                "No data for this month.",
            )
            painter.end()
            return

        x0, y0, col_w, row_h, n_cols, _ = self._grid_geometry()

        # --- header row -----------------------------------------------------
        painter.setPen(COLOR_MUTED)
        hf = painter.font()
        hf.setPointSizeF(9)
        hf.setBold(True)
        painter.setFont(hf)
        for c, label in enumerate(WEEKDAY_LABELS):
            r = QRect(x0 + c * col_w, y0 - HEADER_H, col_w, HEADER_H)
            painter.drawText(r, Qt.AlignmentFlag.AlignCenter, label)
        r = QRect(x0 + 5 * col_w, y0 - HEADER_H, col_w, HEADER_H)
        painter.drawText(r, Qt.AlignmentFlag.AlignCenter, TOTAL_LABEL)

        max_abs = self._month.max_abs_pnl

        today = date.today()

        for ri, week in enumerate(self._month.rows):
            for ci, cell in enumerate(week.cells):
                rect = self._cell_rect(ri, ci)
                self._paint_day_cell(painter, rect, cell, max_abs, today)
            total_rect = self._cell_rect(ri, 5)
            self._paint_total_cell(painter, total_rect, week)

        painter.end()

    def _paint_day_cell(
        self,
        painter: QPainter,
        rect: QRect,
        cell: DayCell,
        max_abs: float,
        today: date,
    ) -> None:
        has_trades = cell.in_month and cell.trade_count > 0

        # Background fill.
        if not cell.in_month:
            bg = COLOR_CELL_OUT
        elif not has_trades:
            bg = COLOR_CELL_NEUTRAL
        else:
            if max_abs > 0:
                t = min(1.0, abs(cell.net_pnl) / max_abs)
            else:
                t = 0.0
            # Floor of 0.6 guarantees even the smallest day has a clearly
            # visible tint that supports black text. Cap at 1.0 (pastel).
            t = 0.6 + 0.4 * t
            base = COLOR_WIN_BASE if cell.net_pnl >= 0 else COLOR_LOSS_BASE
            bg = _lerp_color(COLOR_LERP_START, base, t)

        painter.fillRect(rect.adjusted(1, 1, -1, -1), bg)

        # Border (selection / today / default).
        if self._selected == cell.day and cell.in_month:
            pen = QPen(COLOR_SEL)
            pen.setWidth(2)
            painter.setPen(pen)
        elif cell.day == today and cell.in_month:
            # Today border picks up the user-chosen "Labels & ticks"
            # color so it stays in family with the rest of the theme.
            pen = QPen(QColor(get_palette().label))
            pen.setWidth(2)
            painter.setPen(pen)
        else:
            painter.setPen(QPen(COLOR_GRID, 1))
        painter.drawRect(rect.adjusted(1, 1, -2, -2))

        # Day-number badge (top-left). Black on colored cells, dim grey
        # on the empty/out-of-month cells.
        if has_trades:
            day_color = COLOR_CELL_TEXT
        elif cell.in_month:
            day_color = COLOR_FG
        else:
            day_color = COLOR_DIM
        painter.setPen(day_color)
        df = painter.font()
        df.setPointSizeF(9)
        df.setBold(False)
        df.setItalic(False)
        painter.setFont(df)
        day_rect = QRect(rect.x() + 6, rect.y() + 4, rect.width() - 12, 16)
        painter.drawText(
            day_rect,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
            str(cell.day.day),
        )

        if not cell.in_month:
            return

        if not has_trades:
            painter.setPen(COLOR_DIM)
            ef = painter.font()
            ef.setPointSizeF(14)
            ef.setBold(False)
            painter.setFont(ef)
            painter.drawText(
                rect, Qt.AlignmentFlag.AlignCenter, "—",
            )
            return

        # P&L body — black text on the colored cell, regardless of sign.
        body_rect = QRect(
            rect.x() + 4, rect.y() + 22,
            rect.width() - 8, rect.height() - 26,
        )

        pnl_text = _fmt_currency(cell.net_pnl)
        painter.setPen(COLOR_CELL_TEXT)
        bf = painter.font()
        bf.setPointSizeF(11)
        bf.setBold(True)
        painter.setFont(bf)

        if self._cell_mode == MODE_PNL_ONLY:
            painter.drawText(
                body_rect, Qt.AlignmentFlag.AlignCenter, pnl_text,
            )
        elif self._cell_mode == MODE_PNL_COUNT:
            painter.drawText(
                body_rect,
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                pnl_text,
            )
            sf = painter.font()
            sf.setPointSizeF(8)
            sf.setBold(False)
            painter.setFont(sf)
            painter.setPen(COLOR_CELL_TEXT)
            sub = (
                f"{cell.trade_count} trade"
                if cell.trade_count == 1
                else f"{cell.trade_count} trades"
            )
            painter.drawText(
                body_rect,
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom,
                sub,
            )
        else:  # MODE_PNL_COUNT_WL
            painter.drawText(
                body_rect,
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                pnl_text,
            )
            sf = painter.font()
            sf.setPointSizeF(8)
            sf.setBold(False)
            painter.setFont(sf)
            painter.setPen(COLOR_CELL_TEXT)
            mid = QRect(
                body_rect.x(), body_rect.y() + body_rect.height() // 2 - 6,
                body_rect.width(), 14,
            )
            painter.drawText(
                mid, Qt.AlignmentFlag.AlignCenter,
                f"{cell.trade_count} trade" + ("" if cell.trade_count == 1 else "s"),
            )
            painter.drawText(
                body_rect,
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom,
                f"{cell.win_count}W / {cell.loss_count}L",
            )

    def _paint_total_cell(
        self,
        painter: QPainter,
        rect: QRect,
        week: WeekRow,
    ) -> None:
        painter.fillRect(rect.adjusted(1, 1, -1, -1), COLOR_TOTAL_BG)
        painter.setPen(QPen(COLOR_GRID, 1))
        painter.drawRect(rect.adjusted(1, 1, -2, -2))

        if week.weekly_trade_count == 0:
            painter.setPen(COLOR_DIM)
            f = painter.font()
            f.setPointSizeF(14)
            f.setBold(False)
            painter.setFont(f)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "—")
            return

        net = week.weekly_net
        color = COLOR_WIN_BASE if net >= 0 else COLOR_LOSS_BASE
        painter.setPen(color)
        f = painter.font()
        f.setPointSizeF(11)
        f.setBold(True)
        painter.setFont(f)
        top_rect = QRect(rect.x(), rect.y() + 4, rect.width(), rect.height() // 2)
        painter.drawText(
            top_rect,
            Qt.AlignmentFlag.AlignCenter,
            _fmt_currency(net),
        )
        painter.setPen(COLOR_MUTED)
        sf = painter.font()
        sf.setPointSizeF(8)
        sf.setBold(False)
        painter.setFont(sf)
        bot_rect = QRect(
            rect.x(), rect.y() + rect.height() // 2,
            rect.width(), rect.height() // 2 - 4,
        )
        sub = (
            f"{week.weekly_trade_count} trade"
            if week.weekly_trade_count == 1
            else f"{week.weekly_trade_count} trades"
        )
        painter.drawText(bot_rect, Qt.AlignmentFlag.AlignCenter, sub)
