"""Dialog for setting the stop-loss price on one or more existing trades.

Replaces the one-line ``QInputDialog.getDouble`` flow so users can
enter either a stop price or a risk dollar amount; whichever they
pick, the other auto-fills from the trade's entry price + share count.

For a multi-trade bulk operation the dialog still accepts a single
stop *price* (a shared dollar stop doesn't usually make sense across
different tickers, but it's occasionally useful for baskets). The
risk-dollars field is disabled in that case — there's no single
"risk amount" when shares and entries differ per trade.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QLabel, QVBoxLayout,
)

from gui.dialogs._geometry import DialogGeometryMixin
from gui.widgets.stop_risk_inputs import StopRiskInputs


class StopLossDialog(DialogGeometryMixin, QDialog):
    """Set (or clear) a stop on one or more trades."""

    GEOMETRY_KEY = "dialog/stop_loss/geometry"

    def __init__(
        self,
        trades: list[dict],
        *,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle(
            "Set stop loss" if len(trades) == 1
            else f"Set stop loss ({len(trades)} trades)"
        )
        self.setModal(True)
        self._trades = list(trades)

        # Context for the paired input only makes sense for a single trade.
        if len(trades) == 1:
            t = trades[0]
            entry = float(t.get("avg_entry_price") or 0.0)
            shares = int(t.get("total_shares") or 0)
            direction = str(t.get("direction") or "Long")
            self.stop_risk = StopRiskInputs(
                entry_price=entry, shares=shares, direction=direction,
                parent=self,
            )
            # Seed with the existing stop, if any.
            existing = t.get("stop_loss_price")
            if existing is not None:
                try:
                    self.stop_risk.set_stop_price(float(existing))
                except (TypeError, ValueError):
                    pass
            header_text = (
                f"<b>{t.get('symbol', '?')}</b> · "
                f"{t.get('direction', '')} · entry "
                f"${entry:.4f} × {shares:,} shares"
            )
        else:
            # Multi-trade: stop-only, no risk linking.
            self.stop_risk = StopRiskInputs(
                entry_price=0.0, shares=0, direction="Long",
                parent=self,
            )
            self.stop_risk.risk_spin.setEnabled(False)
            self.stop_risk.risk_spin.setSpecialValueText(
                "(N/A for multi-trade)"
            )
            header_text = (
                f"Applying a stop price to <b>{len(trades)}</b> trades. "
                "Risk-dollar linking is disabled because share counts "
                "and entry prices vary across the selection."
            )

        header = QLabel(header_text, self)
        header.setWordWrap(True)
        header.setObjectName("hintLabel")

        self.chk_clear = QCheckBox(
            "Clear the stop instead of setting one", self,
        )
        self.chk_clear.toggled.connect(self._on_clear_toggled)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)

        outer = QVBoxLayout(self)
        outer.addWidget(header)
        outer.addWidget(self.stop_risk)
        outer.addWidget(self.chk_clear)
        outer.addStretch()
        outer.addWidget(self._buttons)

        self.resize(420, 260)
        self._restore_geometry()

    # ---- slots ---------------------------------------------------------

    def _on_clear_toggled(self, checked: bool) -> None:
        self.stop_risk.setEnabled(not checked)

    # ---- result --------------------------------------------------------

    def chosen_stop_price(self) -> Optional[float]:
        """Return the stop to apply, or None to clear the stop."""
        if self.chk_clear.isChecked():
            return None
        return self.stop_risk.stop_price()
