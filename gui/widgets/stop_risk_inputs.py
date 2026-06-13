"""Paired stop-price / risk-dollar inputs with bidirectional sync.

Traders think about trade risk in two equivalent ways:

    * A *stop price* — "I'll bail if AAPL dips to $182.50".
    * A *risk amount* — "I'm putting $100 on this trade".

This widget lets the user enter whichever is more natural and derives
the other from the trade's entry price + share count + direction:

    risk_amount = |entry − stop| × shares
    stop_price  = entry − (risk / shares)   (Long)
                = entry + (risk / shares)   (Short)

Typing in one field immediately updates the other. Changing the
context (entry price, shares, direction) recomputes the *risk*
from the *stop* — stop stays pinned as the authoritative value,
risk re-derives, since a real-world stop price is independent of
position sizing.

Zero-or-blank values on both fields are valid and mean "no stop
recorded" — callers read that as ``stop_price() is None``.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QDoubleSpinBox, QFrame, QGridLayout, QLabel, QWidget,
)


class StopRiskInputs(QWidget):
    """Two linked QDoubleSpinBoxes — stop price and risk dollars."""

    # Emitted after either field lands on a new user-driven value.
    values_changed = Signal()

    def __init__(
        self,
        entry_price: float = 0.0,
        shares: int = 0,
        direction: str = "Long",
        *,
        parent=None,
    ):
        super().__init__(parent)
        self._entry = float(entry_price)
        self._shares = int(shares)
        self._direction = direction or "Long"
        self._syncing = False

        self.stop_spin = QDoubleSpinBox(self)
        self.stop_spin.setRange(0.0, 1_000_000.0)
        self.stop_spin.setDecimals(4)
        self.stop_spin.setPrefix("$")
        self.stop_spin.setSpecialValueText("(none)")
        self.stop_spin.valueChanged.connect(self._on_stop_changed)

        self.risk_spin = QDoubleSpinBox(self)
        self.risk_spin.setRange(0.0, 10_000_000.0)
        self.risk_spin.setDecimals(2)
        self.risk_spin.setPrefix("$")
        self.risk_spin.setSpecialValueText("(none)")
        self.risk_spin.valueChanged.connect(self._on_risk_changed)

        self.hint_label = QLabel("", self)
        self.hint_label.setObjectName("hintLabel")
        self.hint_label.setWordWrap(True)

        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(4)
        grid.addWidget(QLabel("Stop price:", self), 0, 0)
        grid.addWidget(self.stop_spin, 0, 1)
        grid.addWidget(QLabel("Risk amount:", self), 1, 0)
        grid.addWidget(self.risk_spin, 1, 1)
        grid.addWidget(self.hint_label, 2, 0, 1, 2)
        grid.setColumnStretch(1, 1)

        self._refresh_hint()

    # ---- context -------------------------------------------------------

    def set_context(
        self,
        entry_price: float,
        shares: int,
        direction: str,
    ) -> None:
        """Update entry/shares/direction. Stop stays pinned; risk
        re-derives from the new context."""
        self._entry = float(entry_price or 0.0)
        self._shares = int(shares or 0)
        self._direction = direction or "Long"
        # Re-sync risk from the current stop, without firing signals.
        stop = self._raw_stop()
        if stop is not None and self._entry > 0 and self._shares > 0:
            new_risk = abs(self._entry - stop) * self._shares
            self._set_silent(self.risk_spin, new_risk)
        self._refresh_hint()

    # ---- public accessors ---------------------------------------------

    def stop_price(self) -> Optional[float]:
        """Return the stop price, or None when effectively unset."""
        v = self._raw_stop()
        return v if v is not None else None

    def risk_amount(self) -> float:
        """Dollar risk implied by the current stop, or 0.0 if unset."""
        return float(self.risk_spin.value())

    def set_stop_price(self, price: Optional[float]) -> None:
        self._set_silent(
            self.stop_spin, 0.0 if price is None else float(price),
        )
        # Re-sync risk from the new stop.
        stop = self._raw_stop()
        if stop is not None and self._entry > 0 and self._shares > 0:
            self._set_silent(
                self.risk_spin,
                abs(self._entry - stop) * self._shares,
            )
        else:
            self._set_silent(self.risk_spin, 0.0)
        self._refresh_hint()

    # ---- internals -----------------------------------------------------

    def _raw_stop(self) -> Optional[float]:
        v = self.stop_spin.value()
        return v if v > 0 else None

    @staticmethod
    def _set_silent(spin: QDoubleSpinBox, value: float) -> None:
        blocked = spin.blockSignals(True)
        try:
            spin.setValue(value)
        finally:
            spin.blockSignals(blocked)

    def _on_stop_changed(self, value: float) -> None:
        if self._syncing:
            return
        self._syncing = True
        try:
            if value <= 0 or self._entry <= 0 or self._shares <= 0:
                self._set_silent(self.risk_spin, 0.0)
            else:
                new_risk = abs(self._entry - value) * self._shares
                self._set_silent(self.risk_spin, new_risk)
        finally:
            self._syncing = False
        self._refresh_hint()
        self.values_changed.emit()

    def _on_risk_changed(self, value: float) -> None:
        if self._syncing:
            return
        self._syncing = True
        try:
            if value <= 0 or self._entry <= 0 or self._shares <= 0:
                self._set_silent(self.stop_spin, 0.0)
            else:
                risk_per_share = value / self._shares
                if self._direction == "Short":
                    stop = self._entry + risk_per_share
                else:
                    stop = max(0.0, self._entry - risk_per_share)
                self._set_silent(self.stop_spin, stop)
        finally:
            self._syncing = False
        self._refresh_hint()
        self.values_changed.emit()

    def _refresh_hint(self) -> None:
        if self._entry <= 0 or self._shares <= 0:
            self.hint_label.setText(
                "<i>Enter the trade's entry price and shares to link "
                "stop ↔ risk.</i>"
            )
            return
        stop = self._raw_stop()
        if stop is None:
            self.hint_label.setText(
                f"<i>No stop recorded. Trade will use the default "
                f"risk amount (if set) for R-multiple reports.</i>"
            )
            return
        risk = self.risk_spin.value()
        per_share = abs(self._entry - stop)
        self.hint_label.setText(
            f"<i>Risking ${per_share:.4f} per share × "
            f"{self._shares} = ${risk:,.2f}.</i>"
        )
