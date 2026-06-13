"""Calendar tab — Mon–Fri month-grid heatmap with side panel.

Top bar: prev / month combo / year combo / next / today / go-to-date /
cell-content mode dropdown.

Layout: QSplitter — calendar grid (3) | side panel listing the selected
day's trades (1).

Single click on a day cell highlights it and updates the side panel.
Double click emits `day_drilldown(date)` which MainWindow handles by
switching to the Trades tab and applying a date filter.

Persists selected month + cell-content mode in QSettings:
    calendar/year, calendar/month, calendar/cell_mode
"""
from __future__ import annotations

import sqlite3
from datetime import date
from typing import Callable, Optional

from PySide6.QtCore import QSettings, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QHBoxLayout, QHeaderView, QLabel,
    QPushButton, QSplitter, QTableView, QVBoxLayout, QWidget,
)

from analytics.calendar_data import (
    CalendarMonth, build_month, shift_month, trades_on_day,
)
from analytics.metrics import compute_metrics
from gui.dialogs.date_jump import DateJumpDialog
from gui.settings_keys import (
    CALENDAR_CELL_MODE as SETTINGS_MODE_KEY,
    CALENDAR_MONTH as SETTINGS_MONTH_KEY,
    CALENDAR_YEAR as SETTINGS_YEAR_KEY,
)
from gui.widgets.calendar_grid import (
    MODE_PNL_COUNT, MODE_PNL_COUNT_WL, MODE_PNL_ONLY, CalendarGrid,
)
from ingest import db_manager
from models.table_model import TradeTableModel

MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

CELL_MODE_OPTIONS: list[tuple[str, str]] = [
    (MODE_PNL_ONLY, "P&L only"),
    (MODE_PNL_COUNT, "P&L + count"),
    (MODE_PNL_COUNT_WL, "P&L + count + W/L"),
]

YEAR_RANGE_PADDING = 5  # years before earliest / after latest trade


