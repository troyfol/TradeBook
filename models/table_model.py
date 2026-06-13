"""QAbstractTableModel adapter for the Trades tab."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor

COLOR_WIN = QColor("#00C853")
COLOR_LOSS = QColor("#FF1744")


class TradeTableModel(QAbstractTableModel):
    """Backed by a list of trade dicts returned from db_manager.

    Columns (key, label):
        entry_time / "Date"
        symbol     / "Symbol"
        direction  / "Direction"
        avg_entry_price  / "Entry $"
        avg_exit_price   / "Exit $"
        total_shares     / "Shares"
        gross_pnl        / "Gross P&L"
        net_pnl          / "Net P&L"
        total_commission / "Commission"
        hold_duration_seconds / "Hold"
    """

    COLUMNS: list[tuple[str, str]] = [
        ("entry_time", "Date"),
        ("symbol", "Symbol"),
        ("direction", "Direction"),
        ("avg_entry_price", "Entry $"),
        ("avg_exit_price", "Exit $"),
        ("stop_loss_price", "Stop $"),
        ("total_shares", "Shares"),
        ("gross_pnl", "Gross P&L"),
        ("net_pnl", "Net P&L"),
        ("total_commission", "Commission"),
        ("hold_duration_seconds", "Hold"),
    ]

    # Right-aligned columns (numeric).
    _RIGHT_ALIGNED = {
        "avg_entry_price", "avg_exit_price", "stop_loss_price",
        "total_shares", "gross_pnl", "net_pnl",
        "total_commission", "hold_duration_seconds",
    }

    def __init__(self, trades: list[dict] | None = None, parent=None):
        super().__init__(parent)
        self._rows: list[dict] = list(trades) if trades else []

    # --- data management ---------------------------------------------------

    def set_trades(self, trades: list[dict]) -> None:
        self.beginResetModel()
        self._rows = list(trades)
        self.endResetModel()

    def row_at(self, row_index: int) -> dict | None:
        if 0 <= row_index < len(self._rows):
            return self._rows[row_index]
        return None

    # --- Qt model API ------------------------------------------------------

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
            if key in ("gross_pnl", "net_pnl"):
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
            return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        return None

    # --- sorting -----------------------------------------------------------

    def sort(self, column: int, order: Qt.SortOrder = Qt.SortOrder.AscendingOrder) -> None:
        key = self.COLUMNS[column][0]
        reverse = order == Qt.SortOrder.DescendingOrder

        self.layoutAboutToBeChanged.emit()
        with_val = [r for r in self._rows if r.get(key) is not None]
        without_val = [r for r in self._rows if r.get(key) is None]
        with_val.sort(key=lambda r: r[key], reverse=reverse)
        # Open trades (None exit/pnl) always pinned at the bottom regardless of order.
        self._rows = with_val + without_val
        self.layoutChanged.emit()

    # --- formatting --------------------------------------------------------

    @staticmethod
    def _format(row: dict, key: str) -> str:
        val = row.get(key)
        if key == "entry_time":
            if val is None:
                return ""
            if isinstance(val, datetime):
                return val.strftime("%Y-%m-%d %H:%M")
            # fall back to string form if the driver gave us a raw str
            return str(val)[:16]

        if val is None:
            return "—"

        if key in ("avg_entry_price", "avg_exit_price", "stop_loss_price"):
            return f"{val:.4f}"
        if key in ("gross_pnl", "net_pnl", "total_commission"):
            sign = "-" if val < 0 else ""
            return f"{sign}${abs(val):,.2f}"
        if key == "total_shares":
            return f"{int(val):,}"
        if key == "hold_duration_seconds":
            return _format_duration(int(val))
        return str(val)


def _format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        m, s = divmod(seconds, 60)
        return f"{m}m {s}s"
    h, rem = divmod(seconds, 3600)
    m, _ = divmod(rem, 60)
    return f"{h}h {m}m"
