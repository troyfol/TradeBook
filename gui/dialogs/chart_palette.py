"""Per-card color customization dialog.

Takes a target ``ChartCard`` and edits that card's palette override.
"Restore Defaults" clears the override so the card reverts to the
TradeBook default palette (``DEFAULT_PALETTE``). Sibling cards are
never touched.
"""
from __future__ import annotations

from dataclasses import asdict

from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QColorDialog, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
    QLabel, QPushButton, QVBoxLayout, QWidget,
)

from gui.widgets.chart_palette import (
    DEFAULT_PALETTE, ChartPalette, get_palette,
)


_LABELS: list[tuple[str, str]] = [
    ("positive", "Positive values"),
    ("negative", "Negative values"),
    ("axis", "Axis lines"),
    ("label", "Labels & ticks"),
    ("background", "Background"),
]


class _Swatch(QWidget):
    """Color chip + hex label + Pick button."""

    def __init__(self, initial: str, parent=None):
        super().__init__(parent)
        self._color = initial
        self._chip = QLabel("", self)
        self._chip.setFixedSize(28, 20)
        self._chip.setAutoFillBackground(False)
        self._hex = QLabel(initial, self)
        self._hex.setStyleSheet("color: #b0b0b0; font-family: monospace;")
        self._btn = QPushButton("Pick…", self)
        self._btn.clicked.connect(self._pick)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        row.addWidget(self._chip)
        row.addWidget(self._hex, 1)
        row.addWidget(self._btn)
        self._refresh()

    def _refresh(self) -> None:
        self._chip.setStyleSheet(
            f"background: {self._color}; border: 1px solid #555;"
        )
        self._hex.setText(self._color)

    def color(self) -> str:
        return self._color

    def set_color(self, hex_str: str) -> None:
        self._color = hex_str
        self._refresh()

    def _pick(self) -> None:
        c = QColorDialog.getColor(
            QColor(self._color), self, "Pick color",
            QColorDialog.ColorDialogOption.DontUseNativeDialog,
        )
        if c.isValid():
            self.set_color(c.name())


class ChartPaletteDialog(QDialog):
    """Edit a single ChartCard's palette override.

    If the card has no override yet, the dialog is pre-populated with
    the global default so the user sees what they're starting from.
    Apply sets the override on the card; Restore Defaults clears it
    so the card inherits from the global again.
    """

    def __init__(self, *, card, parent=None):
        super().__init__(parent)
        self._card = card
        self.setWindowTitle(f"Customize colors — {card.title()}")
        self.setMinimumWidth(380)

        # Pre-populate with whatever this card is currently rendering
        # (override if set, else the global).
        starting = card.chart_palette_override() or get_palette()
        data = asdict(starting)
        self._swatches: dict[str, _Swatch] = {}

        form = QFormLayout()
        form.setSpacing(8)
        for key, label in _LABELS:
            sw = _Swatch(data[key], self)
            self._swatches[key] = sw
            form.addRow(QLabel(label), sw)

        hint = QLabel(
            "Changes apply to this widget only. "
            "<i>Restore Defaults</i> reverts to the TradeBook theme.",
            self,
        )
        hint.setStyleSheet("color: #909090; font-size: 9pt;")
        hint.setWordWrap(True)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.RestoreDefaults,
            parent=self,
        )
        btns.accepted.connect(self._apply)
        btns.rejected.connect(self.reject)
        btns.button(
            QDialogButtonBox.StandardButton.RestoreDefaults
        ).clicked.connect(self._restore_defaults)

        outer = QVBoxLayout(self)
        outer.addLayout(form)
        outer.addWidget(hint)
        outer.addWidget(btns)

    def _apply(self) -> None:
        new = ChartPalette(
            **{k: sw.color() for k, sw in self._swatches.items()}
        )
        self._card.set_chart_palette_override(new)
        self.accept()

    def _restore_defaults(self) -> None:
        # Visually reset the swatches to the TradeBook default so the
        # user sees what they'll get, and clear the override so the
        # card re-reads from the global.
        for key, sw in self._swatches.items():
            sw.set_color(getattr(DEFAULT_PALETTE, key))
        self._card.set_chart_palette_override(None)
