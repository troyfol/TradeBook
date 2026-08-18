"""Trades tab — ZoomableTableView over TradeTableModel, with empty state
and right-click Hide / Delete actions.

Multi-select: rows are selectable in extended mode (Ctrl/Shift click).
Right-click acts on the full selection if the clicked row is part of it,
otherwise on the clicked row only — the standard "right-click promotes
to single-row action" pattern. Bulk Delete confirms once for the whole
batch. A Delete-all-visible button on the toolbar wipes the entire model
(after confirmation), respecting any active date filter.

Phase 5 addition: optional `date_filter` (set via `set_date_filter`) that
restricts visible rows to trades whose exit_time falls on a single day.
When active, a dismissible banner appears above the table.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Callable, Optional

from PySide6.QtCore import Qt, QPoint, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDoubleSpinBox, QHBoxLayout, QHeaderView,
    QLabel, QMenu, QMessageBox, QPushButton, QStackedWidget, QVBoxLayout,
    QWidget,
)

from analytics.reports import FilterSpec, apply_filters
from gui.dialogs.manual_trade import ManualTradeDialog
from gui.dialogs.stop_loss import StopLossDialog
from gui.widgets.report_filter_bar import ReportFilterBar
from gui.widgets.zoom_controls import ZoomableTableView, ZoomControls
from ingest import db_manager
from models.table_model import TradeTableModel


def _is_default_spec(spec: FilterSpec) -> bool:
    """A spec with no filters set — used to short-circuit the filter pass
    so the default Trades view shows everything (open trades included)."""
    return (
        spec.start is None
        and spec.end is None
        and not spec.symbols
        and spec.direction is None
        and spec.result is None
        and spec.min_hold_seconds == 0
        and spec.max_hold_seconds == 0
    )


class TradesTab(QWidget):
    """Read-only table of logical trades.

    Context menu:
        Hide   — writes to `hidden_trades`, survives rebuild_trades
        Delete — hard-removes trade + underlying executions (with confirm)

    Shows a dimmed empty-state label when there are no (visible) trades.
    On a true first-run (no trades AND no date filter) the empty state
    also offers a button that takes the user straight to the New Trade
    tab — that's the only screen they need on day one.
    """

    # Emitted when the first-run "Go to New Trade" button is clicked.
    # MainWindow routes this to a tab switch.
    goto_new_trade_requested = Signal()

    def __init__(self, get_conn: Callable, parent=None):
        super().__init__(parent)
        self._get_conn = get_conn
        self._date_filter: Optional[date] = None
        self._all_trades: list[dict] = []

        self.model = TradeTableModel()
        self.table = ZoomableTableView(self)
        self.table.setModel(self.model)
        self.table.setSortingEnabled(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)

        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_context_menu)

        self.empty_label = QLabel(
            "No trades yet — paste an order export in the New Trade tab.",
            self,
        )
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setObjectName("emptyState")

        # First-run shortcut: button below the empty label that jumps
        # straight to the New Trade tab. Hidden when a date filter is
        # active (the "no trades on this day" message doesn't need it).
        self.btn_goto_new_trade = QPushButton("Go to New Trade →", self)
        self.btn_goto_new_trade.clicked.connect(
            self.goto_new_trade_requested.emit
        )

        self.empty_widget = QWidget(self)
        empty_layout = QVBoxLayout(self.empty_widget)
        empty_layout.setContentsMargins(20, 20, 20, 20)
        empty_layout.setSpacing(16)
        empty_layout.addStretch()
        empty_layout.addWidget(self.empty_label)
        button_row = QHBoxLayout()
        button_row.addStretch()
        button_row.addWidget(self.btn_goto_new_trade)
        button_row.addStretch()
        empty_layout.addLayout(button_row)
        empty_layout.addStretch()

        self.stack = QStackedWidget(self)
        self.stack.addWidget(self.table)         # index 0
        self.stack.addWidget(self.empty_widget)  # index 1

        self.zoom_controls = ZoomControls(self.table, self)

        # Bulk-action toolbar row.
        self.btn_new_manual = QPushButton("+ New manual trade", self)
        self.btn_new_manual.setToolTip(
            "Record a trade by hand — useful for off-broker fills, "
            "paper trades, or correcting auto-builder mistakes."
        )
        self.btn_new_manual.clicked.connect(self._on_new_manual_trade)

        # Bulk risk → stop-loss. Enter a dollar amount, pick one or
        # more trades, click Apply. Each trade's stop is derived from
        # its own entry price + share count + direction, so one risk
        # value produces per-trade stops. $0 clears the stop.
        self.risk_input = QDoubleSpinBox(self)
        self.risk_input.setRange(0.0, 10_000_000.0)
        self.risk_input.setDecimals(2)
        self.risk_input.setPrefix("$")
        self.risk_input.setSingleStep(25.0)
        self.risk_input.setSpecialValueText("(clear)")
        self.risk_input.setValue(0.0)
        self.risk_input.setFixedWidth(110)
        self.risk_input.setToolTip(
            "Dollar risk to apply to the selected trades"
        )

        self.btn_apply_risk = QPushButton("Apply risk to selected", self)
        self.btn_apply_risk.setToolTip(
            "Compute a stop price from the risk amount using each "
            "trade's own entry price + shares + direction, then set "
            "it. $0 clears the stop on the selected trades."
        )
        self.btn_apply_risk.clicked.connect(self._on_apply_risk_to_selected)

        self.btn_executions = QPushButton("Manage executions…", self)
        self.btn_executions.setToolTip(
            "View, edit, or delete the raw fills behind these trades — "
            "including fills the trade builder couldn't group into any "
            "trade, which are unreachable from anywhere else."
        )
        self.btn_executions.clicked.connect(self._on_manage_executions)

        self.btn_delete_all = QPushButton("Delete all visible", self)
        self.btn_delete_all.setToolTip(
            "Delete every trade currently shown in the table "
            "(respects the active date filter). Deletions go to the "
            "Recycle Bin and can be restored within 30 days."
        )
        self.btn_delete_all.clicked.connect(self._on_delete_all_visible)

        toolbar_row = QHBoxLayout()
        toolbar_row.setContentsMargins(0, 0, 0, 0)
        toolbar_row.addWidget(self.zoom_controls)
        toolbar_row.addStretch()
        toolbar_row.addWidget(QLabel("Risk:", self))
        toolbar_row.addWidget(self.risk_input)
        toolbar_row.addWidget(self.btn_apply_risk)
        toolbar_row.addSpacing(12)
        toolbar_row.addWidget(self.btn_new_manual)
        toolbar_row.addWidget(self.btn_executions)
        toolbar_row.addWidget(self.btn_delete_all)

        # Reports-style filter bar. Defaults to "All" so the Trades tab
        # opens unfiltered. Coexists with the calendar drilldown
        # `_date_filter` (both are AND'd in `_apply_filters_and_render`).
        self.filter_bar = ReportFilterBar(self)
        self.filter_bar.filters_changed.connect(
            lambda _spec: self._apply_filters_and_render()
        )

        # Date-filter banner (hidden until set_date_filter is called).
        # Styling lives in dark_theme.qss keyed on #DateFilterBanner.
        self.filter_banner = QWidget(self)
        self.filter_banner.setObjectName("DateFilterBanner")
        self.filter_banner_label = QLabel("", self.filter_banner)
        self.filter_banner_clear = QPushButton("Clear filter", self.filter_banner)
        self.filter_banner_clear.clicked.connect(self.clear_date_filter)
        banner_layout = QHBoxLayout(self.filter_banner)
        banner_layout.setContentsMargins(10, 6, 10, 6)
        banner_layout.addWidget(self.filter_banner_label, 1)
        banner_layout.addWidget(self.filter_banner_clear)
        self.filter_banner.setVisible(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(self.filter_bar)
        layout.addLayout(toolbar_row)
        layout.addWidget(self.filter_banner)
        layout.addWidget(self.stack, 1)

        self.refresh()
        # Default sort: newest first
        self.table.sortByColumn(0, Qt.SortOrder.DescendingOrder)

    # ---- public API -------------------------------------------------------

    def refresh(self, preloaded_trades: Optional[list[dict]] = None) -> None:
        """Re-pull trades from the DB, refresh the symbol picker, re-render.

        Pass ``preloaded_trades`` to reuse a fetch the caller already did
        (MainWindow.refresh_all shares one query across all tabs).
        """
        if preloaded_trades is not None:
            self._all_trades = preloaded_trades
        else:
            self._all_trades = db_manager.fetch_trades_for_display(
                self._get_conn(),
            )
        symbols = sorted({
            t.get("symbol", "") for t in self._all_trades if t.get("symbol")
        })
        self.filter_bar.set_symbols(symbols)
        self._apply_filters_and_render()

    def _apply_filters_and_render(self) -> None:
        """Re-filter the cached trade list against current UI state and
        push it into the table model. Cheap — no DB hit."""
        trades = self._all_trades
        spec = self.filter_bar.current_spec()
        if not _is_default_spec(spec):
            trades = apply_filters(trades, spec)
        if self._date_filter is not None:
            trades = [
                t for t in trades if self._exit_date(t) == self._date_filter
            ]
        self.model.set_trades(trades)
        self._update_empty_state()

    def set_date_filter(self, d: Optional[date]) -> None:
        """Restrict the visible list to trades whose exit_time is on `d`.

        Pass None to clear the filter.
        """
        self._date_filter = d
        if d is None:
            self.filter_banner.setVisible(False)
            self.empty_label.setText(
                "No trades yet — paste an order export in the New Trade tab."
            )
            self.btn_goto_new_trade.setVisible(True)
        else:
            self.filter_banner_label.setText(
                f"Showing trades for {d.strftime('%A, %b %d, %Y')}"
            )
            self.filter_banner.setVisible(True)
            self.empty_label.setText("No trades on this day.")
            self.btn_goto_new_trade.setVisible(False)
        self._apply_filters_and_render()

    def clear_date_filter(self) -> None:
        self.set_date_filter(None)

    @staticmethod
    def _exit_date(trade: dict) -> Optional[date]:
        et = trade.get("exit_time")
        if et is None:
            return None
        return et.date() if isinstance(et, datetime) else et

    def _update_empty_state(self) -> None:
        if self.model.rowCount() == 0:
            self.stack.setCurrentIndex(1)
        else:
            self.stack.setCurrentIndex(0)

    # ---- context menu -----------------------------------------------------

    def _selected_rows(self) -> list[dict]:
        """Return the trade dicts for every currently selected row."""
        rows: list[dict] = []
        seen: set[int] = set()
        for idx in self.table.selectionModel().selectedRows():
            r = self.model.row_at(idx.row())
            if r is None:
                continue
            rid = r.get("id")
            if rid in seen:
                continue
            seen.add(rid)
            rows.append(r)
        return rows

    def _on_context_menu(self, pos: QPoint) -> None:
        idx = self.table.indexAt(pos)
        if not idx.isValid():
            return
        clicked = self.model.row_at(idx.row())
        if clicked is None:
            return

        # If the right-clicked row isn't already part of the selection,
        # treat it as a single-row action (and reset the selection so the
        # user sees what's about to be acted on).
        selected = self._selected_rows()
        if not any(r.get("id") == clicked.get("id") for r in selected):
            self.table.clearSelection()
            self.table.selectRow(idx.row())
            selected = [clicked]

        n = len(selected)
        menu = QMenu(self)
        hide_action = menu.addAction(f"Hide ({n})" if n > 1 else "Hide")
        delete_action = menu.addAction(
            f"Delete ({n})" if n > 1 else "Delete"
        )
        menu.addSeparator()
        # Edit is only meaningful for a single manual trade.
        edit_action = None
        if n == 1 and selected[0].get("is_manual"):
            edit_action = menu.addAction("Edit manual trade…")
        stop_action = menu.addAction(
            "Set stop loss…" if n == 1 else f"Set stop loss… ({n})"
        )
        clear_stop_action = None
        if n == 1 and selected[0].get("stop_loss_price") is not None:
            clear_stop_action = menu.addAction("Clear stop loss")

        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))

        if chosen is None:
            return
        if chosen == hide_action:
            self._hide_rows(selected)
        elif chosen == delete_action:
            self._delete_rows(selected)
        elif edit_action is not None and chosen == edit_action:
            self._edit_manual_trade(selected[0])
        elif chosen == stop_action:
            self._set_stop_for_rows(selected)
        elif clear_stop_action is not None and chosen == clear_stop_action:
            db_manager.set_stop_loss(
                self._get_conn(), int(selected[0]["id"]), None,
            )
            self.refresh()

    def _hide_rows(self, rows: list[dict]) -> None:
        if not rows:
            return
        conn = self._get_conn()
        for r in rows:
            db_manager.hide_trade(
                conn, r["symbol"], r["entry_time"], r["direction"],
            )
        self.refresh()

    def _delete_rows(self, rows: list[dict]) -> None:
        if not rows:
            return
        if len(rows) == 1:
            r = rows[0]
            prompt = (
                f"Delete the {r['direction']} {r['symbol']} trade entered "
                f"at {r['entry_time']}?\n\n"
                "The trade (and its executions, journal, and tags) moves "
                "to the Recycle Bin — restorable from File → Recycle bin."
            )
        else:
            prompt = (
                f"Delete {len(rows)} selected trades?\n\n"
                "Each moves to the Recycle Bin with its executions, "
                "journal, and tags — restorable from File → Recycle bin."
            )
        reply = QMessageBox.question(
            self, "Delete trades", prompt,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        conn = self._get_conn()
        ids = [int(r["id"]) for r in rows]
        db_manager.soft_delete_trades(conn, ids)
        self.refresh()

    # ---- delete all visible -----------------------------------------------

    def _on_delete_all_visible(self) -> None:
        n = self.model.rowCount()
        if n == 0:
            return
        scope = (
            f" for {self._date_filter.strftime('%A, %b %d, %Y')}"
            if self._date_filter is not None else ""
        )
        reply = QMessageBox.question(
            self, "Delete all visible trades",
            f"Delete all {n} visible trade(s){scope}?\n\n"
            "Each moves to the Recycle Bin with its executions, "
            "journal, and tags — restorable from File → Recycle bin.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        ids = [
            int(self.model.row_at(i)["id"])
            for i in range(n)
            if self.model.row_at(i) is not None
        ]
        if not ids:
            return
        db_manager.soft_delete_trades(self._get_conn(), ids)
        self.refresh()

    # ---- manual trade entry ----------------------------------------------

    def _on_manage_executions(self) -> None:
        """Open the raw-executions editor; refresh if it changed anything.

        With rows selected, the editor is scoped to just those trades'
        fills — the manager should reflect what's in the trade section,
        not an unrelated wall of executions. With nothing selected it
        shows everything.
        """
        from gui.dialogs.executions import ExecutionsDialog

        selected = [
            int(r["id"]) for r in self._selected_rows()
            if r.get("id") is not None
        ]
        dlg = ExecutionsDialog(
            self._get_conn, self, trade_ids=selected or None,
        )
        dlg.exec()
        if dlg.mutated:
            self.refresh()

    def _on_new_manual_trade(self) -> None:
        dlg = ManualTradeDialog(parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        vals = dlg.values()
        db_manager.insert_manual_trade(self._get_conn(), **vals)
        self.refresh()

    def _edit_manual_trade(self, row: dict) -> None:
        # Re-pull the full row so we have every column (display rows
        # trim to what the model needs).
        full = db_manager.fetch_trade(self._get_conn(), int(row["id"]))
        if full is None:
            return
        dlg = ManualTradeDialog(existing=full, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        vals = dlg.values()
        db_manager.update_manual_trade(
            self._get_conn(), int(row["id"]), **vals,
        )
        self.refresh()

    # ---- stop-loss entry -------------------------------------------------

    # ---- bulk risk → stop application -----------------------------------

    def _on_apply_risk_to_selected(self) -> None:
        rows = self._selected_rows()
        if not rows:
            QMessageBox.information(
                self, "No selection",
                "Select one or more trades first, then click Apply.",
            )
            return
        risk = float(self.risk_input.value())
        conn = self._get_conn()
        applied = 0
        cleared = 0
        skipped: list[str] = []
        for r in rows:
            tid = int(r["id"])
            if risk <= 0:
                db_manager.set_stop_loss(conn, tid, None)
                cleared += 1
                continue
            # Re-read the full trade row so we have entry + shares +
            # direction (the display rows may not carry all of them).
            full = db_manager.fetch_trade(conn, tid) or r
            stop = db_manager.risk_to_stop_price(
                str(full.get("direction") or "Long"),
                float(full.get("avg_entry_price") or 0.0),
                int(full.get("total_shares") or 0),
                risk,
            )
            if stop is None:
                skipped.append(
                    f"{full.get('symbol') or '?'} "
                    f"@{full.get('entry_time') or ''}"
                )
                continue
            db_manager.set_stop_loss(conn, tid, stop)
            applied += 1
        self.refresh()

        # Status feedback — use a single confirmation dialog so the
        # user knows what happened (especially when some trades were
        # skipped for missing entry/shares).
        parts: list[str] = []
        if applied:
            parts.append(
                f"Set stop on {applied} trade(s) with ${risk:,.2f} "
                "of risk."
            )
        if cleared:
            parts.append(f"Cleared stop on {cleared} trade(s).")
        if skipped:
            preview = ", ".join(skipped[:3])
            more = f" (+{len(skipped) - 3} more)" if len(skipped) > 3 else ""
            parts.append(
                f"Skipped {len(skipped)} trade(s) without a usable "
                f"entry/shares: {preview}{more}."
            )
        if parts:
            QMessageBox.information(self, "Apply risk", "\n\n".join(parts))

    # ---- single-row Set-Stop-Loss (right-click) -------------------------

    def _set_stop_for_rows(self, rows: list[dict]) -> None:
        if not rows:
            return
        # For a single-row action pull the full trade row so the dialog
        # has entry price + shares + direction for the risk linking.
        trades_for_dialog: list[dict] = []
        conn = self._get_conn()
        if len(rows) == 1:
            full = db_manager.fetch_trade(conn, int(rows[0]["id"]))
            trades_for_dialog = [full] if full is not None else rows
        else:
            trades_for_dialog = rows

        dlg = StopLossDialog(trades_for_dialog, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        stop = dlg.chosen_stop_price()
        for r in rows:
            db_manager.set_stop_loss(conn, int(r["id"]), stop)
        self.refresh()
