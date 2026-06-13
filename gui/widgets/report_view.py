"""Generic report view: title + filterable table + bar chart + export buttons.

A `ReportView` accepts a `Report` (from analytics.reports) via `set_report`
and renders it as:
    - title label
    - QTableView backed by ReportTableModel (formatted + colored cells)
    - optional CategoryBarChart below the table (when chart_kind == "bar")
    - Copy / Export-CSV buttons in the top-right corner

The widget is reused for every report sub-tab.
"""
from __future__ import annotations

import csv
import io
import math
from typing import Optional

import pyqtgraph as pg
from PySide6.QtCore import (
    QAbstractTableModel, QModelIndex, Qt, Signal,
)
from PySide6.QtGui import QBrush, QColor, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView, QFileDialog, QHBoxLayout, QHeaderView, QLabel,
    QMessageBox, QPushButton, QStackedWidget, QTableView, QVBoxLayout, QWidget,
)

from analytics.reports import Report, ReportColumn
from gui.widgets.composed_charts import CategoryBars

# Theme colors (mirrors charts.py).
COLOR_BG = "#1e1e1e"
COLOR_FG = "#e0e0e0"
COLOR_MUTED = "#b0b0b0"
COLOR_GRID = "#3c3c3c"
COLOR_WIN = "#00C853"
COLOR_LOSS = "#FF1744"

_QCOLOR_WIN = QColor(COLOR_WIN)
_QCOLOR_LOSS = QColor(COLOR_LOSS)


# ---- formatting ------------------------------------------------------------


def _fmt_currency(v: float) -> str:
    if v is None:
        return "—"
    if v == 0:
        return "$0.00"
    sign = "-" if v < 0 else ""
    return f"{sign}${abs(v):,.2f}"


def _fmt_pct(v: float) -> str:
    if v is None:
        return "—"
    return f"{v * 100:.1f}%"


def _fmt_int(v) -> str:
    if v is None:
        return "—"
    return f"{int(v):,}"


def _fmt_float(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, float) and math.isinf(v):
        return "∞"
    return f"{float(v):.2f}"


def _fmt_duration(v) -> str:
    if v is None or v == 0:
        return "—"
    secs = int(v)
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs / 3600:.1f}h"
    return f"{secs / 86400:.1f}d"


_FORMATTERS = {
    "str": lambda v: "" if v is None else str(v),
    "currency": _fmt_currency,
    "pct": _fmt_pct,
    "int": _fmt_int,
    "float": _fmt_float,
    "duration": _fmt_duration,
}


def _align_flag(align: str) -> int:
    if align == "right":
        return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    if align == "center":
        return int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)
    return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)


# ---- table model -----------------------------------------------------------


class ReportTableModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._columns: list[ReportColumn] = []
        self._rows: list[dict] = []

    def set_report(self, report: Report) -> None:
        self.beginResetModel()
        self._columns = list(report.columns)
        self._rows = list(report.rows)
        self.endResetModel()

    # Qt API ----------------------------------------------------------------

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        if parent.isValid():
            return 0
        return len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        if parent.isValid():
            return 0
        return len(self._columns)

    def headerData(self, section: int, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            if 0 <= section < len(self._columns):
                return self._columns[section].label
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        col = self._columns[index.column()]
        raw = row.get(col.key)

        if role == Qt.ItemDataRole.DisplayRole:
            fmt = _FORMATTERS.get(col.fmt, _FORMATTERS["str"])
            return fmt(raw)

        if role == Qt.ItemDataRole.TextAlignmentRole:
            return _align_flag(col.align)

        if role == Qt.ItemDataRole.ForegroundRole and col.color_by_sign:
            try:
                v = float(raw)
            except (TypeError, ValueError):
                return None
            if v > 0:
                return QBrush(_QCOLOR_WIN)
            if v < 0:
                return QBrush(_QCOLOR_LOSS)
        return None


# ---- chart -----------------------------------------------------------------


class _CategoryAxis(pg.AxisItem):
    """Integer-positioned ticks rendered as the row labels."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._labels: list[str] = []

    def set_labels(self, labels) -> None:
        self._labels = list(labels)
        self.picture = None

    def tickValues(self, minVal, maxVal, size):  # noqa: N802
        n = len(self._labels)
        if n == 0:
            return []
        lo = max(0, int(minVal) if minVal > 0 else 0)
        hi = min(n - 1, int(maxVal) + 1)
        candidates = list(range(lo, hi + 1))
        # Cap at ~12 visible labels
        max_ticks = 12
        if len(candidates) > max_ticks:
            step = max(1, len(candidates) // max_ticks)
            candidates = candidates[::step]
        return [(1.0, [float(c) for c in candidates])]

    def tickStrings(self, values, scale, spacing):  # noqa: N802
        out = []
        for v in values:
            i = int(round(v))
            if 0 <= i < len(self._labels):
                out.append(self._labels[i])
            else:
                out.append("")
        return out


class CategoryBarChart(pg.PlotWidget):
    """Generic category-bar chart used by every report's chart panel.

    Bars colored green for nonneg values, red for negative.
    """

    def __init__(self, parent=None):
        self._x_axis = _CategoryAxis(orientation="bottom")
        super().__init__(parent=parent, axisItems={"bottom": self._x_axis})
        self.setBackground(COLOR_BG)
        self.showGrid(x=False, y=True, alpha=0.2)
        self.setLabel("left", "Net P&L ($)", color=COLOR_MUTED)
        self.setMenuEnabled(False)
        self.setMouseEnabled(x=False, y=False)
        self._theme_axes()

    def _theme_axes(self) -> None:
        for side in ("left", "bottom"):
            ax = self.getAxis(side)
            ax.setTextPen(COLOR_MUTED)
            ax.setPen(COLOR_GRID)

    def set_data(self, report: Report) -> None:
        self.clear()
        if (
            report.chart_kind != "bar"
            or not report.rows
            or not report.chart_x_key
            or not report.chart_y_key
        ):
            self._x_axis.set_labels([])
            return

        labels = [str(r.get(report.chart_x_key, "")) for r in report.rows]
        values: list[float] = []
        for r in report.rows:
            try:
                values.append(float(r.get(report.chart_y_key) or 0.0))
            except (TypeError, ValueError):
                values.append(0.0)

        self._x_axis.set_labels(labels)
        if report.chart_title:
            self.setTitle(report.chart_title, color=COLOR_FG, size="10pt")
        else:
            self.setTitle("")

        green_x, green_h, red_x, red_h = [], [], [], []
        for i, h in enumerate(values):
            if h >= 0:
                green_x.append(i)
                green_h.append(h)
            else:
                red_x.append(i)
                red_h.append(h)

        n = len(values)
        bar_width = 0.5 if n >= 6 else 0.35

        if green_x:
            self.addItem(pg.BarGraphItem(
                x=green_x, height=green_h, width=bar_width,
                brush=COLOR_WIN, pen=None,
            ))
        if red_x:
            self.addItem(pg.BarGraphItem(
                x=red_x, height=red_h, width=bar_width,
                brush=COLOR_LOSS, pen=None,
            ))

        self.addLine(y=0, pen=pg.mkPen(color=COLOR_GRID, width=1))
        self.setXRange(-0.75, n - 0.25, padding=0)


# ---- view widget -----------------------------------------------------------


class ReportView(QWidget):
    """Title + (table + optional chart) + export buttons.

    `set_report` is the only entry point.
    """

    export_requested = Signal(str)  # path

    # Persisted view-mode key so the Table/Chart toggle restores
    # across launches.
    SETTINGS_VIEW_MODE = "reports/view_mode"

    VIEW_TABLE = "table"
    VIEW_CHART = "chart"

    def __init__(self, parent=None):
        super().__init__(parent)
        self._report: Optional[Report] = None

        # ---- header --------------------------------------------------------
        self.title_label = QLabel("", self)
        self.title_label.setObjectName("sectionTitle")

        # Table | Chart view-mode toggle. Two checkable buttons in
        # auto-exclusive mode behave like a tasteful radio pair on
        # the dark theme.
        self.btn_view_table = QPushButton("Table", self)
        self.btn_view_table.setCheckable(True)
        self.btn_view_table.setAutoExclusive(True)
        self.btn_view_table.setChecked(True)
        self.btn_view_table.setToolTip(
            "Show the report as a sortable data table"
        )
        self.btn_view_table.clicked.connect(
            lambda: self._set_view_mode(self.VIEW_TABLE),
        )

        self.btn_view_chart = QPushButton("Chart", self)
        self.btn_view_chart.setCheckable(True)
        self.btn_view_chart.setAutoExclusive(True)
        self.btn_view_chart.setToolTip(
            "Show the report as horizontal category bars"
        )
        self.btn_view_chart.clicked.connect(
            lambda: self._set_view_mode(self.VIEW_CHART),
        )

        self.btn_copy = QPushButton("Copy", self)
        self.btn_copy.clicked.connect(self._on_copy)

        self.btn_csv = QPushButton("Export CSV", self)
        self.btn_csv.clicked.connect(self._on_export_csv)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.addWidget(self.title_label, 1)
        header.addWidget(self.btn_view_table)
        header.addWidget(self.btn_view_chart)
        header.addSpacing(8)
        header.addWidget(self.btn_copy)
        header.addWidget(self.btn_csv)

        # ---- table ---------------------------------------------------------
        self.model = ReportTableModel(self)
        self.table = QTableView(self)
        self.table.setModel(self.model)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        h.setStretchLastSection(True)

        # ---- chart (new dashboard-style horizontal category bars) -------
        # Larger page size than dashboard cards — there's room here.
        self.chart_view = CategoryBars(page_size=14, pct_basis="abs_total")

        # ---- empty state ---------------------------------------------------
        self.empty_label = QLabel("No data.", self)
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setObjectName("emptyState")

        # The populated stack hosts both views — a child stack flips
        # between them when the user clicks the toggle.
        self.populated = QWidget(self)
        self.view_stack = QStackedWidget(self.populated)
        self.view_stack.addWidget(self.table)        # index 0 (table view)
        self.view_stack.addWidget(self.chart_view)   # index 1 (chart view)
        pop_layout = QVBoxLayout(self.populated)
        pop_layout.setContentsMargins(0, 0, 0, 0)
        pop_layout.setSpacing(8)
        pop_layout.addWidget(self.view_stack, 1)

        self.stack = QStackedWidget(self)
        self.stack.addWidget(self.populated)   # index 0
        self.stack.addWidget(self.empty_label)  # index 1

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(8)
        outer.addLayout(header)
        outer.addWidget(self.stack, 1)

        # Restore view-mode preference from the global settings store.
        self._restore_view_mode()

    # ---- public API --------------------------------------------------------

    def set_report(self, report: Report) -> None:
        self._report = report
        self.title_label.setText(report.title)
        self.model.set_report(report)
        # Feed the chart-view widget the same rows. Each row's "label"
        # is the category and the report's chart_y_key (default
        # ``net_pnl``) is the value.
        self._refresh_chart_view(report)

        if not report.rows:
            self.empty_label.setText(report.empty_message or "No data.")
            self.stack.setCurrentIndex(1)
            self.btn_copy.setEnabled(False)
            self.btn_csv.setEnabled(False)
            self.btn_view_table.setEnabled(False)
            self.btn_view_chart.setEnabled(False)
            return

        # Reports without bar-chart semantics (e.g. drawdown summary)
        # disable the Chart toggle and force back to Table.
        chartable = report.chart_kind == "bar"
        self.btn_view_chart.setEnabled(chartable)
        if not chartable and self.view_stack.currentIndex() == 1:
            self._set_view_mode(self.VIEW_TABLE)

        self.stack.setCurrentIndex(0)
        self.btn_copy.setEnabled(True)
        self.btn_csv.setEnabled(True)
        self.btn_view_table.setEnabled(True)

    def _refresh_chart_view(self, report: Report) -> None:
        """Populate the CategoryBars chart from the report's rows."""
        y_key = report.chart_y_key or "net_pnl"
        rows: list[tuple[str, float]] = []
        for r in report.rows:
            try:
                v = float(r.get(y_key) or 0.0)
            except (TypeError, ValueError):
                continue
            label = str(r.get("label") or r.get(report.chart_x_key or "", "?"))
            rows.append((label, v))
        self.chart_view.set_rows(rows)

    # ---- view-mode toggle ----------------------------------------------

    def _set_view_mode(self, mode: str) -> None:
        if mode == self.VIEW_CHART:
            self.view_stack.setCurrentIndex(1)
            self.btn_view_chart.setChecked(True)
        else:
            self.view_stack.setCurrentIndex(0)
            self.btn_view_table.setChecked(True)
        # Persist via the same QSettings the rest of the app uses.
        self._save_view_mode(mode)

    def _save_view_mode(self, mode: str) -> None:
        from PySide6.QtCore import QSettings
        QSettings("TradeBook", "TradeBook").setValue(
            self.SETTINGS_VIEW_MODE, mode,
        )

    def _restore_view_mode(self) -> None:
        from PySide6.QtCore import QSettings
        saved = QSettings("TradeBook", "TradeBook").value(
            self.SETTINGS_VIEW_MODE, self.VIEW_TABLE,
        )
        if saved == self.VIEW_CHART:
            self.view_stack.setCurrentIndex(1)
            self.btn_view_chart.setChecked(True)
        else:
            self.view_stack.setCurrentIndex(0)
            self.btn_view_table.setChecked(True)

    # ---- export helpers ----------------------------------------------------

    def _rows_as_text(self) -> str:
        if self._report is None or not self._report.rows:
            return ""
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([c.label for c in self._report.columns])
        for r in range(self.model.rowCount()):
            writer.writerow([
                self.model.data(
                    self.model.index(r, c),
                    Qt.ItemDataRole.DisplayRole,
                ) or ""
                for c in range(self.model.columnCount())
            ])
        return buf.getvalue()

    def _on_copy(self) -> None:
        text = self._rows_as_text()
        if not text:
            return
        QGuiApplication.clipboard().setText(text)

    def _on_export_csv(self) -> None:
        if self._report is None or not self._report.rows:
            return
        default_name = (
            self._report.title.lower().replace(" ", "_").replace("&", "and")
            + ".csv"
        )
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Report CSV", default_name, "CSV files (*.csv)"
        )
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8") as fh:
                fh.write(self._rows_as_text())
        except OSError as e:
            QMessageBox.warning(
                self, "Export failed", f"Could not write file:\n{e}"
            )
            return
        self.export_requested.emit(path)
