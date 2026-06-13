"""R-multiple configuration dialog.

Only one knob today: the default dollar amount treated as 1R for
trades that lack a recorded stop-loss price. When set to a positive
value, trades with no stop still contribute to the By R-Multiple
report (using ``net_pnl / default_risk`` as their R). At $0 the setting
is disabled and those trades are excluded as before.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QLabel,
    QVBoxLayout,
)

from gui.dialogs._geometry import DialogGeometryMixin
from gui.settings_keys import RMULTIPLE_DEFAULT_RISK


def load_default_risk(settings: Optional[QSettings]) -> float:
    """Return the saved default risk in dollars, or 0.0 if unset."""
    if settings is None:
        return 0.0
    try:
        return float(settings.value(RMULTIPLE_DEFAULT_RISK, 0.0))
    except (TypeError, ValueError):
        return 0.0


class RMultipleSettingsDialog(DialogGeometryMixin, QDialog):
    GEOMETRY_KEY = "dialog/r_multiple_settings/geometry"

    def __init__(self, settings: QSettings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("R-multiple settings")
        self.setModal(True)
        # Don't name this ``_settings`` — it'd shadow
        # DialogGeometryMixin._settings (a method).
        self._store = settings

        self.default_risk_spin = QDoubleSpinBox(self)
        self.default_risk_spin.setRange(0.0, 1_000_000.0)
        self.default_risk_spin.setDecimals(2)
        self.default_risk_spin.setPrefix("$")
        self.default_risk_spin.setSingleStep(25.0)
        self.default_risk_spin.setSpecialValueText("(disabled)")
        self.default_risk_spin.setValue(load_default_risk(settings))

        form = QFormLayout()
        form.addRow("Default risk per trade:", self.default_risk_spin)

        hint = QLabel(
            "Applies to <b>every trade</b> that doesn't have its own "
            "stop-loss price — R becomes <code>net_pnl / "
            "default_risk</code>. "
            "A per-trade stop (set via right-click → Set stop loss… or "
            "entered while adding a manual trade) always overrides the "
            "default.<br><br>"
            "Set to $0 to disable — trades without a stop are then "
            "excluded from the By R-Multiple report. "
            "Typical values: $50–$500 depending on account size.",
            self,
        )
        hint.setWordWrap(True)
        hint.setObjectName("hintLabel")

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

        self.resize(420, 240)
        self._restore_geometry()

    def _on_accept(self) -> None:
        self._store.setValue(
            RMULTIPLE_DEFAULT_RISK, float(self.default_risk_spin.value()),
        )
        self._store.sync()
        self.accept()
