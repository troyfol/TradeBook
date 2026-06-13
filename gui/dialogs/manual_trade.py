"""Add or edit a manual trade.

Manual trades fill the gap when the auto-builder can't reconstruct a
trade from its executions — off-broker fills, paper trades, corrections
to mis-grouped fills, etc. Stored with ``is_manual=1`` so
``rebuild_trades`` leaves them alone on the next import.
"""
from __future__ import annotations

import datetime as _dt
from typing import Optional

from PySide6.QtCore import QDate, QDateTime, QTime
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDateTimeEdit, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QFormLayout, QLabel, QLineEdit, QMessageBox, QSpinBox,
    QVBoxLayout,
)

from gui.dialogs._geometry import DialogGeometryMixin
from gui.widgets.stop_risk_inputs import StopRiskInputs


class ManualTradeDialog(DialogGeometryMixin, QDialog):
    """Symbol / direction / time / price / shares / commission entry."""

    GEOMETRY_KEY = "dialog/manual_trade/geometry"

    def __init__(
        self,
        *,
        existing: Optional[dict] = None,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle(
            "Edit manual trade" if existing else "New manual trade"
        )
        self.setModal(True)
        self._existing = existing or {}

        # ---- form fields ------------------------------------------------
        self.symbol_edit = QLineEdit(self)
        self.symbol_edit.setPlaceholderText("e.g. AAPL")
        self.symbol_edit.setMaxLength(16)

        self.direction_combo = QComboBox(self)
        self.direction_combo.addItem("Long", "Long")
        self.direction_combo.addItem("Short", "Short")

        now = _dt.datetime.now().replace(second=0, microsecond=0)
        self.entry_dt = QDateTimeEdit(self)
        self.entry_dt.setCalendarPopup(True)
        self.entry_dt.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.entry_dt.setDateTime(QDateTime(
            QDate(now.year, now.month, now.day),
            QTime(now.hour, now.minute),
        ))

        self.exit_dt = QDateTimeEdit(self)
        self.exit_dt.setCalendarPopup(True)
        self.exit_dt.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.exit_dt.setDateTime(QDateTime(
            QDate(now.year, now.month, now.day),
            QTime(now.hour, now.minute),
        ))

        # "Trade still open" checkbox — when checked, exit fields are
        # disabled and exit_time/exit_price are stored as NULL.
        self.chk_open = QCheckBox("Trade still open (no exit yet)", self)
        self.chk_open.toggled.connect(self._on_open_toggled)

        self.entry_price = self._make_price_spin()
        self.exit_price = self._make_price_spin()
        self.shares = QSpinBox(self)
        self.shares.setRange(1, 10_000_000)
        self.shares.setValue(100)
        self.commission = QDoubleSpinBox(self)
        self.commission.setRange(0.0, 10_000.0)
        self.commission.setDecimals(2)
        self.commission.setPrefix("$")
        self.commission.setValue(0.0)

        # Optional stop-loss for R-multiple analytics. Linked to a
        # risk-dollars input so the user can enter whichever is more
        # natural — the other field auto-computes from entry × shares.
        self.stop_risk = StopRiskInputs(parent=self)
        # Keep the widget's context up to date as the user types in
        # the entry price / shares / direction fields.
        self.entry_price.valueChanged.connect(self._resync_stop_context)
        self.shares.valueChanged.connect(self._resync_stop_context)
        self.direction_combo.currentIndexChanged.connect(
            self._resync_stop_context
        )

        # Pre-fill from existing row (edit mode).
        if existing:
            self._populate_from_existing(existing)
        # Seed context once after any prefill.
        self._resync_stop_context()

        # ---- layout -----------------------------------------------------
        form = QFormLayout()
        form.addRow("Symbol:", self.symbol_edit)
        form.addRow("Direction:", self.direction_combo)
        form.addRow("Entry time:", self.entry_dt)
        form.addRow("Entry price:", self.entry_price)
        form.addRow("Shares:", self.shares)
        form.addRow("Commission:", self.commission)
        form.addRow("", self.chk_open)
        form.addRow("Exit time:", self.exit_dt)
        form.addRow("Exit price:", self.exit_price)
        form.addRow("Stop / risk (optional):", self.stop_risk)

        hint = QLabel(
            "P&L is computed automatically from entry / exit / shares.",
            self,
        )
        hint.setObjectName("hintLabel")
        hint.setWordWrap(True)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        self._buttons.accepted.connect(self._on_accept)
        self._buttons.rejected.connect(self.reject)

        outer = QVBoxLayout(self)
        outer.addLayout(form)
        outer.addWidget(hint)
        outer.addStretch()
        outer.addWidget(self._buttons)

        self.resize(420, 460)
        self._restore_geometry()
        self._on_open_toggled(self.chk_open.isChecked())

    def _make_price_spin(self) -> QDoubleSpinBox:
        sb = QDoubleSpinBox(self)
        sb.setRange(0.0, 1_000_000.0)
        sb.setDecimals(4)
        sb.setPrefix("$")
        sb.setValue(0.0)
        return sb

    def _populate_from_existing(self, t: dict) -> None:
        self.symbol_edit.setText(str(t.get("symbol") or ""))
        idx = self.direction_combo.findData(t.get("direction"))
        if idx >= 0:
            self.direction_combo.setCurrentIndex(idx)
        et = t.get("entry_time")
        if isinstance(et, _dt.datetime):
            self.entry_dt.setDateTime(QDateTime(
                QDate(et.year, et.month, et.day),
                QTime(et.hour, et.minute),
            ))
        xt = t.get("exit_time")
        if isinstance(xt, _dt.datetime):
            self.exit_dt.setDateTime(QDateTime(
                QDate(xt.year, xt.month, xt.day),
                QTime(xt.hour, xt.minute),
            ))
        self.entry_price.setValue(float(t.get("avg_entry_price") or 0.0))
        if t.get("avg_exit_price") is not None:
            self.exit_price.setValue(float(t.get("avg_exit_price")))
        self.shares.setValue(int(t.get("total_shares") or 1))
        self.commission.setValue(float(t.get("total_commission") or 0.0))
        if t.get("stop_loss_price") is not None:
            self.stop_risk.set_stop_price(float(t["stop_loss_price"]))
        self.chk_open.setChecked(bool(t.get("is_open")))

    def _on_open_toggled(self, checked: bool) -> None:
        self.exit_dt.setEnabled(not checked)
        self.exit_price.setEnabled(not checked)

    def _resync_stop_context(self) -> None:
        """Feed the StopRiskInputs widget the current entry/shares/
        direction so its auto-link math stays correct as the user
        edits the surrounding fields."""
        self.stop_risk.set_context(
            entry_price=float(self.entry_price.value()),
            shares=int(self.shares.value()),
            direction=str(self.direction_combo.currentData()),
        )

    def _on_accept(self) -> None:
        sym = self.symbol_edit.text().strip().upper()
        if not sym:
            QMessageBox.information(self, "Symbol required", "Enter a symbol.")
            return
        if self.entry_price.value() <= 0:
            QMessageBox.information(
                self, "Entry price required", "Entry price must be > $0.",
            )
            return
        if not self.chk_open.isChecked():
            if self.exit_price.value() <= 0:
                QMessageBox.information(
                    self, "Exit price required",
                    "Exit price must be > $0 (or check 'Trade still open').",
                )
                return
            entry = self._qdt_to_py(self.entry_dt.dateTime())
            exit_ = self._qdt_to_py(self.exit_dt.dateTime())
            if exit_ < entry:
                QMessageBox.information(
                    self, "Bad time range",
                    "Exit time can't be before entry time.",
                )
                return
        self.accept()

    @staticmethod
    def _qdt_to_py(qdt: QDateTime) -> _dt.datetime:
        d = qdt.date()
        t = qdt.time()
        return _dt.datetime(
            d.year(), d.month(), d.day(),
            t.hour(), t.minute(), t.second(),
        )

    # ---- public accessors ---------------------------------------------

    def values(self) -> dict:
        is_open = self.chk_open.isChecked()
        return {
            "symbol": self.symbol_edit.text().strip().upper(),
            "direction": str(self.direction_combo.currentData()),
            "entry_time": self._qdt_to_py(self.entry_dt.dateTime()),
            "exit_time": (
                None if is_open else self._qdt_to_py(self.exit_dt.dateTime())
            ),
            "avg_entry_price": float(self.entry_price.value()),
            "avg_exit_price": (
                None if is_open else float(self.exit_price.value())
            ),
            "total_shares": int(self.shares.value()),
            "total_commission": float(self.commission.value()),
            "stop_loss_price": self.stop_risk.stop_price(),
        }
