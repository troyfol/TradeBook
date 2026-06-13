"""Compact trade-list table model for the Journal tab.

Distinct from `TradeTableModel` so the Journal tab can show a narrower,
more scannable list (Date / Symbol / Direction / Net P&L + a 📝 indicator
column for trades that already have a journal entry) without affecting
the main Trades tab layout.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor

COLOR_WIN = QColor("#00C853")
COLOR_LOSS = QColor("#FF1744")


class JournalTradeModel(QAbstractTableModel):
    """Backed by trade dicts + a set of trade ids that have journal entries."""

    COLUMNS: list[tuple[str, str]] = [
        ("entry_time", "Date"),
        ("symbol", "Symbol"),
        ("direction", "Dir"),
        ("net_pnl", "Net P&L"),
        ("_journal", "📝"),
    ]

    _RIGHT_ALIGNED = {"net_pnl"}
    _CENTER_ALIGNED = {"_journal", "direction"}

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list[dict] = []
        self._journal_ids: set[int] = set()

    # ---- data management --------------------------------------------------

    def set_trades(self, trades: list[dict], journal_ids: set[int]) -> None:
        self.beginResetModel()
        self._rows = list(trades)
        self._journal_ids = set(journal_ids)
        self.endResetModel()

    def row_at(self, row_index: int) -> dict | None:
        if 0 <= row_index < len(self._rows):
            return self._rows[row_index]
        return None

    def find_row_by_id(self, trade_id: int) -> int:
        for i, r in enumerate(self._rows):
            if r.get("id") == trade_id:
                return i
        return -1

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

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        key = self.COLUMNS[index.column()][0]

        if role == Qt.ItemDataRole.DisplayRole:
            return self._format(row, key)

        if role == Qt.ItemDataRole.ForegroundRole:
            if key == "net_pnl":
                val = row.get(key)
                if val is None:
                    return None
                if val > 0:
                    return COLOR_WIN
                if val < 0:
                    return COLOR_LOSS
            return None

        if role == Qt.ItemDataRole.TextAlignmentRole:
            if key in self._RIGHT_ALIGNED:
                return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            if key in self._CENTER_ALIGNED:
                return int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)
            return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        return None

    # ---- formatting -------------------------------------------------------

    def _format(self, row: dict, key: str) -> str:
        if key == "_journal":
            return "📝" if row.get("id") in self._journal_ids else ""

        val = row.get(key)
        if key == "entry_time":
            if val is None:
                return ""
            if isinstance(val, datetime):
                return val.strftime("%Y-%m-%d %H:%M")
            return str(val)[:16]
        if val is None:
            return "—"
        if key == "net_pnl":
            sign = "-" if val < 0 else ""
            return f"{sign}${abs(val):,.2f}"
        return str(val)
