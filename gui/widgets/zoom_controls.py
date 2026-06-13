"""Zoomable table view + compact +/− control widget.

`ZoomableTableView` is a QTableView that scales its font, rows, and columns
in lockstep. `ZoomControls` is a small horizontal widget (minus button,
percentage label, plus button) that drives a target view.

Both tabs with a table use the pair: Trades tab and the New Trade preview.
Zoom level is a signed integer step (0 = baseline, +n / −n for steps).
Persistence is handled by MainWindow via QSettings, not here.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QHeaderView, QLabel, QPushButton, QTableView, QWidget,
)

_MIN_ZOOM = -4
_MAX_ZOOM = 12
_PT_PER_STEP = 1.0


class ZoomableTableView(QTableView):
    """QTableView with font/row/column scaling under a single zoom level.

    Row height is driven by the vertical header's ResizeToContents mode so
    it grows naturally with the font.
    """

    zoom_changed = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        pt = self.font().pointSizeF()
        self._base_pt: float = pt if pt > 0 else 10.0
        self._zoom_level: int = 0

        # Let rows auto-size to content (font) even with a hidden vertical header.
        self.verticalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )

    # ---- public API --------------------------------------------------------

    def zoom_level(self) -> int:
        return self._zoom_level

    def base_point_size(self) -> float:
        return self._base_pt

    def zoom_in(self) -> None:
        self.set_zoom_level(self._zoom_level + 1)

    def zoom_out(self) -> None:
        self.set_zoom_level(self._zoom_level - 1)

    def zoom_reset(self) -> None:
        self.set_zoom_level(0)

    def set_zoom_level(self, level: int) -> None:
        level = max(_MIN_ZOOM, min(_MAX_ZOOM, int(level)))
        if level == self._zoom_level:
            return
        self._zoom_level = level
        self._apply_zoom()
        self.zoom_changed.emit(level)

    # ---- internals ---------------------------------------------------------

    def _apply_zoom(self) -> None:
        pt = self._base_pt + self._zoom_level * _PT_PER_STEP

        f = self.font()
        f.setPointSizeF(pt)
        self.setFont(f)

        hf = self.horizontalHeader().font()
        hf.setPointSizeF(pt)
        self.horizontalHeader().setFont(hf)

        self.resizeColumnsToContents()
        self.resizeRowsToContents()


class ZoomControls(QWidget):
    """Compact toolbar: [ Size:  −   100%   + ] driving a ZoomableTableView."""

    def __init__(self, view: ZoomableTableView, parent=None):
        super().__init__(parent)
        self._view = view

        self.btn_minus = QPushButton("−", self)
        self.btn_plus = QPushButton("+", self)
        for b in (self.btn_minus, self.btn_plus):
            b.setFixedWidth(28)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_minus.setToolTip("Decrease font and column size")
        self.btn_plus.setToolTip("Increase font and column size")

        self.label = QLabel("100%", self)
        self.label.setMinimumWidth(44)
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(QLabel("Size:", self))
        layout.addWidget(self.btn_minus)
        layout.addWidget(self.label)
        layout.addWidget(self.btn_plus)
        layout.addStretch()

        self.btn_minus.clicked.connect(view.zoom_out)
        self.btn_plus.clicked.connect(view.zoom_in)
        view.zoom_changed.connect(self._on_zoom_changed)

        self._on_zoom_changed(view.zoom_level())

    def _on_zoom_changed(self, level: int) -> None:
        base = self._view.base_point_size()
        pt = base + level * _PT_PER_STEP
        pct = int(round(100 * pt / base))
        self.label.setText(f"{pct}%")
        self.btn_minus.setEnabled(level > _MIN_ZOOM)
        self.btn_plus.setEnabled(level < _MAX_ZOOM)
