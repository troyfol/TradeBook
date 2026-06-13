"""Styled metric card — large value, small label, optional color accent."""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout


class StatCard(QFrame):
    """A framed panel that displays one numeric metric.

    Styled via QSS through objectName='StatCard'.
    Value font is sized large (~18pt bold) to dominate the card.
    Color of the value can be overridden per-metric (e.g. green/red for P&L).
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("StatCard")
        self.setFrameShape(QFrame.Shape.StyledPanel)

        self.value_label = QLabel("—", self)
        self.value_label.setObjectName("StatCardValue")
        self.value_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        vf = self.value_label.font()
        vf.setPointSizeF(18)
        vf.setBold(True)
        self.value_label.setFont(vf)

        self.name_label = QLabel("", self)
        self.name_label.setObjectName("StatCardLabel")
        self.name_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        nf = self.name_label.font()
        nf.setPointSizeF(9)
        self.name_label.setFont(nf)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(4)
        layout.addStretch()
        layout.addWidget(self.value_label)
        layout.addWidget(self.name_label)
        layout.addStretch()

        self.setMinimumHeight(92)
        self.setMinimumWidth(150)

    def set_data(
        self,
        label: str,
        value: str,
        color: Optional[str] = None,
    ) -> None:
        self.name_label.setText(label)
        self.value_label.setText(value)
        if color:
            self.value_label.setStyleSheet(
                f"QLabel#StatCardValue {{ color: {color}; }}"
            )
        else:
            self.value_label.setStyleSheet("")
