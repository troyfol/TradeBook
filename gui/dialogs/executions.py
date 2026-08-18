"""Manage Executions — direct view / edit / delete of raw fills.

Reached from the Trades tab. Every other surface in the app works at the
*trade* level, which left a gap: a fill the builder couldn't group (an
over-sell, an exit with no open position) belongs to no trade and so was
unreachable — even though the builder-warning dialog explicitly told the
user to "edit them in place or delete them". This dialog is where that
actually happens.

Ungrouped rows are tinted and can be isolated with a checkbox, since
they're the usual reason for opening this at all. Editing a fill that
*is* part of a trade is allowed but confirmed first — it reshapes an
existing trade's P&L, entry/exit prices, and share count.

Any accepted change runs ``rebuild_trades`` so the trades table reflects
the new fills immediately.
"""
from __future__ import annotations

import datetime as _dt
from typing import Any, Callable, Optional

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPoint, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMenu, QMessageBox, QPushButton,
    QTableView, QVBoxLayout,
)

from config import (
    TYPE_BUY, TYPE_SELL, TYPE_SELL_SHORT, TYPE_BUY_TO_COVER,
)
from ingest import db_manager
from ingest.trade_builder import (
    RESOLUTION_CLOSE_TO_PNL, RESOLUTION_DELETE_EXTRA, RESOLUTION_LEAVE_OPEN,
    RESOLUTION_LABELS,
)

# Contested / ungrouped fills get a muted amber wash — visible without
# shouting. "Contested" means a flip was answered with close_to_pnl or
# leave_open (reconciled, but not clean), or the user flagged it by hand.
_COLOR_UNGROUPED = QColor("#4a3a1e")

_VALID_TYPES = (TYPE_BUY, TYPE_SELL, TYPE_SELL_SHORT, TYPE_BUY_TO_COVER)

# Sentinel for a rejected inline edit.
_INVALID = object()


