"""Small dialog for creating a new tag (name + color)."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QColorDialog, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton,
)


class NewTagDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New tag")
        self._color = "#42A5F5"  # default blue

        self.edit_name = QLineEdit(self)
        self.edit_name.setPlaceholderText("e.g. Mistake, EOD, Earnings…")

        self.btn_color = QPushButton("Pick…", self)
        self.btn_color.clicked.connect(self._on_pick_color)
        self.color_swatch = QLabel(self)
        self.color_swatch.setFixedSize(24, 24)
        self._refresh_swatch()

        color_row = QHBoxLayout()
        color_row.setContentsMargins(0, 0, 0, 0)
        color_row.addWidget(self.color_swatch)
        color_row.addWidget(self.btn_color)
        color_row.addStretch()

        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)

        form = QFormLayout(self)
        form.addRow("Name", self.edit_name)
        form.addRow("Color", color_row)
        form.addRow(self.button_box)

        self.edit_name.setFocus()

    # ---- public API --------------------------------------------------------

    def result_name_color(self) -> tuple[str, str]:
        return self.edit_name.text().strip(), self._color

    # ---- internals ---------------------------------------------------------

    def _on_pick_color(self) -> None:
        c = QColorDialog.getColor(QColor(self._color), self, "Tag color")
        if c.isValid():
            self._color = c.name()
            self._refresh_swatch()

    def _refresh_swatch(self) -> None:
        self.color_swatch.setStyleSheet(
            f"background-color: {self._color}; "
            f"border: 1px solid #555; border-radius: 3px;"
        )
