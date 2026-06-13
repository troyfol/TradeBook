"""P&L goal editor — daily / weekly / monthly / yearly targets stored
in QSettings and surfaced on the Dashboard."""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QLabel,
    QVBoxLayout,
)

from gui.dialogs._geometry import DialogGeometryMixin
from gui.settings_keys import (
    GOAL_DAILY, GOAL_MONTHLY, GOAL_WEEKLY, GOAL_YEARLY,
)


GOAL_KEYS = (
    ("Daily", GOAL_DAILY),
    ("Weekly", GOAL_WEEKLY),
    ("Monthly", GOAL_MONTHLY),
    ("Yearly", GOAL_YEARLY),
)


def load_goals(settings: Optional[QSettings]) -> dict[str, float]:
    """Return ``{key: amount_in_dollars}``. Missing keys default to 0."""
    out: dict[str, float] = {}
    if settings is None:
        return {k: 0.0 for _label, k in GOAL_KEYS}
    for _label, key in GOAL_KEYS:
        try:
            out[key] = float(settings.value(key, 0.0))
        except (TypeError, ValueError):
            out[key] = 0.0
    return out


class GoalsDialog(DialogGeometryMixin, QDialog):
    GEOMETRY_KEY = "dialog/goals/geometry"

    def __init__(self, settings: QSettings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("P&L goals")
        self.setModal(True)
        # Stored under a non-colliding name — ``DialogGeometryMixin``
        # exposes a method called ``_settings`` for geometry persistence,
        # so we can't shadow it with an instance attribute.
        self._store = settings

        self._spins: dict[str, QDoubleSpinBox] = {}
        form = QFormLayout()
        current = load_goals(settings)
        for label, key in GOAL_KEYS:
            sb = QDoubleSpinBox(self)
            sb.setRange(0.0, 1_000_000_000.0)
            sb.setDecimals(2)
            sb.setPrefix("$")
            sb.setSingleStep(50.0)
            sb.setValue(current.get(key, 0.0))
            sb.setSpecialValueText("(no goal)")
            form.addRow(f"{label} target:", sb)
            self._spins[key] = sb

        hint = QLabel(
            "Targets surface as progress bars on the Dashboard. Set any "
            "target to $0 to hide its bar.",
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

        self.resize(360, 260)
        self._restore_geometry()

    def _on_accept(self) -> None:
        for key, spin in self._spins.items():
            self._store.setValue(key, float(spin.value()))
        self._store.sync()
        self.accept()
