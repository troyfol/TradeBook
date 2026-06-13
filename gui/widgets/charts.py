"""pyqtgraph-backed equity curve and daily P&L bar chart."""
from __future__ import annotations

from datetime import datetime

import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import QLabel

from analytics.metrics import daily_pnl_bars, equity_curve_points
from gui.widgets.chart_palette import get_palette, palette_hub


# Muted gray for tick-label text so the numbers recede and the line/bars
# carry the visual weight. The axis titles keep the accent color.
_TICK_MUTED = "#888888"


def _fmt_dollars(value: float) -> str:
    """Human-readable $ string: `$12`, `$1.2k`, `-$5.50`, `$0`."""
    sign = "-" if value < 0 else ""
    mag = abs(value)
    if mag < 0.005:
        return "$0"
    if mag >= 1_000_000:
        return f"{sign}${mag / 1_000_000:.1f}M"
    if mag >= 1_000:
        return f"{sign}${mag / 1_000:.1f}k"
    if mag >= 1 and mag == int(mag):
        return f"{sign}${int(mag):,}"
    return f"{sign}${mag:,.2f}"


class _DollarAxis(pg.AxisItem):
    """Y-axis that prefixes every tick with `$` and collapses to k/M at scale."""

    def tickStrings(self, values, scale, spacing):  # noqa: N802 (pg API)
        return [_fmt_dollars(v * scale) for v in values]


def _translucent(hex_color: str, alpha: int) -> QColor:
    c = QColor(hex_color)
    c.setAlpha(alpha)
    return c


