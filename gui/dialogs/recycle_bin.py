"""Recycle bin viewer for soft-deleted trades.

Lists the contents of ``deleted_trades`` newest-first with restore +
purge actions. Old entries (>30 days) are auto-purged on app startup
so this view stays manageable.
"""
from __future__ import annotations

import datetime as _dt
import sqlite3
from typing import Callable, Optional

from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QHBoxLayout,
    QHeaderView, QLabel, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout,
)

from gui.dialogs._geometry import DialogGeometryMixin
from ingest import db_manager


def _fmt_when(v) -> str:
    if isinstance(v, _dt.datetime):
        return v.strftime("%Y-%m-%d %H:%M")
    if isinstance(v, str):
        return v
    return ""


def _fmt_pnl(v) -> str:
    if v is None:
        return "—"
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "—"
    sign = "-" if v < 0 else ""
    return f"{sign}${abs(v):,.2f}"


class RecycleBinDialog(DialogGeometryMixin, QDialog):
    GEOMETRY_KEY = "dialog/recycle_bin/geometry"

    def __init__(
        self,
        get_conn: Callable[[], sqlite3.Connection],
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Recycle bin")
        self.setModal(True)
        self._get_conn = get_conn
        self._restored_ids: list[int] = []

        intro = QLabel(
            "Trades you've deleted in the last 30 days. Restore puts the "
            "trade (and its journal + tags) back into the active list. "
            "Purge removes it permanently.",
            self,
        )
        intro.setWordWrap(True)
        intro.setObjectName("hintLabel")

        self.table = QTableWidget(0, 5, self)
        self.table.setHorizontalHeaderLabels([
            "Deleted", "Symbol", "Direction", "Entry time", "Net P&L",
        ])
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows,
        )
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection,
        )
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers,
        )
        self.table.verticalHeader().setVisible(False)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        h.setStretchLastSection(True)

        self.btn_restore = QPushButton("Restore selected", self)
        self.btn_restore.clicked.connect(self._on_restore)

        self.btn_purge = QPushButton("Purge selected", self)
        self.btn_purge.clicked.connect(self._on_purge)

        action_row = QHBoxLayout()
        action_row.addWidget(self.btn_restore)
        action_row.addWidget(self.btn_purge)
        action_row.addStretch()

        close_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Close, parent=self,
        )
        close_box.rejected.connect(self.reject)
        close_box.button(
            QDialogButtonBox.StandardButton.Close
        ).clicked.connect(self.accept)

        outer = QVBoxLayout(self)
        outer.addWidget(intro)
        outer.addWidget(self.table, 1)
        outer.addLayout(action_row)
        outer.addWidget(close_box)

        self._reload()
        self.resize(640, 420)
        self._restore_geometry()

    # ---- public --------------------------------------------------------

    def restored_trade_ids(self) -> list[int]:
        """New trade ids created by the user's Restore actions."""
        return list(self._restored_ids)

    # ---- internals -----------------------------------------------------

    def _reload(self) -> None:
        rows = db_manager.fetch_deleted_trades(self._get_conn())
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            id_item = QTableWidgetItem(_fmt_when(row.get("deleted_at")))
            # Stash the deleted_trades.id on column 0 for retrieval later.
            id_item.setData(0x0100, int(row["id"]))  # Qt.UserRole
            self.table.setItem(r, 0, id_item)
            self.table.setItem(
                r, 1, QTableWidgetItem(str(row.get("symbol") or "")),
            )
            self.table.setItem(
                r, 2, QTableWidgetItem(str(row.get("direction") or "")),
            )
            self.table.setItem(
                r, 3, QTableWidgetItem(_fmt_when(row.get("entry_time"))),
            )
            self.table.setItem(
                r, 4, QTableWidgetItem(_fmt_pnl(row.get("net_pnl"))),
            )

    def _selected_deleted_ids(self) -> list[int]:
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        out: list[int] = []
        for r in rows:
            item = self.table.item(r, 0)
            if item is None:
                continue
            try:
                out.append(int(item.data(0x0100)))
            except (TypeError, ValueError):
                continue
        return out

    def _on_restore(self) -> None:
        ids = self._selected_deleted_ids()
        if not ids:
            return
        conn = self._get_conn()
        # Track corrupt rows so we can offer a single purge prompt at the
        # end instead of nagging the user once per bad row in a bulk
        # restore.
        corrupt: list[int] = []
        for did in ids:
            try:
                new_id = db_manager.restore_deleted_trade(conn, did)
            except db_manager.CorruptSnapshotError:
                corrupt.append(did)
                continue
            if new_id is not None:
                self._restored_ids.append(new_id)
        if corrupt:
            self._offer_purge_corrupt(corrupt)
        self._reload()

    def _offer_purge_corrupt(self, deleted_ids: list[int]) -> None:
        """Surface unrecoverable snapshots with an opt-in purge.

        We never auto-delete — the user should know what's going away,
        even when the row is unrecoverable.
        """
        n = len(deleted_ids)
        word = "entry" if n == 1 else "entries"
        reply = QMessageBox.question(
            self, "Corrupt snapshot",
            (
                f"{n} recycle-bin {word} can't be restored — the stored "
                "snapshot is corrupt (likely an interrupted delete).\n\n"
                "Purge the unrecoverable rows? They'll otherwise stay "
                "in the bin and continue to fail on every restore "
                "attempt."
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        conn = self._get_conn()
        for did in deleted_ids:
            db_manager.purge_deleted_trade(conn, did)

    def _on_purge(self) -> None:
        ids = self._selected_deleted_ids()
        if not ids:
            return
        reply = QMessageBox.question(
            self, "Purge permanently",
            f"Permanently delete {len(ids)} entry/entries from the "
            "recycle bin? This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        conn = self._get_conn()
        for did in ids:
            db_manager.purge_deleted_trade(conn, did)
        self._reload()
