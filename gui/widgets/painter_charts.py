"""QPainter-based chart primitives: donut + half-circle gauge.

Self-contained (no pyqtgraph / matplotlib dependency for these). Each
widget exposes a small ``set_data`` API and re-paints from cached
state. Designed for inclusion in ``ChartCard``s on the Dashboard.
"""
from __future__ import annotations

import math
from typing import Optional

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import (
    QBrush, QColor, QFontMetrics, QPainter, QPaintEvent, QPen,
)
from PySide6.QtWidgets import QSizePolicy, QWidget

from gui.widgets.chart_palette import get_palette, palette_hub


COLOR_NEUTRAL = QColor("#606060")
COLOR_HINT = QColor("#909090")
# Calendar-widget brights — reused here so the Largest Gain / Largest
# Loss gauge matches the green/red a profitable or losing day shows
# on the calendar grid regardless of the user's chart palette.
CALENDAR_WIN_BRIGHT = QColor("#00C853")
CALENDAR_LOSS_BRIGHT = QColor("#FF1744")


def _win(w=None) -> QColor:
    return QColor(get_palette(w).positive)


def _loss(w=None) -> QColor:
    return QColor(get_palette(w).negative)


def _text(w=None) -> QColor:
    return QColor(get_palette(w).label)


def _axis(w=None) -> QColor:
    return QColor(get_palette(w).axis)


# ---------------------------------------------------------------------------
# Donut chart — two-slice (winners vs losers) with a centred percentage.
# ---------------------------------------------------------------------------