class ExecutionsModel(QAbstractTableModel):
    """Editable table over ``fetch_executions_with_links`` rows."""

    COLUMNS: list[tuple[str, str]] = [
        ("entered_at", "Entered"),
        ("type", "Type"),
        ("raw_symbol", "Symbol"),
        ("order_status", "Status"),
        ("filled_price", "Fill $"),
        ("qty_filled", "Qty Filled"),
        ("commission", "Commission"),
        ("trade_id", "Trade"),
    ]

    _EDITABLE = {
        "entered_at", "type", "raw_symbol", "filled_price",
        "qty_filled", "commission",
    }
    _RIGHT_ALIGNED = {"filled_price", "qty_filled", "commission", "trade_id"}

    def __init__(self, rows: Optional[list[dict]] = None, parent=None):
        super().__init__(parent)
        self._rows: list[dict] = list(rows or [])
        # execution_id -> {field: new_value} for edits not yet saved.
        self.pending: dict[int, dict] = {}

    # ---- data management --------------------------------------------------

    def set_rows(self, rows: list[dict]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.pending = {}
        self.endResetModel()

    def row_at(self, i: int) -> Optional[dict]:
        return self._rows[i] if 0 <= i < len(self._rows) else None

    # ---- Qt model API -----------------------------------------------------

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.COLUMNS)

    def headerData(self, section: int, orientation: Qt.Orientation,
                   role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return self.COLUMNS[section][1]
        return section + 1

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        base = super().flags(index)
        if not index.isValid():
            return base
        if self.COLUMNS[index.column()][0] in self._EDITABLE:
            return base | Qt.ItemFlag.ItemIsEditable
        return base

    def data(self, index: QModelIndex,
             role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        key = self.COLUMNS[index.column()][0]

        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            val = self._value(row, key)
            if role == Qt.ItemDataRole.EditRole:
                if key == "entered_at" and isinstance(val, _dt.datetime):
                    return val.strftime("%Y-%m-%d %H:%M:%S")
                return val
            return self._display(key, val)

        if role == Qt.ItemDataRole.BackgroundRole:
            if row.get("contested") or row.get("trade_id") is None:
                return _COLOR_UNGROUPED
            return None

        if role == Qt.ItemDataRole.TextAlignmentRole:
            if key in self._RIGHT_ALIGNED:
                return int(
                    Qt.AlignmentFlag.AlignRight
                    | Qt.AlignmentFlag.AlignVCenter
                )
            return int(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            )

        if role == Qt.ItemDataRole.ToolTipRole:
            bits: list[str] = []
            if row.get("trade_id") is None:
                bits.append(
                    "This fill isn't part of any trade — the builder "
                    "couldn't group it. Edit it so it balances, or "
                    "delete it."
                )
            else:
                ids = str(row.get("trade_ids") or row["trade_id"])
                label = "trades" if "," in ids else "trade"
                bits.append(f"Part of {label} #{ids.replace(',', ', #')}.")
            res = row.get("flip_resolution")
            if res:
                bits.append(
                    f"Unmatched shares resolved as: "
                    f"{RESOLUTION_LABELS.get(res, res)}."
                )
            if row.get("contested"):
                bits.append(
                    "Flagged as contested. Right-click to re-resolve it "
                    "or mark it uncontested."
                )
            return "\n".join(bits)
        return None

    def setData(self, index: QModelIndex, value: Any,
                role: int = Qt.ItemDataRole.EditRole) -> bool:
        if role != Qt.ItemDataRole.EditRole or not index.isValid():
            return False
        key = self.COLUMNS[index.column()][0]
        if key not in self._EDITABLE:
            return False
        parsed = _parse_edit(key, value)
        if parsed is _INVALID:
            return False
        row = self._rows[index.row()]
        if parsed == self._value(row, key):
            return False
        self.pending.setdefault(int(row["id"]), {})[key] = parsed
        self.dataChanged.emit(index, index)
        return True

    # ---- helpers ----------------------------------------------------------

    def _value(self, row: dict, key: str) -> Any:
        """Pending edit if there is one, otherwise the stored value."""
        edits = self.pending.get(int(row["id"]), {})
        return edits.get(key, row.get(key))

    def edited_ids(self) -> list[int]:
        return sorted(self.pending)

    @staticmethod
    def _display(key: str, val: Any) -> str:
        if val is None:
            return "—" if key == "trade_id" else ""
        if key == "entered_at":
            if isinstance(val, _dt.datetime):
                return val.strftime("%Y-%m-%d %H:%M:%S")
            return str(val)
        if key == "filled_price":
            return f"{float(val):.4f}"
        if key == "qty_filled":
            return f"{int(val):,}"
        if key == "commission":
            v = float(val)
            sign = "-" if v < 0 else ""
            return f"{sign}${abs(v):,.2f}"
        if key == "trade_id":
            return f"#{int(val)}"
        return str(val)


def _parse_edit(key: str, value: Any) -> Any:
    """Coerce an inline edit, or ``_INVALID`` to reject it."""
    s = str(value).strip() if value is not None else ""
    if key == "entered_at":
        for fmt in (
            "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
            "%Y-%m-%dT%H:%M:%S", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M",
        ):
            try:
                return _dt.datetime.strptime(s, fmt)
            except ValueError:
                continue
        return _INVALID
    if key == "type":
        for canon in _VALID_TYPES:
            if s.casefold() == canon.casefold():
                return canon
        return _INVALID
    if key in ("filled_price", "commission"):
        try:
            return float(s.lstrip("$").replace(",", ""))
        except ValueError:
            return _INVALID
    if key == "qty_filled":
        try:
            n = int(float(s.replace(",", "")))
        except ValueError:
            return _INVALID
        return n if n >= 0 else _INVALID
    if key == "raw_symbol":
        return s.upper()
    return s


class ExecutionsDialog(QDialog):
    """Browse / edit / delete raw executions, then rebuild trades."""

    def __init__(self, get_conn: Callable, parent=None,
                 ungrouped_only: bool = False,
                 trade_ids: Optional[list[int]] = None):
        super().__init__(parent)
        self._get_conn = get_conn
        # When the Trades tab has rows selected, only those trades' fills
        # are listed — the manager mirrors what's actually in the trade
        # section rather than showing an unrelated wall of executions.
        self._trade_ids = list(trade_ids) if trade_ids is not None else None
        self.setWindowTitle(
            "Manage executions"
            + (f" — {len(self._trade_ids)} selected trade(s)"
               if self._trade_ids else "")
        )
        self.resize(900, 520)
        # Set when the dialog mutated anything, so the caller knows to
        # refresh the trade views.
        self.mutated = False

        self.filter_edit = QLineEdit(self)
        self.filter_edit.setPlaceholderText("Filter by symbol…")
        self.filter_edit.textChanged.connect(self._reload)

        self.chk_ungrouped = QCheckBox("Ungrouped fills only", self)
        self.chk_ungrouped.setToolTip(
            "Show only fills that aren't part of any trade — the ones a "
            "trade-builder warning left behind."
        )
        self.chk_ungrouped.setChecked(ungrouped_only)
        self.chk_ungrouped.toggled.connect(self._reload)

        self.chk_contested = QCheckBox("Contested only", self)
        self.chk_contested.setToolTip(
            "Show only fills flagged as contested — ones whose unmatched "
            "shares you resolved as 'close to P&L' or 'leave as open', "
            "plus anything you flagged by hand."
        )
        self.chk_contested.toggled.connect(self._reload)

        top = QHBoxLayout()
        top.addWidget(self.filter_edit, 1)
        top.addWidget(self.chk_contested)
        top.addWidget(self.chk_ungrouped)

        self.model = ExecutionsModel(parent=self)
        self.table = QTableView(self)
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.table.customContextMenuRequested.connect(self._on_context_menu)

        self.lbl_status = QLabel("", self)
        self.lbl_status.setObjectName("hintLabel")

        self.btn_delete = QPushButton("Delete selected", self)
        self.btn_delete.clicked.connect(self._on_delete)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Close,
            parent=self,
        )
        self.buttons.button(
            QDialogButtonBox.StandardButton.Save
        ).setText("Save + rebuild")
        self.buttons.accepted.connect(self._on_save)
        self.buttons.rejected.connect(self.reject)

        bottom = QHBoxLayout()
        bottom.addWidget(self.btn_delete)
        bottom.addStretch()
        bottom.addWidget(self.buttons)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(QLabel(
            "Double-click a cell to edit. Amber rows aren't part of any "
            "trade.", self,
        ))
        layout.addWidget(self.table, 1)
        layout.addWidget(self.lbl_status)
        layout.addLayout(bottom)

        self._reload()

    # ---- data -------------------------------------------------------------

    def _reload(self) -> None:
        rows = db_manager.fetch_executions_with_links(
            self._get_conn(),
            ungrouped_only=self.chk_ungrouped.isChecked(),
            contested_only=self.chk_contested.isChecked(),
            symbol=self.filter_edit.text() or None,
            trade_ids=self._trade_ids,
        )
        self.model.set_rows(rows)
        n_orphan = sum(1 for r in rows if r.get("trade_id") is None)
        n_contested = sum(1 for r in rows if r.get("contested"))
        self.lbl_status.setText(
            f"{len(rows):,} execution(s) shown · {n_contested:,} contested "
            f"· {n_orphan:,} not grouped into any trade"
        )

    def _selected_rows(self) -> list[dict]:
        seen: dict[int, dict] = {}
        for idx in self.table.selectionModel().selectedIndexes():
            r = self.model.row_at(idx.row())
            if r is not None:
                seen[int(r["id"])] = r
        return list(seen.values())

    # ---- context menu -----------------------------------------------------

    def _on_context_menu(self, pos: QPoint) -> None:
        idx = self.table.indexAt(pos)
        if not idx.isValid():
            return
        row = self.model.row_at(idx.row())
        if row is None:
            return
        # Right-clicking outside the selection acts on the clicked row.
        if int(row["id"]) not in {int(r["id"]) for r in self._selected_rows()}:
            self.table.clearSelection()
            self.table.selectRow(idx.row())

        rows = self._selected_rows()
        menu = QMenu(self)

        if any(r.get("contested") for r in rows):
            act = menu.addAction("Mark as uncontested")
            act.triggered.connect(lambda: self._set_contested(rows, False))
        else:
            act = menu.addAction("Mark as contested")
            act.triggered.connect(lambda: self._set_contested(rows, True))

        # Re-resolving only makes sense for a single fill — each carries
        # its own share counts and the resolution rewrites that one row.
        if len(rows) == 1:
            menu.addSeparator()
            header = menu.addAction("Resolve unmatched shares:")
            header.setEnabled(False)
            for mode in (
                RESOLUTION_DELETE_EXTRA,
                RESOLUTION_LEAVE_OPEN,
                RESOLUTION_CLOSE_TO_PNL,
            ):
                a = menu.addAction(f"  {RESOLUTION_LABELS[mode]}")
                a.triggered.connect(
                    lambda _=False, m=mode, r=rows[0]: self._resolve(r, m)
                )

        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _set_contested(self, rows: list[dict], contested: bool) -> None:
        conn = self._get_conn()
        db_manager.set_execution_contested(
            conn, [int(r["id"]) for r in rows], contested,
        )
        self.mutated = True
        self._reload()

    def _resolve(self, row: dict, mode: str) -> None:
        """Re-answer a fill's unmatched shares, then rebuild."""
        conn = self._get_conn()
        # How many shares the position could actually absorb. For a fill
        # already trimmed by a previous delete_extra there's nothing left
        # to remove, so re-running it is a no-op rather than a wipe.
        flips = db_manager.detect_flips(conn)
        flip = next(
            (f for f in flips if f.import_hash == row["import_hash"]), None,
        )
        keep = flip.position_shares if flip else int(row["qty_filled"])

        if mode == RESOLUTION_DELETE_EXTRA and keep <= 0:
            if QMessageBox.question(
                self, "Delete fill",
                "No shares of this fill were matched by a position — "
                "resolving it this way deletes the fill entirely.\n\n"
                "Continue?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            ) != QMessageBox.StandardButton.Yes:
                return

        try:
            db_manager.apply_flip_resolution(
                conn, row["import_hash"], mode,
                keep_shares=keep, auto_commit=False,
            )
            db_manager.rebuild_trades(conn, auto_commit=False)
            conn.commit()
        except Exception as e:  # noqa: BLE001
            conn.rollback()
            QMessageBox.critical(
                self, "Could not resolve",
                f"{type(e).__name__}: {e}\n\nNo changes were made.",
            )
            return
        self.mutated = True
        self._reload()

    # ---- actions ----------------------------------------------------------

    def _on_delete(self) -> None:
        rows = self._selected_rows()
        if not rows:
            QMessageBox.information(
                self, "Nothing selected", "Select one or more fills first.",
            )
            return
        grouped = [r for r in rows if r.get("trade_id") is not None]
        msg = f"Delete {len(rows)} execution(s)?"
        if grouped:
            msg += (
                f"\n\n{len(grouped)} of them belong to an existing trade. "
                "Removing a fill changes that trade's share count and P&L, "
                "and may drop the trade entirely."
            )
        msg += "\n\nThis can't be undone from here."
        if QMessageBox.question(
            self, "Delete executions", msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return

        conn = self._get_conn()
        try:
            db_manager.delete_executions(
                conn, [int(r["id"]) for r in rows], auto_commit=False,
            )
            db_manager.rebuild_trades(conn, auto_commit=False)
            conn.commit()
        except Exception as e:  # noqa: BLE001 — surface anything to the user
            conn.rollback()
            QMessageBox.critical(
                self, "Delete failed",
                f"{type(e).__name__}: {e}\n\nNo changes were made.",
            )
            return
        self.mutated = True
        self._reload()

    def _on_save(self) -> None:
        if not self.model.pending:
            self.accept()
            return

        edited_ids = set(self.model.edited_ids())
        grouped = [
            r for r in self.model._rows
            if int(r["id"]) in edited_ids and r.get("trade_id") is not None
        ]
        msg = f"Apply {len(edited_ids)} edit(s) and rebuild trades?"
        if grouped:
            msg += (
                f"\n\n{len(grouped)} edited fill(s) belong to an existing "
                "trade. Saving will reshape that trade's entry/exit prices, "
                "share count, and P&L."
            )
        if QMessageBox.question(
            self, "Save executions", msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return

        conn = self._get_conn()
        try:
            for exec_id, fields in self.model.pending.items():
                db_manager.update_execution(
                    conn, exec_id, fields, auto_commit=False,
                )
            db_manager.rebuild_trades(conn, auto_commit=False)
            conn.commit()
        except Exception as e:  # noqa: BLE001
            conn.rollback()
            QMessageBox.critical(
                self, "Save failed",
                f"{type(e).__name__}: {e}\n\n"
                "No changes were saved. If two fills ended up identical, "
                "the edit collides with an existing row.",
            )
            return
        self.mutated = True
        self._reload()
        self.accept()