class _ChartOverlay(QLabel):
    """Corner P&L badge floating over a pg.PlotWidget.

    Repositioned by the host widget's resizeEvent so it always sticks
    to the top-right just inside the plot area.
    """

    MARGIN = 10

    def __init__(self, parent):
        super().__init__("", parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        font = QFont()
        font.setPointSize(13)
        font.setBold(True)
        self.setFont(font)
        self.hide()

    def set_value(self, value: float | None, positive_hex: str, negative_hex: str) -> None:
        if value is None:
            self.hide()
            return
        color = positive_hex if value >= 0 else negative_hex
        self.setText(_fmt_dollars(value))
        self.setStyleSheet(
            f"color: {color}; "
            "background: rgba(15, 15, 15, 170); "
            "padding: 3px 8px; "
            "border-radius: 4px;"
        )
        self.adjustSize()
        self.show()
        self.reposition()

    def reposition(self) -> None:
        if not self.isVisible():
            return
        parent = self.parent()
        if parent is None:
            return
        self.move(
            parent.width() - self.width() - self.MARGIN,
            self.MARGIN,
        )


class EquityCurveWidget(pg.PlotWidget):
    """Cumulative net P&L over time. X-axis uses pyqtgraph's DateAxisItem."""

    def __init__(self, parent=None):
        super().__init__(
            parent=parent,
            axisItems={
                "bottom": pg.DateAxisItem(),
                "left": _DollarAxis(orientation="left"),
            },
        )
        self.setLabel("left", "Cumulative Net P&L")
        self.setLabel("bottom", "Date")
        self.showGrid(x=False, y=True, alpha=0.15)
        self.setMenuEnabled(False)
        self.setMouseEnabled(x=True, y=False)
        # Reserve fixed space for axis text so the bottom axis (ticks +
        # label) is never squeezed out and the left axis label has room
        # to render its rotated text.
        self.getPlotItem().getAxis("bottom").setHeight(34)
        self.getPlotItem().getAxis("left").setWidth(72)
        self.getPlotItem().setContentsMargins(4, 6, 10, 4)
        # Generous padding so peaks and troughs aren't flush with the edge.
        self.getPlotItem().getViewBox().setDefaultPadding(0.12)
        self.setMinimumHeight(140)
        self._trades: list[dict] = []
        self._overlay = _ChartOverlay(self)
        self._apply_palette()
        palette_hub().palette_changed.connect(self._on_palette_changed)

    def _apply_palette(self) -> None:
        pal = get_palette(self)
        self.setBackground(pal.background)
        for side in ("left", "bottom"):
            ax = self.getAxis(side)
            ax.setTextPen(_TICK_MUTED)
            ax.setPen(pal.axis)
            ax.setLabel(text=ax.labelText, color=pal.label)

    def _on_palette_changed(self) -> None:
        self._apply_palette()
        self.set_data(self._trades)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        # pyqtgraph's base __init__ fires a resize before subclass attrs
        # are assigned — guard against that.
        overlay = getattr(self, "_overlay", None)
        if overlay is not None:
            overlay.reposition()

    def set_data(self, trades: list[dict]) -> None:
        self._trades = list(trades)
        self.clear()
        times, cum = equity_curve_points(trades)
        if not times:
            self._overlay.set_value(None, "", "")
            return
        xs = [_to_unix(t) for t in times]
        pal = get_palette(self)

        # Build a single polyline that's been split at every zero crossing,
        # so each (xi, yi) → (xi+1, yi+1) pair lives entirely on one side
        # of the x-axis. We then plot the green and red sub-segments as
        # separate connect="finite" arrays with NaN gaps in between.
        green_x, green_y, red_x, red_y = _split_at_zero(xs, cum)

        win_pen = pg.mkPen(
            color=pal.positive, width=5,
            capStyle=Qt.PenCapStyle.RoundCap,
            joinStyle=Qt.PenJoinStyle.RoundJoin,
        )
        loss_pen = pg.mkPen(
            color=pal.negative, width=5,
            capStyle=Qt.PenCapStyle.RoundCap,
            joinStyle=Qt.PenJoinStyle.RoundJoin,
        )
        win_fill = _translucent(pal.positive, 55)
        loss_fill = _translucent(pal.negative, 55)

        if len(green_x) >= 2:
            self.plot(
                green_x, green_y,
                pen=win_pen,
                symbol="o", symbolSize=5,
                symbolBrush=pal.positive, symbolPen=None,
                fillLevel=0, fillBrush=win_fill,
                connect="finite",
            )
        if len(red_x) >= 2:
            self.plot(
                red_x, red_y,
                pen=loss_pen,
                symbol="o", symbolSize=5,
                symbolBrush=pal.negative, symbolPen=None,
                fillLevel=0, fillBrush=loss_fill,
                connect="finite",
            )

        zero_pen = pg.mkPen(
            color=_translucent(pal.label, 90),
            width=1,
            style=Qt.PenStyle.DashLine,
        )
        self.addLine(y=0, pen=zero_pen)

        self._overlay.set_value(cum[-1], pal.positive, pal.negative)


class DailyPnLBarWidget(pg.PlotWidget):
    """One bar per trading day (no weekend gaps). Green for up, red for down."""

    def __init__(self, parent=None):
        self._date_axis = _DateStringAxis(orientation="bottom")
        super().__init__(
            parent=parent,
            axisItems={
                "bottom": self._date_axis,
                "left": _DollarAxis(orientation="left"),
            },
        )
        self.setLabel("left", "Net P&L")
        self.setLabel("bottom", "Trading Day")
        self.showGrid(x=False, y=True, alpha=0.15)
        self.setMenuEnabled(False)
        self.setMouseEnabled(x=True, y=False)
        self.getPlotItem().getAxis("bottom").setHeight(34)
        self.getPlotItem().getAxis("left").setWidth(72)
        self.getPlotItem().setContentsMargins(4, 6, 10, 4)
        self.getPlotItem().getViewBox().setDefaultPadding(0.12)
        self.setMinimumHeight(140)
        self._trades: list[dict] = []
        self._overlay = _ChartOverlay(self)
        self._apply_palette()
        palette_hub().palette_changed.connect(self._on_palette_changed)

    def _apply_palette(self) -> None:
        pal = get_palette(self)
        self.setBackground(pal.background)
        for side in ("left", "bottom"):
            ax = self.getAxis(side)
            ax.setTextPen(_TICK_MUTED)
            ax.setPen(pal.axis)
            ax.setLabel(text=ax.labelText, color=pal.label)

    def _on_palette_changed(self) -> None:
        self._apply_palette()
        self.set_data(self._trades)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        # pyqtgraph's base __init__ fires a resize before subclass attrs
        # are assigned — guard against that.
        overlay = getattr(self, "_overlay", None)
        if overlay is not None:
            overlay.reposition()

    def set_data(self, trades: list[dict]) -> None:
        self._trades = list(trades)
        self.clear()
        bars = daily_pnl_bars(trades)
        if not bars:
            self._date_axis.set_dates([])
            self._overlay.set_value(None, "", "")
            return

        dates = [d for d, _ in bars]
        heights = [v for _, v in bars]
        self._date_axis.set_dates(dates)
        pal = get_palette(self)

        green_x, green_h, red_x, red_h = [], [], [], []
        for i, h in enumerate(heights):
            if h >= 0:
                green_x.append(i)
                green_h.append(h)
            else:
                red_x.append(i)
                red_h.append(h)

        # Bar width scales with bar count: narrower when there are few bars so
        # they don't look comically fat across a wide plot.
        n = len(bars)
        bar_width = 0.5 if n >= 6 else 0.35

        green_brush = _translucent(pal.positive, 210)
        red_brush = _translucent(pal.negative, 210)
        green_pen = pg.mkPen(color=pal.positive, width=1.5)
        red_pen = pg.mkPen(color=pal.negative, width=1.5)

        if green_x:
            self.addItem(pg.BarGraphItem(
                x=green_x, height=green_h, width=bar_width,
                brush=green_brush, pen=green_pen,
            ))
        if red_x:
            self.addItem(pg.BarGraphItem(
                x=red_x, height=red_h, width=bar_width,
                brush=red_brush, pen=red_pen,
            ))

        zero_pen = pg.mkPen(
            color=_translucent(pal.label, 90),
            width=1,
            style=Qt.PenStyle.DashLine,
        )
        self.addLine(y=0, pen=zero_pen)
        # Pad x-range so end bars aren't flush with the chart edges.
        self.setXRange(-0.75, n - 0.25, padding=0)

        self._overlay.set_value(heights[-1], pal.positive, pal.negative)


class _DateStringAxis(pg.AxisItem):
    """Integer tick positions rendered as date strings.

    Overrides `tickValues` so pyqtgraph can't auto-generate sub-integer
    ticks that all round to the same data index (which would render as
    duplicate date labels).
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._dates: list = []

    def set_dates(self, dates) -> None:
        self._dates = list(dates)
        self.picture = None  # invalidate any cached render

    def tickValues(self, minVal, maxVal, size):  # noqa: N802 (pg API)
        n = len(self._dates)
        if n == 0:
            return []
        lo = max(0, int(minVal) if minVal > 0 else 0)
        hi = min(n - 1, int(maxVal) + 1)
        candidates = list(range(lo, hi + 1))
        # Cap at ~10 visible labels to avoid overlap on dense ranges.
        max_ticks = 10
        if len(candidates) > max_ticks:
            step = max(1, len(candidates) // max_ticks)
            candidates = candidates[::step]
        return [(1.0, [float(c) for c in candidates])]

    def tickStrings(self, values, scale, spacing):  # noqa: N802 (pg API)
        out = []
        for v in values:
            i = int(round(v))
            if 0 <= i < len(self._dates):
                out.append(self._dates[i].strftime("%m/%d"))
            else:
                out.append("")
        return out


def _to_unix(dt: datetime) -> float:
    """Naive datetime → POSIX timestamp (local interpretation). pyqtgraph's
    DateAxisItem expects seconds since epoch.
    """
    return dt.timestamp()


def _split_at_zero(
    xs: list[float], ys: list[float],
) -> tuple[list[float], list[float], list[float], list[float]]:
    """Split a polyline into green (y >= 0) and red (y <= 0) sub-curves.

    Linearly interpolates the exact x at every zero crossing so the two
    curves meet on the x-axis with no visible gap. NaN values are inserted
    to break the polyline at points that don't belong to a given side, so
    the result can be plotted with `connect="finite"` (a single pyqtgraph
    `plot` call per color).
    """
    if not xs:
        return [], [], [], []

    nan = float("nan")
    g_x: list[float] = []
    g_y: list[float] = []
    r_x: list[float] = []
    r_y: list[float] = []

    def push(side: str, x: float, y: float) -> None:
        if side == "g":
            g_x.append(x); g_y.append(y)
        else:
            r_x.append(x); r_y.append(y)

    def gap(side: str) -> None:
        if side == "g":
            if g_x and not (g_x[-1] != g_x[-1]):  # last point isn't already NaN
                g_x.append(nan); g_y.append(nan)
        else:
            if r_x and not (r_x[-1] != r_x[-1]):
                r_x.append(nan); r_y.append(nan)

    # Seed: classify the first sample.
    y0 = ys[0]
    if y0 >= 0:
        push("g", xs[0], y0)
        gap("r")
    else:
        push("r", xs[0], y0)
        gap("g")

    for i in range(1, len(xs)):
        x0, y0 = xs[i - 1], ys[i - 1]
        x1, y1 = xs[i], ys[i]

        same_sign = (y0 >= 0 and y1 >= 0) or (y0 < 0 and y1 < 0)
        if same_sign:
            side = "g" if y1 >= 0 else "r"
            push(side, x1, y1)
            # Other side gets a gap so its polyline doesn't reconnect later.
            gap("r" if side == "g" else "g")
            continue

        # Sign change → linear interp for the zero-crossing x.
        # y = y0 + (y1 - y0) * t  ⟹  t = -y0 / (y1 - y0)
        denom = (y1 - y0)
        t = (-y0 / denom) if denom != 0 else 0.0
        xc = x0 + (x1 - x0) * t

        if y0 >= 0:
            # Green ends at the crossing, red picks up from there.
            push("g", xc, 0.0)
            gap("g")
            push("r", xc, 0.0)
            push("r", x1, y1)
        else:
            push("r", xc, 0.0)
            gap("r")
            push("g", xc, 0.0)
            push("g", x1, y1)

    return g_x, g_y, r_x, r_y