class DonutChart(QWidget):
    """Two-slice donut + big % label in the hole. Designed for
    "Winning vs Losing Trades" on the dashboard."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(120, 120)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding,
        )
        self._wins = 0
        self._losses = 0
        palette_hub().palette_changed.connect(self.update)

    def set_data(self, wins: int, losses: int) -> None:
        self._wins = max(0, int(wins))
        self._losses = max(0, int(losses))
        self.update()

    def paintEvent(self, _ev: QPaintEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Compute centred square painting region.
        side = min(self.width(), self.height()) - 12
        if side <= 0:
            return
        cx, cy = self.width() / 2, self.height() / 2
        rect = QRectF(cx - side / 2, cy - side / 2, side, side)
        ring_thickness = max(8.0, side * 0.16)

        total = self._wins + self._losses

        # Background ring (so a 100%-win or 100%-loss case still
        # reads as a complete circle).
        p.setPen(QPen(COLOR_NEUTRAL, ring_thickness, Qt.PenStyle.SolidLine,
                      Qt.PenCapStyle.FlatCap))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawArc(rect.adjusted(
            ring_thickness / 2, ring_thickness / 2,
            -ring_thickness / 2, -ring_thickness / 2,
        ), 0, 360 * 16)

        if total > 0:
            win_frac = self._wins / total
            # Qt arcs use 1/16 of a degree, going counter-clockwise
            # from 3 o'clock. We want clockwise from 12 o'clock so
            # the wins fill from the top.
            start = 90 * 16
            win_span = -int(win_frac * 360 * 16)
            loss_span = -int((1 - win_frac) * 360 * 16)
            inner_rect = rect.adjusted(
                ring_thickness / 2, ring_thickness / 2,
                -ring_thickness / 2, -ring_thickness / 2,
            )
            if self._wins > 0:
                p.setPen(QPen(_win(self), ring_thickness,
                              Qt.PenStyle.SolidLine,
                              Qt.PenCapStyle.FlatCap))
                p.drawArc(inner_rect, start, win_span)
            if self._losses > 0:
                p.setPen(QPen(_loss(self), ring_thickness,
                              Qt.PenStyle.SolidLine,
                              Qt.PenCapStyle.FlatCap))
                p.drawArc(inner_rect, start + win_span, loss_span)

        # Centred label.
        p.setPen(_text(self))
        font = p.font()
        font.setPointSizeF(max(11.0, side * 0.13))
        font.setBold(True)
        p.setFont(font)
        if total == 0:
            label = "—"
        else:
            label = f"{self._wins / total * 100:.0f}%"
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)

        # Sub-label below the percentage.
        sub_font = p.font()
        sub_font.setPointSizeF(max(7.5, side * 0.06))
        sub_font.setBold(False)
        p.setFont(sub_font)
        p.setPen(COLOR_HINT)
        sub_rect = QRectF(rect)
        sub_rect.translate(0, side * 0.18)
        sub = (
            f"{self._wins} W · {self._losses} L"
            if total > 0 else "no trades"
        )
        p.drawText(sub_rect, Qt.AlignmentFlag.AlignCenter, sub)


# ---------------------------------------------------------------------------
# Half-circle gauge — used for Profit Factor and Largest Gain vs Largest Loss.
# ---------------------------------------------------------------------------


class GaugeChart(QWidget):
    """Half-circle dial. ``set_value(numerator, denominator)`` fills
    the arc by the ratio (clamped 0–1). Color is green when the
    numerator is the "good" half (configurable), red otherwise.

    For Profit Factor: ``set_value(profit_factor, full_scale)`` —
    ratio caps at 1.0 visually but the label can show the true
    number (e.g. "PF 4.20" with a full-arc draw).

    For Largest Gain vs Largest Loss: pass them directly; the dial
    fills toward the dominant side (green if gain > loss).
    """

    MODE_GAIN_LOSS = "gain_loss"   # split-color: gain side green, loss side red
    MODE_PROFIT_FACTOR = "pf"     # single arc, color by value

    def __init__(self, parent=None, *, mode: str = MODE_PROFIT_FACTOR):
        super().__init__(parent)
        self.setMinimumSize(140, 100)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding,
        )
        self._mode = mode
        self._gain = 0.0
        self._loss = 0.0
        self._pf = 0.0
        self._pf_cap = 3.0   # PF >= cap fills the whole arc
        palette_hub().palette_changed.connect(self.update)

    # --- public API --------------------------------------------------

    def set_gain_loss(self, gain: float, loss: float) -> None:
        """Set numbers used for the split-color half-circle."""
        self._mode = self.MODE_GAIN_LOSS
        self._gain = max(0.0, float(gain))
        self._loss = max(0.0, float(loss))
        self.update()

    def set_profit_factor(self, pf: Optional[float], cap: float = 3.0) -> None:
        self._mode = self.MODE_PROFIT_FACTOR
        self._pf_cap = max(1.0, float(cap))
        self._pf = float(pf) if (pf is not None and math.isfinite(pf)) else 0.0
        self.update()

    # --- paint -------------------------------------------------------

    def paintEvent(self, _ev: QPaintEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Half-circle takes top ~70% of the widget; label sits below.
        avail_w = self.width() - 16
        avail_h = self.height() - 36
        side = min(avail_w, avail_h * 2)
        if side <= 0:
            return
        cx = self.width() / 2
        # Center the arc vertically so the bottom of the half-circle
        # has room for a label.
        top = (self.height() - 36 - side / 2) / 2
        rect = QRectF(cx - side / 2, top, side, side)
        thickness = max(10.0, side * 0.10)
        inner = rect.adjusted(
            thickness / 2, thickness / 2,
            -thickness / 2, -thickness / 2,
        )

        # Background half-arc — provides the "track" the colored arc
        # fills along.
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(_axis(self), thickness,
                      Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
        p.drawArc(inner, 0, 180 * 16)

        # Foreground arc(s).
        if self._mode == self.MODE_GAIN_LOSS:
            total = self._gain + self._loss
            if total > 0:
                gain_frac = self._gain / total
                # 180 degrees total split into gain (left) and loss (right).
                # We draw counter-clockwise from 0 (3 o'clock) toward 180
                # (9 o'clock). Gain fills from the LEFT, loss from the RIGHT
                # so the colored side that's bigger visually dominates.
                gain_span = int(gain_frac * 180 * 16)
                loss_span = int((1 - gain_frac) * 180 * 16)
                if self._gain > 0:
                    p.setPen(QPen(CALENDAR_WIN_BRIGHT, thickness,
                                  Qt.PenStyle.SolidLine,
                                  Qt.PenCapStyle.FlatCap))
                    # Gain occupies the left half (90° → 180°).
                    p.drawArc(inner, 180 * 16 - gain_span, gain_span)
                if self._loss > 0:
                    p.setPen(QPen(CALENDAR_LOSS_BRIGHT, thickness,
                                  Qt.PenStyle.SolidLine,
                                  Qt.PenCapStyle.FlatCap))
                    p.drawArc(inner, 0, loss_span)
            label = (
                f"+${self._gain:,.2f}  /  -${self._loss:,.2f}"
                if (self._gain or self._loss) else "no trades"
            )
        else:  # Profit Factor mode
            frac = min(1.0, self._pf / self._pf_cap) if self._pf_cap else 0.0
            color = (
                _win(self) if self._pf >= 1.0
                else (_loss(self) if self._pf > 0 else COLOR_NEUTRAL)
            )
            if frac > 0:
                p.setPen(QPen(color, thickness,
                              Qt.PenStyle.SolidLine,
                              Qt.PenCapStyle.FlatCap))
                # Fill from left (180°) toward right (0°).
                span = int(frac * 180 * 16)
                p.drawArc(inner, 180 * 16 - span, span)
            label = (
                f"{self._pf:.2f}" if self._pf > 0 else "—"
            )

        # Bottom label.
        font = p.font()
        font.setPointSizeF(max(10.0, side * 0.09))
        font.setBold(True)
        p.setFont(font)
        bottom_rect = QRectF(
            0, self.height() - 32, self.width(), 28,
        )
        if (
            self._mode == self.MODE_GAIN_LOSS
            and (self._gain or self._loss)
        ):
            # Render three segments with independent colors so the
            # gain side is calendar-green, the loss side calendar-red,
            # and the separating slash stays the default label color.
            gain_text = f"+${self._gain:,.2f}"
            sep = "  /  "
            loss_text = f"-${self._loss:,.2f}"
            fm = QFontMetrics(font)
            gw = fm.horizontalAdvance(gain_text)
            sw = fm.horizontalAdvance(sep)
            lw = fm.horizontalAdvance(loss_text)
            total = gw + sw + lw
            x = (self.width() - total) / 2
            y_base = bottom_rect.center().y() + fm.ascent() / 2 - 2
            p.setPen(CALENDAR_WIN_BRIGHT)
            p.drawText(int(x), int(y_base), gain_text)
            x += gw
            p.setPen(_text(self))
            p.drawText(int(x), int(y_base), sep)
            x += sw
            p.setPen(CALENDAR_LOSS_BRIGHT)
            p.drawText(int(x), int(y_base), loss_text)
        else:
            p.setPen(_text(self))
            p.drawText(bottom_rect, Qt.AlignmentFlag.AlignCenter, label)
