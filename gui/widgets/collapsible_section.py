"""Reusable collapsible container with a click-to-toggle header.

Each section has a title row (▼/▶ chevron + title text + optional
right-side widget) and a content area below. The content area is
shown / hidden via ``setVisible`` so collapsed sections take zero
vertical space.

Optional QSettings persistence: pass ``settings_key`` to remember the
expanded / collapsed state across launches.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QSettings, Qt, Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QSizePolicy, QToolButton, QVBoxLayout, QWidget,
)


_CHEVRON_OPEN = "▼"
_CHEVRON_CLOSED = "▶"


class CollapsibleSection(QFrame):
    """A toggle-header + content area you can hide/show with one click.

    Usage:
        sec = CollapsibleSection("My charts", parent=self)
        sec.set_content(my_widget)
        sec.set_expanded(True)
    """

    toggled = Signal(bool)  # True = expanded

    def __init__(
        self,
        title: str,
        parent: Optional[QWidget] = None,
        *,
        settings: Optional[QSettings] = None,
        settings_key: Optional[str] = None,
    ):
        super().__init__(parent)
        self.setObjectName("CollapsibleSection")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self._settings = settings
        self._settings_key = settings_key

        self._toggle_btn = QToolButton(self)
        self._toggle_btn.setText(f"{_CHEVRON_OPEN}  {title}")
        self._toggle_btn.setCheckable(True)
        self._toggle_btn.setChecked(True)
        self._toggle_btn.setAutoRaise(True)
        self._toggle_btn.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon,
        )
        self._toggle_btn.setSizePolicy(
            QSizePolicy.Policy.MinimumExpanding, QSizePolicy.Policy.Fixed,
        )
        self._toggle_btn.setStyleSheet(
            "QToolButton { background: transparent; border: none; "
            "color: #e0e0e0; font-size: 11pt; font-weight: bold; "
            "padding: 4px 2px; text-align: left; } "
            "QToolButton:hover { color: #ffffff; }"
        )
        self._toggle_btn.clicked.connect(self._on_toggled)

        self._title_text = title

        # Header row: chevron + title (left) + optional right-side slot.
        self._header_layout = QHBoxLayout()
        self._header_layout.setContentsMargins(0, 0, 0, 0)
        self._header_layout.setSpacing(6)
        self._header_layout.addWidget(self._toggle_btn, 1)

        # Content host. The actual user widget gets added via set_content.
        self._content_host = QFrame(self)
        self._content_layout = QVBoxLayout(self._content_host)
        self._content_layout.setContentsMargins(0, 4, 0, 0)
        self._content_layout.setSpacing(0)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addLayout(self._header_layout)
        outer.addWidget(self._content_host)

        # Restore saved state if a key was provided.
        if self._settings is not None and self._settings_key:
            saved = self._settings.value(self._settings_key, True)
            if isinstance(saved, str):
                saved = saved.lower() in ("true", "1", "yes", "on")
            self.set_expanded(bool(saved), emit=False)

    # ---- public --------------------------------------------------------

    def set_content(self, widget: QWidget) -> None:
        """Install (or replace) the content widget."""
        # Clear existing.
        while self._content_layout.count():
            item = self._content_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
        self._content_layout.addWidget(widget)

    def add_header_widget(self, widget: QWidget) -> None:
        """Tuck a widget on the right side of the header row."""
        self._header_layout.addWidget(widget)

    def set_expanded(self, expanded: bool, *, emit: bool = True) -> None:
        self._toggle_btn.setChecked(expanded)
        self._content_host.setVisible(expanded)
        chev = _CHEVRON_OPEN if expanded else _CHEVRON_CLOSED
        self._toggle_btn.setText(f"{chev}  {self._title_text}")
        if self._settings is not None and self._settings_key:
            self._settings.setValue(self._settings_key, bool(expanded))
        if emit:
            self.toggled.emit(expanded)

    def is_expanded(self) -> bool:
        return self._toggle_btn.isChecked()

    # ---- internals -----------------------------------------------------

    def _on_toggled(self, checked: bool) -> None:
        self.set_expanded(checked)
