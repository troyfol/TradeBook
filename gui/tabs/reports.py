"""Reports tab — filter bar + 8 report sub-tabs.

Sub-tabs:
    By Symbol | By Direction | By Day of Week | By Hour of Day |
    By Hold Time | By Entry Price | Streaks | Drawdown

The active filter spec is shared across every sub-tab; switching tabs
re-renders the previously cached filtered trade list, so toggling tabs
is instantaneous after the initial load.

Bucket-based reports (Entry Price + Hold Time) support user-overridable
bin edges via a Configure Bins button on the top bar. The button label
and enabled state track the active sub-tab.
"""
from __future__ import annotations

import math
from typing import Callable, Optional

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QHBoxLayout, QPushButton, QTabWidget,
    QVBoxLayout, QWidget,
)

from analytics import (
    drawdown, r_multiple, reports as report_mod, streaks, time_analysis,
)
from analytics.reports import (
    DEFAULT_PRICE_EDGES, FilterSpec, apply_filters,
)
from analytics.time_analysis import DEFAULT_HOLD_EDGES_MINUTES
from gui.dialogs.bin_config import (
    BinConfigDialog, KIND_HOLD_MINUTES, KIND_PRICE, format_edges, parse_edges,
)
from gui.dialogs.r_multiple_settings import load_default_risk
from gui.settings_keys import (
    REPORTS_ACTIVE_SUBTAB as SETTINGS_ACTIVE_TAB,
    REPORTS_HOLD_EDGES_MINUTES as SETTINGS_HOLD_EDGES_MINUTES,
    REPORTS_PLANNED_STOPS_ONLY as SETTINGS_PLANNED_STOPS_ONLY,
    REPORTS_PRICE_EDGES as SETTINGS_PRICE_EDGES,
)
from gui.widgets.report_filter_bar import ReportFilterBar
from gui.widgets.report_view import ReportView
from ingest import db_manager

# Sub-tab labels (also used as builder dispatch keys).
LABEL_BY_SYMBOL = "By Symbol"
LABEL_BY_DIRECTION = "By Direction"
LABEL_BY_DOW = "By Day of Week"
LABEL_BY_HOUR = "By Hour of Day"
LABEL_BY_HOLD = "By Hold Time"
LABEL_BY_PRICE = "By Entry Price"
LABEL_STREAKS = "Streaks"
LABEL_DRAWDOWN = "Drawdown"
LABEL_BY_R = "By R-Multiple"

# Build callbacks: (label, fn(trades) -> Report).
REPORT_BUILDERS: list[tuple[str, Callable]] = [
    (LABEL_BY_SYMBOL, report_mod.by_symbol),
    (LABEL_BY_DIRECTION, report_mod.by_direction),
    (LABEL_BY_DOW, time_analysis.by_day_of_week),
    (LABEL_BY_HOUR, time_analysis.by_hour_of_day),
    (LABEL_BY_HOLD, time_analysis.by_hold_time),
    (LABEL_BY_PRICE, report_mod.by_ticker_price),
    (LABEL_BY_R, r_multiple.by_r_bucket),
    (LABEL_STREAKS, streaks.by_streaks),
    (LABEL_DRAWDOWN, drawdown.by_drawdown),
]