class CalendarTab(QWidget):
    # Emitted on double-click of a day cell. MainWindow wires this to the
    # Trades tab so the user is jumped over with that day pre-filtered.
    day_drilldown = Signal(object)  # date

    def __init__(
        self,
        get_conn: Callable,
        settings: Optional[QSettings] = None,
        parent=None,
    ):
        super().__init__(parent)
        self._get_conn = get_conn
        self._settings = settings

        today = date.today()
        self._year = today.year
        self._month = today.month
        self._cell_mode = MODE_PNL_ONLY
        # Cached closed-trade list populated by refresh(). Reused by nav
        # handlers (prev/next) and day-click so month navigation doesn't
        # re-query the DB on every arrow click.
        self._closed_cache: list[dict] = []
        self._restore_settings()

        # --- top bar --------------------------------------------------------
        self.btn_prev = QPushButton("◀", self)
        self.btn_prev.setFixedWidth(32)
        self.btn_prev.clicked.connect(self._prev_month)

        self.btn_next = QPushButton("▶", self)
        self.btn_next.setFixedWidth(32)
        self.btn_next.clicked.connect(self._next_month)

        self.combo_month = QComboBox(self)
        for i, name in enumerate(MONTH_NAMES, start=1):
            self.combo_month.addItem(name, i)
        self.combo_month.currentIndexChanged.connect(self._on_month_combo_changed)

        self.combo_year = QComboBox(self)
        self._populate_year_combo()
        self.combo_year.currentIndexChanged.connect(self._on_year_combo_changed)

        self.btn_today = QPushButton("Today", self)
        self.btn_today.clicked.connect(self._goto_today)

        self.btn_goto = QPushButton("Go to date…", self)
        self.btn_goto.clicked.connect(self._on_goto_date)

        self.combo_mode = QComboBox(self)
        for key, label in CELL_MODE_OPTIONS:
            self.combo_mode.addItem(label, key)
        self.combo_mode.currentIndexChanged.connect(self._on_mode_changed)

        top_bar = QHBoxLayout()
        top_bar.setSpacing(6)
        top_bar.addWidget(self.btn_prev)
        top_bar.addWidget(self.combo_month)
        top_bar.addWidget(self.combo_year)
        top_bar.addWidget(self.btn_next)
        top_bar.addSpacing(8)
        top_bar.addWidget(self.btn_today)
        top_bar.addWidget(self.btn_goto)
        top_bar.addStretch(1)
        top_bar.addWidget(QLabel("Cell:", self))
        top_bar.addWidget(self.combo_mode)

        # --- grid + side panel ---------------------------------------------
        self.grid = CalendarGrid(self)
        self.grid.day_clicked.connect(self._on_day_clicked)
        self.grid.day_double_clicked.connect(self._on_day_double_clicked)
        self.grid.set_cell_mode(self._cell_mode)

        # Side panel: title + month summary + trades-of-day mini table
        self.side_panel = QWidget(self)
        side_layout = QVBoxLayout(self.side_panel)
        side_layout.setContentsMargins(8, 8, 8, 8)
        side_layout.setSpacing(6)

        self.lbl_month_summary = QLabel("", self.side_panel)
        self.lbl_month_summary.setStyleSheet(
            "color: #b0b0b0; font-size: 10pt;"
        )
        self.lbl_month_summary.setWordWrap(True)

        self.lbl_day_title = QLabel("Click a day to see trades.", self.side_panel)
        self.lbl_day_title.setStyleSheet(
            "color: #e0e0e0; font-size: 11pt; font-weight: bold;"
        )
        self.lbl_day_title.setWordWrap(True)

        self.lbl_day_summary = QLabel("", self.side_panel)
        self.lbl_day_summary.setStyleSheet("color: #b0b0b0; font-size: 9pt;")
        self.lbl_day_summary.setWordWrap(True)

        self.day_model = TradeTableModel()
        self.day_table = QTableView(self.side_panel)
        self.day_table.setModel(self.day_model)
        self.day_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.day_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.day_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.day_table.setAlternatingRowColors(True)
        self.day_table.verticalHeader().setVisible(False)
        h = self.day_table.horizontalHeader()
        h.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        h.setStretchLastSection(True)
        # Hide columns we don't need in the compact day view: Gross, Commission.
        for col_key in ("gross_pnl", "total_commission"):
            for i, (k, _) in enumerate(TradeTableModel.COLUMNS):
                if k == col_key:
                    self.day_table.setColumnHidden(i, True)
                    break

        side_layout.addWidget(self.lbl_month_summary)
        side_layout.addSpacing(4)
        side_layout.addWidget(self.lbl_day_title)
        side_layout.addWidget(self.lbl_day_summary)
        side_layout.addWidget(self.day_table, 1)

        # Splitter
        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.addWidget(self.grid)
        self.splitter.addWidget(self.side_panel)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([900, 300])

        main = QVBoxLayout(self)
        main.setContentsMargins(8, 8, 8, 8)
        main.setSpacing(6)
        main.addLayout(top_bar)
        main.addWidget(self.splitter, 1)

        # Sync widgets to current state and load data.
        self._sync_combos()
        self._set_mode_combo()
        self.refresh()

    # ---- public API --------------------------------------------------------

    def refresh(
        self, preloaded_closed: Optional[list[dict]] = None,
    ) -> None:
        if preloaded_closed is not None:
            self._closed_cache = preloaded_closed
        else:
            self._closed_cache = db_manager.fetch_closed_trades(
                self._get_conn(),
            )
        cm = build_month(self._closed_cache, self._year, self._month)
        self.grid.set_month(cm)
        self._update_month_summary(cm)
        # Re-render the side panel against currently selected day, if any.
        sel = self.grid.selected_day()
        if sel and sel.year == self._year and sel.month == self._month:
            self._show_day(sel, self._closed_cache)
        else:
            self._clear_day_panel()

    def goto_date(self, d: date) -> None:
        """Navigate to the month containing `d` and select that day."""
        self._year = d.year
        self._month = d.month
        # Nav doesn't need to re-query the year range from the DB — it
        # only changes when trades are added/removed, which triggers a
        # full refresh. Just sync the combo widgets to the new month.
        self._sync_combos()
        self._save_settings()
        # Rebuild the grid against the already-cached trade list.
        cm = build_month(self._closed_cache, self._year, self._month)
        self.grid.set_month(cm)
        self._update_month_summary(cm)
        self.grid.set_selected_day(d)
        self._show_day(d, self._closed_cache)

    # ---- internals ---------------------------------------------------------

    def _populate_year_combo(self) -> None:
        """Refresh the year combo to span trade history ± padding."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT MIN(strftime('%Y', exit_time)), "
                "       MAX(strftime('%Y', exit_time)) FROM trades"
            ).fetchone()
        except (sqlite3.Error, TypeError):
            row = (None, None)
        today = date.today()
        years = {today.year, self._year}
        if row and row[0] and row[1]:
            try:
                years.add(int(row[0]))
                years.add(int(row[1]))
            except ValueError:
                pass
        lo = min(years) - YEAR_RANGE_PADDING
        hi = max(years) + YEAR_RANGE_PADDING

        self.combo_year.blockSignals(True)
        self.combo_year.clear()
        for y in range(lo, hi + 1):
            self.combo_year.addItem(str(y), y)
        self.combo_year.blockSignals(False)

    def _sync_combos(self) -> None:
        self.combo_month.blockSignals(True)
        self.combo_month.setCurrentIndex(self._month - 1)
        self.combo_month.blockSignals(False)

        self.combo_year.blockSignals(True)
        idx = self.combo_year.findData(self._year)
        if idx >= 0:
            self.combo_year.setCurrentIndex(idx)
        self.combo_year.blockSignals(False)

    def _set_mode_combo(self) -> None:
        self.combo_mode.blockSignals(True)
        idx = self.combo_mode.findData(self._cell_mode)
        if idx >= 0:
            self.combo_mode.setCurrentIndex(idx)
        self.combo_mode.blockSignals(False)

    # ---- nav handlers ------------------------------------------------------

    def _prev_month(self) -> None:
        self._year, self._month = shift_month(self._year, self._month, -1)
        self._sync_combos()
        self._save_settings()
        self._rerender_month()

    def _next_month(self) -> None:
        self._year, self._month = shift_month(self._year, self._month, +1)
        self._sync_combos()
        self._save_settings()
        self._rerender_month()

    def _rerender_month(self) -> None:
        """Redraw the grid against the cached trade list — no DB read.

        Nav clicks (prev/next/today/goto-month) don't change the trade
        universe, so there's no reason to re-query every time.
        """
        cm = build_month(self._closed_cache, self._year, self._month)
        self.grid.set_month(cm)
        self._update_month_summary(cm)
        sel = self.grid.selected_day()
        if sel and sel.year == self._year and sel.month == self._month:
            self._show_day(sel, self._closed_cache)
        else:
            self._clear_day_panel()

    def _goto_today(self) -> None:
        self.goto_date(date.today())

    def _on_goto_date(self) -> None:
        dlg = DateJumpDialog(date(self._year, self._month, 1), self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.goto_date(dlg.selected_date())

    def _on_month_combo_changed(self, idx: int) -> None:
        if idx < 0:
            return
        self._month = idx + 1
        self._save_settings()
        self.refresh()

    def _on_year_combo_changed(self, idx: int) -> None:
        if idx < 0:
            return
        y = self.combo_year.itemData(idx)
        if isinstance(y, int):
            self._year = y
            self._save_settings()
            self.refresh()

    def _on_mode_changed(self, idx: int) -> None:
        if idx < 0:
            return
        mode = self.combo_mode.itemData(idx)
        if isinstance(mode, str):
            self._cell_mode = mode
            self.grid.set_cell_mode(mode)
            self._save_settings()

    # ---- day click handlers ------------------------------------------------

    def _on_day_clicked(self, d: date) -> None:
        self._show_day(d, self._closed_cache)

    def _on_day_double_clicked(self, d: date) -> None:
        self.day_drilldown.emit(d)

    # ---- side panel rendering ---------------------------------------------

    def _update_month_summary(self, cm: CalendarMonth) -> None:
        if cm.month_trade_count == 0:
            self.lbl_month_summary.setText(
                f"{MONTH_NAMES[cm.month - 1]} {cm.year}: no closed trades."
            )
            return
        sign = "+" if cm.month_net >= 0 else "−"
        self.lbl_month_summary.setText(
            f"{MONTH_NAMES[cm.month - 1]} {cm.year}:  "
            f"{sign}${abs(cm.month_net):,.2f}  ·  "
            f"{cm.month_trade_count} trade"
            + ("" if cm.month_trade_count == 1 else "s")
        )

    def _clear_day_panel(self) -> None:
        self.lbl_day_title.setText("Click a day to see trades.")
        self.lbl_day_summary.setText("")
        self.day_model.set_trades([])

    def _show_day(self, d: date, all_trades: list[dict]) -> None:
        day_trades = trades_on_day(all_trades, d)
        self.lbl_day_title.setText(d.strftime("%A, %b %d, %Y"))
        if not day_trades:
            self.lbl_day_summary.setText("No trades on this day.")
            self.day_model.set_trades([])
            return
        m = compute_metrics(day_trades)
        sign = "+" if m.total_pnl >= 0 else "−"
        self.lbl_day_summary.setText(
            f"{sign}${abs(m.total_pnl):,.2f}  ·  "
            f"{m.total_trades} trade"
            + ("" if m.total_trades == 1 else "s")
            + f"  ·  {m.win_count}W / {m.loss_count}L"
        )
        self.day_model.set_trades(day_trades)

    # ---- settings ----------------------------------------------------------

    def _restore_settings(self) -> None:
        if self._settings is None:
            return
        try:
            y = int(self._settings.value(SETTINGS_YEAR_KEY, self._year))
            m = int(self._settings.value(SETTINGS_MONTH_KEY, self._month))
            if 1 <= m <= 12:
                self._year, self._month = y, m
        except (TypeError, ValueError):
            pass
        mode = self._settings.value(SETTINGS_MODE_KEY)
        if isinstance(mode, str) and mode in (
            MODE_PNL_ONLY, MODE_PNL_COUNT, MODE_PNL_COUNT_WL,
        ):
            self._cell_mode = mode

    def _save_settings(self) -> None:
        if self._settings is None:
            return
        self._settings.setValue(SETTINGS_YEAR_KEY, self._year)
        self._settings.setValue(SETTINGS_MONTH_KEY, self._month)
        self._settings.setValue(SETTINGS_MODE_KEY, self._cell_mode)
