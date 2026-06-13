"""Composed (non-painter) chart widgets — built from QProgressBar
rows + labels rather than custom-painted shapes.

Used by the Dashboard for:
    * DualHorizontalBar  — Avg Win vs Loss, Hold Time W vs L
    * CategoryBars       — Day of Week, Hour of Day, Price, Duration,
                           Tag Breakdown
    * BigValueCard       — Total Fees

All of them follow the same dark-card aesthetic as the existing
``StatCard``. They emit no signals; data is pushed in via ``set_data``
or ``set_rows``.

Phase 14: ``ChartCard`` gains a draggable title bar with size-cycle
and minimize buttons. The Dashboard listens for ``layout_changed``
and ``swap_requested`` to re-flow the masonry grid + persist state.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QMouseEvent
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QMenu, QProgressBar, QPushButton,
    QSizePolicy, QToolButton, QVBoxLayout, QWidget,
)

from gui.widgets.chart_palette import get_palette, palette_hub


_COLOR_NEUTRAL = "#909090"


def _color_win(w=None) -> str:
    return get_palette(w).positive


def _color_loss(w=None) -> str:
    return get_palette(w).negative


def _fmt_currency(v: float) -> str:
    if v is None:
        return "$0.00"
    sign = "-" if v < 0 else ""
    return f"{sign}${abs(v):,.2f}"


# ---------------------------------------------------------------------------
# DualHorizontalBar — two stacked rows, one for "winner" side, one for "loser"
# ---------------------------------------------------------------------------


class DualHorizontalBar(QFrame):
    """Two stacked horizontal bars with text labels above each.

    Use cases:
        * Avg Winning Trade vs Avg Losing Trade
        * Hold Time of Winning Trades vs Hold Time of Losing Trades
    """

    def __init__(
        self,
        *,
        win_label: str = "Winners",
        loss_label: str = "Losers",
        formatter=_fmt_currency,
        parent=None,
    ):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self._fmt = formatter

        self._win_text = QLabel(f"{win_label}: —", self)
        self._win_text.setStyleSheet("color: #e0e0e0; font-size: 10pt;")
        self._win_bar = QProgressBar(self)
        self._win_bar.setTextVisible(False)
        self._win_bar.setFixedHeight(8)
        self._win_bar.setRange(0, 100)
        self._win_bar.setValue(0)

        self._loss_text = QLabel(f"{loss_label}: —", self)
        self._loss_text.setStyleSheet("color: #e0e0e0; font-size: 10pt;")
        self._loss_bar = QProgressBar(self)
        self._loss_bar.setTextVisible(False)
        self._loss_bar.setFixedHeight(8)
        self._loss_bar.setRange(0, 100)
        self._loss_bar.setValue(0)
        self._apply_palette()
        palette_hub().palette_changed.connect(self._apply_palette)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)
        layout.addWidget(self._win_text)
        layout.addWidget(self._win_bar)
        layout.addSpacing(6)
        layout.addWidget(self._loss_text)
        layout.addWidget(self._loss_bar)

        self._win_label = win_label
        self._loss_label = loss_label

    def _apply_palette(self) -> None:
        self._win_bar.setStyleSheet(
            "QProgressBar { border: none; background: #2d2d2d; "
            "border-radius: 2px; } "
            f"QProgressBar::chunk {{ background: {_color_win(self)}; "
            "border-radius: 2px; }"
        )
        self._loss_bar.setStyleSheet(
            "QProgressBar { border: none; background: #2d2d2d; "
            "border-radius: 2px; } "
            f"QProgressBar::chunk {{ background: {_color_loss(self)}; "
            "border-radius: 2px; }"
        )

    def set_data(
        self,
        win_value: Optional[float],
        loss_value: Optional[float],
    ) -> None:
        """Two raw values (e.g. avg winner $, avg loser $). The bars
        scale to the larger absolute magnitude so both are visible."""
        scale = max(
            abs(win_value or 0.0),
            abs(loss_value or 0.0),
            1e-9,
        )
        if win_value is None:
            self._win_text.setText(f"{self._win_label}: n/a")
            self._win_bar.setValue(0)
        else:
            self._win_text.setText(
                f"{self._win_label}: {self._fmt(win_value)}"
            )
            self._win_bar.setValue(
                int(min(100.0, abs(win_value) / scale * 100.0))
            )
        if loss_value is None:
            self._loss_text.setText(f"{self._loss_label}: n/a")
            self._loss_bar.setValue(0)
        else:
            self._loss_text.setText(
                f"{self._loss_label}: {self._fmt(loss_value)}"
            )
            self._loss_bar.setValue(
                int(min(100.0, abs(loss_value) / scale * 100.0))
            )


# ---------------------------------------------------------------------------
# CategoryBars — list of [label  bar  $value pct%] rows, paginated
# ---------------------------------------------------------------------------


class CategoryBars(QFrame):
    """Compact list of horizontal bars, one per category.

    Each row is ``[ label ]   [ progress bar ]   [ $value  pct% ]``.
    Rows past ``page_size`` get folded behind a ◀ / ▶ pager so the
    Dashboard card stays a fixed height.
    """

    page_changed = Signal(int)

    def __init__(
        self,
        *,
        page_size: int = 7,
        formatter=_fmt_currency,
        pct_basis: str = "abs_total",  # "abs_total" | "win_loss_split"
        parent=None,
    ):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self._page_size = max(1, page_size)
        self._fmt = formatter
        self._pct_basis = pct_basis
        self._rows: list[tuple[str, float]] = []
        self._page = 0

        self._grid = QGridLayout()
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(8)
        self._grid.setVerticalSpacing(4)
        self._grid.setColumnStretch(1, 1)

        self._pager_left = QPushButton("◀", self)
        self._pager_right = QPushButton("▶", self)
        for btn in (self._pager_left, self._pager_right):
            btn.setFixedWidth(28)
            btn.setFixedHeight(22)
        self._pager_left.clicked.connect(lambda: self._step_page(-1))
        self._pager_right.clicked.connect(lambda: self._step_page(1))

        self._page_label = QLabel("", self)
        self._page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._page_label.setStyleSheet("color: #909090; font-size: 9pt;")

        pager_row = QHBoxLayout()
        pager_row.setContentsMargins(0, 0, 0, 0)
        pager_row.setSpacing(4)
        pager_row.addWidget(self._pager_left)
        pager_row.addWidget(self._page_label, 1)
        pager_row.addWidget(self._pager_right)

        self._row_widgets: list[
            tuple[QLabel, QProgressBar, QLabel]
        ] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(4)
        outer.addLayout(self._grid, 1)
        outer.addStretch()
        outer.addLayout(pager_row)

        self._render()
        palette_hub().palette_changed.connect(self._render)

    # ---- public ------------------------------------------------------

    def set_rows(self, rows: list[tuple[str, float]]) -> None:
        """rows is a list of ``(label, value)`` tuples."""
        self._rows = list(rows)
        # Clamp page to valid range.
        n_pages = max(1, (len(self._rows) + self._page_size - 1) // self._page_size)
        if self._page >= n_pages:
            self._page = max(0, n_pages - 1)
        self._render()

    def page_count(self) -> int:
        if not self._rows:
            return 1
        return (len(self._rows) + self._page_size - 1) // self._page_size

    def current_page(self) -> int:
        return self._page

    # ---- internals ---------------------------------------------------

    def _step_page(self, delta: int) -> None:
        target = self._page + delta
        if 0 <= target < self.page_count():
            self._page = target
            self._render()
            self.page_changed.emit(self._page)

    def _percent_for(self, value: float) -> float:
        """Return the % to show next to a row, computed against the
        configured ``pct_basis``.
        """
        if self._pct_basis == "win_loss_split":
            wins = sum(v for _l, v in self._rows if v > 0)
            losses = sum(-v for _l, v in self._rows if v < 0)
            denom = wins if value >= 0 else losses
            return (abs(value) / denom * 100.0) if denom > 0 else 0.0
        # default: % of absolute total
        denom = sum(abs(v) for _l, v in self._rows)
        return (abs(value) / denom * 100.0) if denom > 0 else 0.0

    def _scale_max(self) -> float:
        """Bar fill is scaled to the largest absolute value in the
        dataset (across all pages), so the visual is comparable."""
        return max((abs(v) for _l, v in self._rows), default=1.0)

    def _render(self) -> None:
        # Clear existing row widgets.
        for lbl, bar, stat in self._row_widgets:
            for w in (lbl, bar, stat):
                self._grid.removeWidget(w)
                w.deleteLater()
        self._row_widgets = []

        if not self._rows:
            empty = QLabel("(no data)", self)
            empty.setStyleSheet("color: #606060; font-style: italic;")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._grid.addWidget(empty, 0, 0, 1, 3)
            self._row_widgets = [(empty, QProgressBar(), QLabel())]
            self._update_pager()
            return

        scale = self._scale_max() or 1.0

        start = self._page * self._page_size
        end = min(start + self._page_size, len(self._rows))
        for r, (label, value) in enumerate(self._rows[start:end]):
            name_lbl = QLabel(str(label), self)
            name_lbl.setStyleSheet("color: #e0e0e0; font-size: 10pt;")
            name_lbl.setMinimumWidth(80)

            bar = QProgressBar(self)
            bar.setTextVisible(False)
            bar.setFixedHeight(6)
            bar.setRange(0, 100)
            bar.setValue(int(min(100.0, abs(value) / scale * 100.0)))
            bar_color = (
                _color_win(self) if value > 0
                else (_color_loss(self) if value < 0 else _COLOR_NEUTRAL)
            )
            bar.setStyleSheet(
                f"QProgressBar {{ border: none; background: #2d2d2d; "
                f"border-radius: 2px; }}"
                f"QProgressBar::chunk {{ background: {bar_color}; "
                f"border-radius: 2px; }}"
            )

            pct = self._percent_for(value)
            stat_lbl = QLabel(
                f"{self._fmt(value)}  <span style='color:#808080'>"
                f"{pct:.1f}%</span>",
                self,
            )
            stat_lbl.setTextFormat(Qt.TextFormat.RichText)
            stat_lbl.setAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            )
            stat_lbl.setStyleSheet(
                "color: #e0e0e0; font-size: 10pt;"
            )
            stat_lbl.setMinimumWidth(110)

            self._grid.addWidget(name_lbl, r, 0)
            self._grid.addWidget(bar, r, 1)
            self._grid.addWidget(stat_lbl, r, 2)
            self._row_widgets.append((name_lbl, bar, stat_lbl))

        self._update_pager()

    def _update_pager(self) -> None:
        n = self.page_count()
        if n <= 1:
            self._pager_left.setVisible(False)
            self._pager_right.setVisible(False)
            self._page_label.setVisible(False)
        else:
            self._pager_left.setVisible(True)
            self._pager_right.setVisible(True)
            self._page_label.setVisible(True)
            self._page_label.setText(f"{self._page + 1} / {n}")
            self._pager_left.setEnabled(self._page > 0)
            self._pager_right.setEnabled(self._page < n - 1)


# ---------------------------------------------------------------------------
# BigValueCard — single large dollar number, used for Total Fees
# ---------------------------------------------------------------------------


class BigValueCard(QFrame):
    """One big number — used for Total Fees and similar single-stat
    displays. Lives inside a ``ChartCard``."""

    def __init__(
        self,
        *,
        formatter=_fmt_currency,
        color_by_sign: bool = False,
        parent=None,
    ):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self._fmt = formatter
        self._color_by_sign = color_by_sign

        self._value = QLabel("—", self)
        self._value.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._value.setStyleSheet(
            "color: #e0e0e0; font-size: 28pt; font-weight: bold;"
        )
        self._last_value: Optional[float] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 18, 6, 18)
        layout.setSpacing(4)
        layout.addStretch()
        layout.addWidget(self._value)
        layout.addStretch()
        palette_hub().palette_changed.connect(
            lambda: self.set_value(self._last_value)
        )

    def set_value(self, value: Optional[float]) -> None:
        self._last_value = value
        if value is None:
            self._value.setText("—")
            self._value.setStyleSheet(
                "color: #909090; font-size: 28pt; font-weight: bold;"
            )
            return
        text = self._fmt(value)
        color = "#e0e0e0"
        if self._color_by_sign:
            if value > 0:
                color = _color_win(self)
            elif value < 0:
                color = _color_loss(self)
        self._value.setText(text)
        self._value.setStyleSheet(
            f"color: {color}; font-size: 28pt; font-weight: bold;"
        )


# ---------------------------------------------------------------------------
# ChartCard — title bar (drag-to-move) + content host + size grip
# ---------------------------------------------------------------------------


# Size class names kept as stable strings — used only by the Configure
# Charts dialog defaults and v1→v2 layout migration. Free-canvas mode
# stores absolute (x, y, w, h) per card.
SIZE_SMALL = "small"
SIZE_WIDE = "wide"
SIZE_TALL = "tall"
SIZE_LARGE = "large"

# Pixel dimensions used when seeding a new card or migrating a v1
# size-class layout to the free canvas.
SIZE_DEFAULTS_PX: dict[str, tuple[int, int]] = {
    SIZE_SMALL: (300, 240),
    SIZE_WIDE: (620, 240),
    SIZE_TALL: (300, 460),
    SIZE_LARGE: (620, 460),
}

# Legacy aliases — free-canvas mode doesn't use grid spans, but a few
# tests + imports still reference these. Keep them around as no-op
# constants so downstream code doesn't crash on import.
SIZE_CYCLE: tuple[str, ...] = (SIZE_SMALL, SIZE_WIDE, SIZE_TALL, SIZE_LARGE)
SIZE_SPANS: dict[str, tuple[int, int]] = {
    SIZE_SMALL: (1, 1),
    SIZE_WIDE: (1, 2),
    SIZE_TALL: (2, 1),
    SIZE_LARGE: (2, 2),
}


class _CardResizeGrip(QWidget):
    """Bottom-right corner resize handle that resizes its parent
    ``ChartCard`` only (not the top-level window like ``QSizeGrip``
    does when its parent is a non-window widget).

    Tracks the global mouse position from press → release and applies
    the delta to the card's width/height, clamped to the card's
    minimum size.
    """

    GRIP_PX = 14

    def __init__(self, card: "ChartCard"):
        super().__init__(card)
        self._card = card
        self.setFixedSize(self.GRIP_PX, self.GRIP_PX)
        self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        self._press_global: Optional[QPoint] = None
        self._press_size = None

    def mousePressEvent(self, e: QMouseEvent) -> None:  # type: ignore[override]
        if e.button() == Qt.MouseButton.LeftButton:
            self._press_global = e.globalPosition().toPoint()
            self._press_size = self._card.size()
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e: QMouseEvent) -> None:  # type: ignore[override]
        if (
            self._press_global is not None
            and self._press_size is not None
            and (e.buttons() & Qt.MouseButton.LeftButton)
        ):
            delta = e.globalPosition().toPoint() - self._press_global
            new_w = max(self._card.minimumWidth(), self._press_size.width() + delta.x())
            new_h = max(self._card.minimumHeight(), self._press_size.height() + delta.y())
            canvas = self._card.parent()
            cur_w, cur_h = self._card.width(), self._card.height()
            x, y = self._card.x(), self._card.y()
            tw, th = cur_w, cur_h
            if canvas is not None and hasattr(canvas, "would_collide"):
                if not canvas.would_collide(
                    self._card, QRect(x, y, new_w, new_h)
                ):
                    tw, th = new_w, new_h
                elif not canvas.would_collide(
                    self._card, QRect(x, y, new_w, cur_h)
                ):
                    tw = new_w
                elif not canvas.would_collide(
                    self._card, QRect(x, y, cur_w, new_h)
                ):
                    th = new_h
            else:
                tw, th = new_w, new_h
            if (tw, th) != (cur_w, cur_h):
                self._card.resize(tw, th)
                if canvas is not None and hasattr(canvas, "ensure_room_for"):
                    canvas.ensure_room_for(self._card)
            e.accept()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e: QMouseEvent) -> None:  # type: ignore[override]
        was_dragging = self._press_global is not None
        self._press_global = None
        self._press_size = None
        super().mouseReleaseEvent(e)
        if was_dragging:
            self._card.geometry_changed.emit()
            self._card.layout_changed.emit()

    def paintEvent(self, _e) -> None:  # type: ignore[override]
        # Three diagonal pips so the grip is visible against any
        # card background.
        from PySide6.QtGui import QColor, QPainter, QPen
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        pen = QPen(QColor("#909090"))
        pen.setWidth(1)
        p.setPen(pen)
        w, h = self.width(), self.height()
        for i in (0, 4, 8):
            p.drawLine(i, h - 1, w - 1, i)


class _CardTitleBar(QWidget):
    """Drag-to-move title bar. Holds the title text + minimize button.

    Tracks the global mouse position from press → release so the parent
    card can be repositioned within its canvas (parent widget) on every
    move event.
    """

    def __init__(self, card: "ChartCard"):
        super().__init__(card)
        self._card = card
        self._press_global: Optional[QPoint] = None
        self._press_card_pos: Optional[QPoint] = None
        self.setCursor(Qt.CursorShape.SizeAllCursor)
        self.setObjectName("ChartCardTitleBar")

        self._label = QLabel("", self)
        self._label.setStyleSheet(
            "color: #b0b0b0; font-size: 10pt; font-weight: bold; "
            "background: transparent;"
        )
        self._label.setCursor(Qt.CursorShape.SizeAllCursor)

        self._btn_min = QToolButton(self)
        self._btn_min.setText("—")
        self._btn_min.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_min.setToolTip("Minimize / restore card")
        self._btn_min.setFixedSize(22, 22)
        self._btn_min.clicked.connect(self._card.toggle_minimized)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(self._label, 1)
        layout.addWidget(self._btn_min)

    def set_title(self, text: str) -> None:
        self._label.setText(text)

    def mousePressEvent(self, e: QMouseEvent) -> None:  # type: ignore[override]
        if e.button() == Qt.MouseButton.LeftButton:
            self._press_global = e.globalPosition().toPoint()
            self._press_card_pos = self._card.pos()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e: QMouseEvent) -> None:  # type: ignore[override]
        if (
            self._press_global is not None
            and self._press_card_pos is not None
            and (e.buttons() & Qt.MouseButton.LeftButton)
        ):
            delta = e.globalPosition().toPoint() - self._press_global
            new_x = max(0, self._press_card_pos.x() + delta.x())
            new_y = max(0, self._press_card_pos.y() + delta.y())
            canvas = self._card.parent()
            cur_x, cur_y = self._card.x(), self._card.y()
            w, h = self._card.width(), self._card.height()
            tx, ty = cur_x, cur_y
            if canvas is not None and hasattr(canvas, "would_collide"):
                # Slide-along-walls: try the full diagonal first,
                # then fall back to single-axis moves so the card
                # keeps sliding along an obstacle rather than
                # sticking the moment a corner touches.
                if not canvas.would_collide(
                    self._card, QRect(new_x, new_y, w, h)
                ):
                    tx, ty = new_x, new_y
                elif not canvas.would_collide(
                    self._card, QRect(new_x, cur_y, w, h)
                ):
                    tx = new_x
                elif not canvas.would_collide(
                    self._card, QRect(cur_x, new_y, w, h)
                ):
                    ty = new_y
            else:
                tx, ty = new_x, new_y
            if (tx, ty) != (cur_x, cur_y):
                self._card.move(tx, ty)
                if canvas is not None and hasattr(canvas, "ensure_room_for"):
                    canvas.ensure_room_for(self._card)
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e: QMouseEvent) -> None:  # type: ignore[override]
        was_dragging = self._press_global is not None
        self._press_global = None
        self._press_card_pos = None
        super().mouseReleaseEvent(e)
        if was_dragging:
            # Single signal at end of drag — avoids hammering persistence
            # 60 times a second during a move.
            self._card.geometry_changed.emit()
            self._card.layout_changed.emit()


class ChartCard(QFrame):
    """Free-canvas chart card — drag the title bar to move, drag the
    bottom-right corner grip to resize. Position + size are absolute
    pixel values within the parent canvas widget.

    Emits:
        * ``geometry_changed`` — final pos / size after a drag-resize
          or drag-move ends, and after minimize toggle
        * ``layout_changed`` — back-compat alias of geometry_changed
          (covers minimize + size cycle in legacy callers)
    """

    layout_changed = Signal()
    geometry_changed = Signal()
    swap_requested = Signal(str, str)   # legacy — kept as no-op signal

    DEFAULT_W, DEFAULT_H = SIZE_DEFAULTS_PX[SIZE_SMALL]
    MIN_W, MIN_H = 220, 180
    MIN_H_MINIMIZED = 36

    def __init__(
        self,
        key: str,
        title: str,
        content: QWidget,
        parent=None,
    ):
        super().__init__(parent)
        self.setObjectName("ChartCard")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setMinimumSize(self.MIN_W, self.MIN_H)
        self.resize(self.DEFAULT_W, self.DEFAULT_H)

        self._key = key
        self._title_text = title
        self._size_class = SIZE_SMALL  # legacy field — written by migration only
        self._minimized = False
        self._restore_h: int = self.DEFAULT_H
        self._geom_emit_pending = False
        # Per-card palette override — None means "use the global
        # default". ``chart_palette.get_palette(widget)`` walks up
        # to find this. See ``chart_palette._walk_override``.
        self._palette_override = None

        self._title_bar = _CardTitleBar(self)
        self._title_bar.set_title(title)

        self._content = content

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(10, 8, 10, 14)
        self._layout.setSpacing(6)
        self._layout.addWidget(self._title_bar)
        self._layout.addWidget(self._content, 1)

        # Bottom-right resize grip — custom widget so dragging it
        # resizes only this card. (QSizeGrip walks up to the top-level
        # window when its parent isn't itself a window, which would
        # resize the entire app.)
        self._grip = _CardResizeGrip(self)
        self._grip.raise_()
        self._reposition_grip()

    # ---- public API ---------------------------------------------------

    def key(self) -> str:
        return self._key

    def content(self) -> QWidget:
        return self._content

    def title(self) -> str:
        return self._title_text

    def size_class(self) -> str:
        # Legacy accessor — returns the last-applied preset name, or
        # SIZE_SMALL by default. Free-canvas users should read pixel
        # dimensions via .geometry() instead.
        return self._size_class

    def is_minimized(self) -> bool:
        return self._minimized

    def set_size_class(self, size: str, *, emit: bool = True) -> None:
        """Apply a named preset size in pixels. Used by the v1→v2
        migration path and the Configure Charts dialog."""
        dims = SIZE_DEFAULTS_PX.get(size)
        if dims is None:
            return
        self._size_class = size
        w, h = dims
        self.resize(w, h)
        if emit:
            self.layout_changed.emit()
            self.geometry_changed.emit()

    def cycle_size(self) -> None:
        order = (SIZE_SMALL, SIZE_WIDE, SIZE_TALL, SIZE_LARGE)
        idx = order.index(self._size_class) if self._size_class in order else 0
        self.set_size_class(order[(idx + 1) % len(order)])

    def set_minimized(self, minimized: bool, *, emit: bool = True) -> None:
        if minimized == self._minimized:
            return
        self._minimized = bool(minimized)
        if self._minimized:
            self._restore_h = self.height()
            self._content.setVisible(False)
            self.setMinimumHeight(self.MIN_H_MINIMIZED)
            self.resize(self.width(), self.MIN_H_MINIMIZED)
        else:
            self._content.setVisible(True)
            self.setMinimumHeight(self.MIN_H)
            self.resize(self.width(), max(self.MIN_H, self._restore_h))
        self._title_bar._btn_min.setText("▢" if self._minimized else "—")
        if emit:
            self.layout_changed.emit()
            self.geometry_changed.emit()

    def toggle_minimized(self) -> None:
        self.set_minimized(not self._minimized)

    # ---- per-card palette override ------------------------------------

    def chart_palette_override(self):
        """Return this card's palette override, or ``None`` if it
        inherits from the global default. ``chart_palette.get_palette``
        calls this via walking up the parent chain."""
        return self._palette_override

    def set_chart_palette_override(self, palette) -> None:
        """Set or clear this card's palette override. Pass ``None`` to
        revert to the global default. Emits the global palette-changed
        signal so the inner widget repaints; sibling cards resolve the
        same global and render unchanged."""
        from gui.widgets.chart_palette import palette_hub
        self._palette_override = palette
        self.layout_changed.emit()
        palette_hub().palette_changed.emit()

    # ---- internals ----------------------------------------------------

    def _reposition_grip(self) -> None:
        self._grip.move(
            self.width() - self._grip.width() - 2,
            self.height() - self._grip.height() - 2,
        )

    def resizeEvent(self, e) -> None:  # type: ignore[override]
        super().resizeEvent(e)
        self._reposition_grip()
        # Coalesce — a single grip-drag fires dozens of resize events.
        # Defer to the next event loop tick so we emit once per frame.
        if not self._geom_emit_pending:
            self._geom_emit_pending = True
            QTimer.singleShot(0, self._emit_geom_deferred)

    def _emit_geom_deferred(self) -> None:
        self._geom_emit_pending = False
        # Defensive: if the card's underlying C++ object has been
        # destroyed (e.g. window state change reparenting), skip the
        # emit. shiboken6.isValid would be ideal but isn't always
        # importable; a duck-typed check on parent() is good enough.
        try:
            self.parent()  # raises RuntimeError on dead C++ object
        except RuntimeError:
            return
        self.geometry_changed.emit()
        self.layout_changed.emit()

    # ---- right-click ---------------------------------------------------

    def contextMenuEvent(self, e) -> None:  # type: ignore[override]
        menu = QMenu(self)
        act_colors = QAction("Customize chart colors…", menu)
        act_colors.triggered.connect(self._open_palette_dialog)
        menu.addAction(act_colors)
        menu.exec(e.globalPos())

    def _open_palette_dialog(self) -> None:
        from gui.dialogs.chart_palette import ChartPaletteDialog
        ChartPaletteDialog(card=self, parent=self).exec()


# ---------------------------------------------------------------------------
# ChartCanvas — free-positioning host for ChartCards
# ---------------------------------------------------------------------------


class ChartCanvas(QWidget):
    """A no-layout container that hosts ``ChartCard`` widgets at
    arbitrary (x, y, w, h) positions.

    The canvas grows automatically when a card is dragged or resized
    past its current bounds (so the wrapping QScrollArea reveals
    scrollbars). It never shrinks below the visible area of its
    parent — that's the job of the explicit "Fit to window" action.
    """

    DEFAULT_W = 1200
    DEFAULT_H = 800
    EDGE_MARGIN = 12

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(self.DEFAULT_W, self.DEFAULT_H)

    def cards(self) -> list[ChartCard]:
        return [c for c in self.children() if isinstance(c, ChartCard)]

    def ensure_room_for(self, card: ChartCard) -> None:
        right = card.x() + card.width() + self.EDGE_MARGIN
        bottom = card.y() + card.height() + self.EDGE_MARGIN
        new_w = max(self.minimumWidth(), right)
        new_h = max(self.minimumHeight(), bottom)
        if new_w != self.minimumWidth() or new_h != self.minimumHeight():
            self.setMinimumSize(new_w, new_h)
            self.resize(new_w, new_h)

    def would_collide(self, ignore_card: ChartCard, rect: QRect) -> bool:
        """True if ``rect`` (in canvas coords) overlaps any visible
        sibling card other than ``ignore_card``."""
        for c in self.cards():
            if c is ignore_card or c.isHidden():
                continue
            if c.geometry().intersects(rect):
                return True
        return False

    def content_bounds(self) -> tuple[int, int]:
        """Smallest (w, h) that still contains every visible card."""
        cards = [c for c in self.cards() if c.isVisible()]
        if not cards:
            return (self.DEFAULT_W, self.DEFAULT_H)
        max_x = max(c.x() + c.width() for c in cards)
        max_y = max(c.y() + c.height() for c in cards)
        return (max_x + self.EDGE_MARGIN, max_y + self.EDGE_MARGIN)