class ReportsTab(QWidget):
    def __init__(
        self,
        get_conn: Callable,
        settings: Optional[QSettings] = None,
        parent=None,
    ):
        super().__init__(parent)
        self._get_conn = get_conn
        self._settings = settings
        self._filtered_cache: list[dict] = []
        # Cache of the full closed-trade list. Populated by `refresh` and
        # reused by `_on_filters_changed` so filter toggles don't re-hit
        # the DB on every click.
        self._all_closed: list[dict] = []

        # Persisted bin overrides.
        self._price_edges: list[float] = self._load_edges(
            SETTINGS_PRICE_EDGES, DEFAULT_PRICE_EDGES,
        )
        self._hold_edges_minutes: list[float] = self._load_edges(
            SETTINGS_HOLD_EDGES_MINUTES, DEFAULT_HOLD_EDGES_MINUTES,
        )

        # ---- top bar (filter + bins button) -------------------------------
        self.filter_bar = ReportFilterBar(self)
        self.filter_bar.filters_changed.connect(self._on_filters_changed)

        self.btn_bins = QPushButton("Configure Bins…", self)
        self.btn_bins.clicked.connect(self._on_configure_bins)
        self.btn_bins.setToolTip(
            "Set custom bin edges for the active bucket-based report."
        )

        # Only meaningful on the R-Multiple report — apply_import_time_risk
        # back-fills a stop for every loser from its own realized loss, so
        # those trades are -1R by construction. Ticking this drops them and
        # leaves only stops the user actually planned.
        self.chk_planned_stops = QCheckBox("Planned stops only", self)
        self.chk_planned_stops.setToolTip(
            "Exclude trades whose stop was auto-filled at import from the "
            "trade's own realized loss.\nThose are always exactly -1R, so "
            "including them makes the R distribution look tighter than it is."
        )
        self.chk_planned_stops.setChecked(
            self._load_planned_stops_only()
        )
        self.chk_planned_stops.toggled.connect(self._on_planned_stops_toggled)
        self.chk_planned_stops.setVisible(False)

        bins_row = QHBoxLayout()
        bins_row.setContentsMargins(0, 0, 0, 0)
        bins_row.addStretch()
        bins_row.addWidget(self.chk_planned_stops)
        bins_row.addWidget(self.btn_bins)

        # ---- sub-tabs ------------------------------------------------------
        self.subtabs = QTabWidget(self)
        self._views: list[ReportView] = []
        for label, _ in REPORT_BUILDERS:
            view = ReportView(self.subtabs)
            self.subtabs.addTab(view, label)
            self._views.append(view)
        self.subtabs.currentChanged.connect(self._on_subtab_changed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        layout.addWidget(self.filter_bar)
        layout.addLayout(bins_row)
        layout.addWidget(self.subtabs, 1)

        # Restore last-active sub-tab.
        if self._settings is not None:
            try:
                idx = int(self._settings.value(SETTINGS_ACTIVE_TAB, 0))
            except (TypeError, ValueError):
                idx = 0
            if 0 <= idx < self.subtabs.count():
                self.subtabs.setCurrentIndex(idx)

        # Initial bins-button state for the restored sub-tab.
        self._update_bins_button(self.subtabs.currentIndex())

        # First load: pull symbols + render with default (All) filters.
        self.refresh()

    # ---- public ------------------------------------------------------------

    def refresh(
        self, preloaded_closed: Optional[list[dict]] = None,
    ) -> None:
        """Reload trades from DB, refresh symbol picker, re-render active tab."""
        if preloaded_closed is not None:
            self._all_closed = preloaded_closed
        else:
            self._all_closed = db_manager.fetch_closed_trades(
                self._get_conn(),
            )
        symbols = sorted({
            t.get("symbol", "")
            for t in self._all_closed
            if t.get("symbol")
        })
        self.filter_bar.set_symbols(symbols)

        spec = self.filter_bar.current_spec()
        self._filtered_cache = apply_filters(self._all_closed, spec)
        self._render_active()

    # ---- internals ---------------------------------------------------------

    def _on_filters_changed(self, spec: FilterSpec) -> None:
        # Filter-control changes don't need a DB round-trip — reuse the
        # cached `_all_closed` populated by the most recent refresh().
        self._filtered_cache = apply_filters(self._all_closed, spec)
        self._render_active()

    def _on_subtab_changed(self, idx: int) -> None:
        if self._settings is not None:
            self._settings.setValue(SETTINGS_ACTIVE_TAB, idx)
        self._update_bins_button(idx)
        self._render_active()

    def _update_bins_button(self, idx: int) -> None:
        label = self._label_for(idx)
        # The planned-stops filter only applies to the R-Multiple report.
        self.chk_planned_stops.setVisible(label == LABEL_BY_R)
        if label == LABEL_BY_PRICE:
            self.btn_bins.setText("Configure Price Bins…")
            self.btn_bins.setEnabled(True)
        elif label == LABEL_BY_HOLD:
            self.btn_bins.setText("Configure Hold-Time Bins…")
            self.btn_bins.setEnabled(True)
        else:
            self.btn_bins.setText("Configure Bins…")
            self.btn_bins.setEnabled(False)

    def _label_for(self, idx: int) -> Optional[str]:
        if 0 <= idx < len(REPORT_BUILDERS):
            return REPORT_BUILDERS[idx][0]
        return None

    def _render_active(self) -> None:
        idx = self.subtabs.currentIndex()
        label = self._label_for(idx)
        if label is None:
            return
        builder = REPORT_BUILDERS[idx][1]

        if label == LABEL_BY_PRICE:
            report = builder(self._filtered_cache, edges=self._price_edges)
        elif label == LABEL_BY_HOLD:
            edges_seconds = [
                float("inf") if math.isinf(m) else float(m) * 60.0
                for m in self._hold_edges_minutes
            ]
            report = builder(self._filtered_cache, edges=edges_seconds)
        elif label == LABEL_BY_R:
            default_risk = load_default_risk(self._settings) or None
            report = builder(
                self._filtered_cache,
                default_risk=default_risk,
                exclude_derived=self.chk_planned_stops.isChecked(),
            )
        else:
            report = builder(self._filtered_cache)

        self._views[idx].set_report(report)

    # ---- planned-stops filter ----------------------------------------------

    def _load_planned_stops_only(self) -> bool:
        if self._settings is None:
            return False
        raw = self._settings.value(SETTINGS_PLANNED_STOPS_ONLY, False)
        return str(raw).lower() in ("1", "true", "yes")

    def _on_planned_stops_toggled(self, checked: bool) -> None:
        if self._settings is not None:
            self._settings.setValue(SETTINGS_PLANNED_STOPS_ONLY, checked)
        self._render_active()

    # ---- bin configuration -------------------------------------------------

    def _on_configure_bins(self) -> None:
        idx = self.subtabs.currentIndex()
        label = self._label_for(idx)

        if label == LABEL_BY_PRICE:
            dlg = BinConfigDialog(
                KIND_PRICE, self._price_edges, list(DEFAULT_PRICE_EDGES), self,
            )
            if dlg.exec() == QDialog.DialogCode.Accepted:
                edges = dlg.selected_edges()
                if edges:
                    self._price_edges = edges
                    self._save_edges(SETTINGS_PRICE_EDGES, edges)
                    self._render_active()
        elif label == LABEL_BY_HOLD:
            dlg = BinConfigDialog(
                KIND_HOLD_MINUTES, self._hold_edges_minutes,
                list(DEFAULT_HOLD_EDGES_MINUTES), self,
            )
            if dlg.exec() == QDialog.DialogCode.Accepted:
                edges = dlg.selected_edges()
                if edges:
                    self._hold_edges_minutes = edges
                    self._save_edges(SETTINGS_HOLD_EDGES_MINUTES, edges)
                    self._render_active()

    # ---- settings round-trip ----------------------------------------------

    def _load_edges(
        self, key: str, default: list[float],
    ) -> list[float]:
        if self._settings is None:
            return list(default)
        raw = self._settings.value(key)
        if not isinstance(raw, str) or not raw.strip():
            return list(default)
        try:
            return parse_edges(raw)
        except ValueError:
            return list(default)

    def _save_edges(self, key: str, edges: list[float]) -> None:
        if self._settings is None:
            return
        self._settings.setValue(key, format_edges(edges))
