"""Tag picker dialog: pick from existing tags + create / delete tags.

Used by `JournalTab` (via the `+ Tag` button on the chip strip).

UI:
    * Checkable list of all tags. Color swatch + name per row.
    * `New tag…` button → opens `NewTagDialog` for name + color.
    * `Delete` button on each row removes the tag globally (with confirm).
    * Ok / Cancel — Ok returns the checked-id list to the caller.
"""
from __future__ import annotations

import sqlite3
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QListWidget, QListWidgetItem,
    QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from gui.dialogs._geometry import DialogGeometryMixin
from gui.dialogs.new_tag import NewTagDialog
from ingest import db_manager


def _swatch_icon(color_hex: str, size: int = 14) -> QIcon:
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    c = QColor(color_hex)
    if not c.isValid():
        c = QColor("#888888")
    p.setBrush(c)
    p.setPen(QColor("#000000"))
    p.drawEllipse(1, 1, size - 2, size - 2)
    p.end()
    return QIcon(pm)


class TagPickerDialog(DialogGeometryMixin, QDialog):
    """Lets the user toggle which tags are attached to a trade."""

    GEOMETRY_KEY = "dialog/tag_picker/geometry"

    def __init__(
        self,
        get_conn: Callable[[], sqlite3.Connection],
        currently_selected: list[int],
        parent=None,
    ):
        super().__init__(parent)
        self._get_conn = get_conn
        self._initial_selected = set(currently_selected)
        self._result_ids: list[int] = []

        self.setWindowTitle("Pick tags")
        self.setMinimumWidth(320)

        self.list = QListWidget(self)
        self.list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)

        self.btn_new = QPushButton("New tag…", self)
        self.btn_new.clicked.connect(self._on_new_tag)
        self.btn_delete = QPushButton("Delete selected", self)
        self.btn_delete.clicked.connect(self._on_delete_tag)

        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        self.button_box.accepted.connect(self._on_accept)
        self.button_box.rejected.connect(self.reject)

        top = QHBoxLayout()
        top.addWidget(self.btn_new)
        top.addWidget(self.btn_delete)
        top.addStretch()

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.list, 1)
        layout.addWidget(self.button_box)

        self._reload_list()
        self._restore_geometry()

    # ---- public ------------------------------------------------------------

    def selected_ids(self) -> list[int]:
        return list(self._result_ids)

    # ---- internals ---------------------------------------------------------

    def _reload_list(self) -> None:
        self.list.clear()
        tags = db_manager.fetch_all_tags(self._get_conn())
        for t in tags:
            item = QListWidgetItem(_swatch_icon(t["color"]), t["name"])
            item.setData(Qt.ItemDataRole.UserRole, t["id"])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            checked = t["id"] in self._initial_selected
            item.setCheckState(
                Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
            )
            self.list.addItem(item)

    def _on_new_tag(self) -> None:
        dlg = NewTagDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            name, color = dlg.result_name_color()
            if not name:
                return
            try:
                new_id = db_manager.create_tag(self._get_conn(), name, color)
            except sqlite3.IntegrityError:
                QMessageBox.warning(
                    self, "Duplicate tag",
                    f"A tag named {name!r} already exists.",
                )
                return
            self._initial_selected.add(new_id)
            self._reload_list()

    def _on_delete_tag(self) -> None:
        item = self.list.currentItem()
        if item is None:
            return
        tag_id = item.data(Qt.ItemDataRole.UserRole)
        name = item.text()
        reply = QMessageBox.question(
            self, "Delete tag",
            f"Delete the tag {name!r} globally?\n\n"
            f"It will be removed from every trade that currently uses it. "
            f"This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        db_manager.delete_tag(self._get_conn(), int(tag_id))
        self._initial_selected.discard(int(tag_id))
        self._reload_list()

    def _on_accept(self) -> None:
        ids: list[int] = []
        for i in range(self.list.count()):
            item = self.list.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                ids.append(int(item.data(Qt.ItemDataRole.UserRole)))
        self._result_ids = ids
        self.accept()
