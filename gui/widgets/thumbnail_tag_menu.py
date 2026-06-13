"""Right-click ``Tags ▸`` submenu for thumbnail-eligible images.

Shared between the JournalEditor's image right-click (in the Briefs and
Strategies tabs) and the ImageThumbStrip's per-thumbnail right-click.
The same menu shape backs both surfaces so the user sees a single
consistent control wherever they tag an image.

The submenu carries:

* one checkable action per preset (toggle assignment for this image),
* ``Add new tag…`` (creates a new preset and assigns it to the image),
* ``Manage tags…`` (rename/delete preset dialog).

When the host indicates the image is not currently pinned to the
thumbnail strip (``is_pinned=False``), the entire submenu is disabled
so the user can't tag an image that won't show in the strip. The
disabled-state hint reminds them to pin the image first.

The helper is pure UI plumbing — it never touches the DB. It calls back
through the supplied ``on_toggle`` / ``on_add_new`` / ``on_manage``
callbacks so the host tab can perform the DB mutation, refresh local
state, and broadcast preset changes to the other tab.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Sequence

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QInputDialog, QLineEdit,
    QListWidget, QListWidgetItem, QMenu, QMessageBox, QPushButton,
    QVBoxLayout, QWidget,
)

from ingest import db_manager


@dataclass(frozen=True)
class ThumbTag:
    """View-model for a thumbnail tag preset.

    Mirrors the columns returned by ``fetch_thumbnail_tags`` but lives
    in the GUI layer so this module doesn't have to import db_manager.
    """
    id: int
    name: str


def build_thumbnail_tag_submenu(
    parent_menu: QMenu,
    *,
    is_pinned: bool,
    current_tag_ids: Iterable[int],
    all_tags: Sequence[ThumbTag],
    on_toggle: Callable[[int, bool], None],
    on_add_new: Callable[[str], None],
    on_manage: Callable[[], None],
) -> QMenu:
    """Append a ``Tags ▸`` submenu onto ``parent_menu`` and return it.

    Parameters
    ----------
    parent_menu:
        The QMenu (e.g. the image right-click menu) that should host
        the submenu.
    is_pinned:
        ``True`` if the image is currently in the document's thumbnail
        opt-in set. When ``False`` every entry in the submenu is
        disabled and the submenu's title carries a clarifying suffix.
    current_tag_ids:
        Tag ids currently assigned to this image on this doc.
    all_tags:
        Every preset in display order.
    on_toggle:
        Invoked when the user toggles a checkable preset entry. Called
        as ``on_toggle(tag_id, new_state)``.
    on_add_new:
        Invoked after the user supplies a non-empty name via the
        ``Add new tag…`` prompt. Called as ``on_add_new(name)``.
    on_manage:
        Invoked when the user picks ``Manage tags…``.
    """
    title = "Tags" if is_pinned else "Tags (pin to thumbnails first)"
    submenu = parent_menu.addMenu(title)
    submenu.setEnabled(is_pinned)

    current = {int(v) for v in current_tag_ids}

    if not all_tags:
        empty = submenu.addAction("(no tags yet)")
        empty.setEnabled(False)
    else:
        for tag in all_tags:
            act = submenu.addAction(tag.name)
            act.setCheckable(True)
            act.setChecked(tag.id in current)
            # Use the action's ``toggled`` signal so the new state lands
            # in the callback rather than the pre-click state.
            act.toggled.connect(
                lambda checked, tid=int(tag.id):
                on_toggle(tid, bool(checked))
            )

    submenu.addSeparator()

    add_act = submenu.addAction("Add new tag…")
    add_act.triggered.connect(
        lambda: _prompt_for_new_tag(submenu, on_add_new)
    )

    manage_act = submenu.addAction("Manage tags…")
    manage_act.triggered.connect(on_manage)

    return submenu


def _prompt_for_new_tag(
    parent: QWidget,
    on_add_new: Callable[[str], None],
) -> None:
    name, ok = QInputDialog.getText(
        parent, "New thumbnail tag", "Tag name:", QLineEdit.EchoMode.Normal,
    )
    if not ok:
        return
    cleaned = (name or "").strip()
    if not cleaned:
        return
    on_add_new(cleaned)


# ---- Manage Tags dialog --------------------------------------------------


class ManageThumbnailTagsDialog(QDialog):
    """Compact rename / delete editor for thumbnail tag presets.

    Takes a sqlite connection directly and calls ``db_manager`` for all
    mutations — keeps the dialog self-contained (no closures over the
    parent tab's helpers, which would dangle if the tab were torn down
    while the dialog is open). The dialog also tracks whether any
    mutation happened via ``mutated`` so the calling tab can decide
    whether to refresh its own state + broadcast a cross-tab preset
    change after ``exec()`` returns.
    """

    def __init__(
        self,
        parent: Optional[QWidget],
        conn: sqlite3.Connection,
    ):
        super().__init__(parent)
        self.setWindowTitle("Manage thumbnail tags")
        self.setMinimumWidth(320)
        self._conn = conn
        # Flipped to True as soon as any rename / delete / add lands.
        # Callers read this after exec() to know whether to broadcast
        # ``thumbnail_tag_presets_changed`` and re-fetch their local
        # tag map (cascade may have dropped link rows).
        self.mutated: bool = False

        self.list = QListWidget(self)
        self.list.itemDoubleClicked.connect(self._rename_selected)

        btn_rename = QPushButton("Rename…", self)
        btn_rename.clicked.connect(self._rename_selected)
        btn_delete = QPushButton("Delete", self)
        btn_delete.clicked.connect(self._delete_selected)
        btn_add = QPushButton("Add new…", self)
        btn_add.clicked.connect(self._add_new)

        side = QVBoxLayout()
        side.setContentsMargins(0, 0, 0, 0)
        side.setSpacing(6)
        side.addWidget(btn_add)
        side.addWidget(btn_rename)
        side.addWidget(btn_delete)
        side.addStretch()

        body = QHBoxLayout()
        body.setSpacing(8)
        body.addWidget(self.list, 1)
        body.addLayout(side)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Close, parent=self,
        )
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)

        outer = QVBoxLayout(self)
        outer.addLayout(body, 1)
        outer.addWidget(buttons)

        self._reload_into_list()

    # ---- internal ----------------------------------------------------

    def _reload_into_list(self) -> None:
        self.list.clear()
        for row in db_manager.fetch_thumbnail_tags(self._conn):
            item = QListWidgetItem(row["name"], self.list)
            item.setData(Qt.ItemDataRole.UserRole, int(row["id"]))

    def _selected_tag(self) -> Optional[ThumbTag]:
        item = self.list.currentItem()
        if item is None:
            return None
        return ThumbTag(
            id=int(item.data(Qt.ItemDataRole.UserRole)),
            name=item.text(),
        )

    def _rename_selected(self) -> None:
        tag = self._selected_tag()
        if tag is None:
            return
        new_name, ok = QInputDialog.getText(
            self, "Rename tag", "New name:",
            QLineEdit.EchoMode.Normal, tag.name,
        )
        if not ok:
            return
        cleaned = (new_name or "").strip()
        if not cleaned or cleaned == tag.name:
            return
        try:
            ok2 = db_manager.rename_thumbnail_tag(
                self._conn, tag.id, cleaned,
            )
        except ValueError:
            return  # empty names guard at the db layer; ignored
        if not ok2:
            QMessageBox.warning(
                self, "Rename failed",
                f"A tag named “{cleaned}” already exists.",
            )
            return
        self.mutated = True
        self._reload_into_list()

    def _delete_selected(self) -> None:
        tag = self._selected_tag()
        if tag is None:
            return
        confirm = QMessageBox.question(
            self, "Delete tag",
            (f"Delete the tag “{tag.name}”?\n\n"
             "Every thumbnail currently carrying this tag will lose it. "
             "The thumbnail itself is not removed."),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        if db_manager.delete_thumbnail_tag(self._conn, tag.id):
            self.mutated = True
        self._reload_into_list()

    def _add_new(self) -> None:
        name, ok = QInputDialog.getText(
            self, "New tag", "Tag name:", QLineEdit.EchoMode.Normal,
        )
        if not ok:
            return
        cleaned = (name or "").strip()
        if not cleaned:
            return
        try:
            db_manager.add_thumbnail_tag(self._conn, cleaned)
        except ValueError:
            return
        self.mutated = True
        self._reload_into_list()
