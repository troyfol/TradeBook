"""Inline tag chip strip for the Journal tab.

Renders one removable chip per attached tag plus a `[+]` button that
opens a `TagPickerDialog`. Used by `JournalTab` underneath the editor.
"""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QToolButton,
    QWidget,
)


def _readable_text_color(bg_hex: str) -> str:
    """Pick black or white text for legibility against a colored chip."""
    c = QColor(bg_hex)
    if not c.isValid():
        return "#ffffff"
    # Relative luminance — sRGB approximation.
    lum = (0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()) / 255.0
    return "#000000" if lum > 0.6 else "#ffffff"


class TagChip(QFrame):
    """Single removable tag pill."""

    removed = Signal(int)  # tag_id

    def __init__(self, tag_id: int, name: str, color: str, parent=None):
        super().__init__(parent)
        self.tag_id = tag_id
        self.setObjectName("TagChip")
        text_color = _readable_text_color(color)
        self.setStyleSheet(
            f"QFrame#TagChip {{ background-color: {color}; "
            f"border-radius: 8px; padding: 0px; }}"
            f"QFrame#TagChip QLabel {{ color: {text_color}; "
            f"background: transparent; font-weight: bold; }}"
            f"QFrame#TagChip QToolButton {{ color: {text_color}; "
            f"background: transparent; border: none; font-weight: bold; }}"
            f"QFrame#TagChip QToolButton:hover {{ color: #FFCDD2; }}"
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 2, 4, 2)
        layout.setSpacing(4)
        self.label = QLabel(name, self)
        layout.addWidget(self.label)
        self.btn_x = QToolButton(self)
        self.btn_x.setText("✕")
        self.btn_x.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_x.clicked.connect(lambda: self.removed.emit(self.tag_id))
        layout.addWidget(self.btn_x)


class TagChipStrip(QWidget):
    """Editable strip of `TagChip`s + a `[+]` button.

    The strip owns no DB state — it's pure widgetry. Callers feed it tag
    info via `set_tags(...)` and connect to `tags_changed` to persist.
    """

    tags_changed = Signal(list)  # list[int] — current tag ids
    add_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._all_tags: list[dict] = []
        self._selected_ids: list[int] = []

        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(6)

        self.btn_add = QPushButton("+ Tag", self)
        self.btn_add.setFixedHeight(24)
        self.btn_add.clicked.connect(self.add_requested.emit)
        self.btn_add.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed,
        )

        self._stretch_added = False
        self._rebuild()

    # ---- public API --------------------------------------------------------

    def set_known_tags(self, tags: list[dict]) -> None:
        """Provide the master list of tag dicts (id, name, color)."""
        self._all_tags = list(tags)
        self._rebuild()

    def set_selected_ids(self, tag_ids: list[int]) -> None:
        self._selected_ids = list(tag_ids)
        self._rebuild()

    def selected_ids(self) -> list[int]:
        return list(self._selected_ids)

    # ---- internals ---------------------------------------------------------

    def _clear_layout(self) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            w = item.widget()
            if w is not None and w is not self.btn_add:
                w.setParent(None)
                w.deleteLater()

    def _rebuild(self) -> None:
        self._clear_layout()
        by_id = {t["id"]: t for t in self._all_tags}
        for tid in self._selected_ids:
            t = by_id.get(tid)
            if t is None:
                continue
            chip = TagChip(t["id"], t["name"], t["color"], self)
            chip.removed.connect(self._on_chip_removed)
            self._layout.addWidget(chip)
        self._layout.addWidget(self.btn_add)
        self._layout.addStretch(1)

    def _on_chip_removed(self, tag_id: int) -> None:
        if tag_id in self._selected_ids:
            self._selected_ids.remove(tag_id)
            self._rebuild()
            self.tags_changed.emit(self.selected_ids())
