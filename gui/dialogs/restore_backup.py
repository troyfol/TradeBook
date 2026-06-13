"""Restore-from-backup picker dialog.

Shows the available snapshots in ``backups/`` newest-first, with a
quick "trades in this snapshot" peek so the user can pick the right
one. Accepting calls back into the caller (MainWindow) which performs
the actual file swap + DB reconnect.
"""
from __future__ import annotations

import datetime as _dt
import sqlite3
from pathlib import Path
from typing import Optional

from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QHBoxLayout,
    QHeaderView, QLabel, QMessageBox, QTableWidget, QTableWidgetItem,
    QVBoxLayout,
)

from gui.dialogs._geometry import DialogGeometryMixin
from ingest import backups as backups_mod


def _peek_trade_count(path: Path) -> Optional[int]:
    """Open the snapshot read-only and count its trades. Returns None
    if the file isn't a valid SQLite DB or the trades table is missing."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM trades"
            ).fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()
    except sqlite3.Error:
        return None


class RestoreBackupDialog(DialogGeometryMixin, QDialog):
    GEOMETRY_KEY = "dialog/restore_backup/geometry"

    def __init__(
        self,
        auto_dir: Path,
        manual_dir: Path,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Restore from backup")
        self.setModal(True)

        self._auto_dir = Path(auto_dir)
        self._manual_dir = Path(manual_dir)
        self._chosen: Optional[Path] = None

        intro = QLabel(
            "Choose a snapshot to restore. The current database is "
            "snapshotted first so you can roll the restore back if it "
            "wasn't what you wanted.<br><br>"
            "<b>Auto</b> snapshots are taken by the app (startup / "
            "pre-import) and capped to the most recent few. "
            "<b>Manual</b> snapshots come from File → Back up now and "
            "are kept indefinitely.",
            self,
        )
        intro.setWordWrap(True)
        intro.setObjectName("hintLabel")

        self.table = QTableWidget(0, 4, self)
        self.table.setHorizontalHeaderLabels(
            ["Snapshot", "Type", "Taken", "Trades"],
        )
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers,
        )
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows,
        )
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection,
        )
        self.table.verticalHeader().setVisible(False)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        h.setStretchLastSection(False)
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)

        self._populate()

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        ok_btn = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok_btn.setText("Restore selected")
        self._buttons.accepted.connect(self._on_accept)
        self._buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.table, 1)
        layout.addWidget(self._buttons)

        self.resize(560, 380)
        self._restore_geometry()

    # ---- public --------------------------------------------------------

    def chosen_backup(self) -> Optional[Path]:
        return self._chosen

    # ---- internals -----------------------------------------------------

    def _populate(self) -> None:
        pairs = backups_mod.list_all_backups(
            self._auto_dir, self._manual_dir,
        )
        self.table.setRowCount(len(pairs))
        for r, (p, kind) in enumerate(pairs):
            try:
                mt = _dt.datetime.fromtimestamp(p.stat().st_mtime)
                taken = mt.strftime("%Y-%m-%d %H:%M:%S")
            except OSError:
                taken = "?"
            count = _peek_trade_count(p)
            count_str = "?" if count is None else str(count)

            name_item = QTableWidgetItem(p.name)
            # Stash the full path on column 0 for retrieval on accept.
            name_item.setData(0x0100, str(p))  # Qt.UserRole = 0x0100
            self.table.setItem(r, 0, name_item)
            self.table.setItem(
                r, 1, QTableWidgetItem(kind.capitalize()),
            )
            self.table.setItem(r, 2, QTableWidgetItem(taken))
            self.table.setItem(r, 3, QTableWidgetItem(count_str))

        if pairs:
            self.table.selectRow(0)

    def _on_accept(self) -> None:
        sel = self.table.selectionModel().selectedRows()
        if not sel:
            QMessageBox.information(
                self, "No selection", "Pick a snapshot to restore.",
            )
            return
        row = sel[0].row()
        item = self.table.item(row, 0)
        if item is None:
            return
        path = Path(str(item.data(0x0100) or ""))
        if not path.exists():
            QMessageBox.warning(
                self, "Snapshot missing",
                f"{path.name} no longer exists. Refresh and try again.",
            )
            return
        # Confirm — restore is destructive (overwrites current DB,
        # though the safety snapshot mitigates this).
        reply = QMessageBox.question(
            self, "Confirm restore",
            f"Restore {path.name}?\n\n"
            "Your current database will be snapshotted first, then "
            "replaced by the chosen backup. The app will reconnect "
            "to the restored database automatically.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._chosen = path
        self.accept()
