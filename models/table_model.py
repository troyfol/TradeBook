"""QAbstractTableModel adapter for the Trades tab."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor

COLOR_WIN = QColor("#00C853")
COLOR_LOSS = QColor("#FF1744")
# Muted tones for realized P&L on a still-open (partially closed) trade —
# dimmer than the finalized win/loss colors so a provisional figure never
# reads as a booked result.
COLOR_REALIZED_WIN = QColor("#4E9E68")
COLOR_REALIZED_LOSS = QColor("#B0576A")


def _open_shares(row: dict) -> int:
    """Remaining open shares for a trade = total minus already-closed."""
    total = int(row.get("total_shares") or 0)
    closed = int(row.get("closed_shares") or 0)
    return max(total - closed, 0)


def _is_partial_open(row: dict) -> bool:
    """True when this open trade has been partially scaled out of."""
    if row.get("net_pnl") is not None or row.get("exit_time") is not None:
        return False
    return (
        int(row.get("closed_shares") or 0) > 0
        and row.get("realized_net_pnl") is not None
    )


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
                    # Provisional realized P&L on a partially-closed open
                    # trade — dimmer so it reads as "not booked yet".
                    if _is_partial_open(row):
                        rkey = ("realized_pnl" if key == "gross_pnl"
                                else "realized_net_pnl")
                        rval = row.get(rkey)
                        if rval is not None and rval > 0:
                            return COLOR_REALIZED_WIN
                        if rval is not None and rval < 0:
                            return COLOR_REALIZED_LOSS
                    return None
                if val > 0:
                    return COLOR_WIN
                if val < 0:
                    return COLOR_LOSS
            return None

        if role == Qt.ItemDataRole.ToolTipRole:
            if key == "stop_loss_price" and row.get("stop_is_derived"):
                return (
                    "Auto-filled at import from this trade's realized "
                    "loss, not a stop you planned — so it scores exactly "
                    "-1R by construction.\nTick “Planned stops only” on "
                    "the By R-Multiple report to exclude these, or set a "
                    "real stop via right-click → Set stop loss…"
                )
            if _is_partial_open(row) and key in (
                "gross_pnl", "net_pnl", "total_shares",
            ):
                total = int(row.get("total_shares") or 0)
                closed = int(row.get("closed_shares") or 0)
                return (
                    f"Realized on {closed:,} of {total:,} shares closed. "
                    f"{_open_shares(row):,} still open — full P&L posts "
                    "when the position fully closes."
                )
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
        # Sort on (type-name, value) so a column holding mixed types can't
        # raise. entry_time in particular can arrive as either a datetime
        # or a raw str depending on the sqlite converter — see _format's
        # fallback — and comparing the two is a TypeError that would take
        # down the whole tab on a header click.
        with_val.sort(
            key=lambda r: (type(r[key]).__name__, r[key]), reverse=reverse,
        )
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

        # Partially-closed open trade: show the realized P&L on the closed
        # slice (marked with * so it reads as provisional) and the open
        # remaining share count instead of the raw total.
        if _is_partial_open(row):
            if key in ("gross_pnl", "net_pnl"):
                rkey = ("realized_pnl" if key == "gross_pnl"
                        else "realized_net_pnl")
                rval = row.get(rkey)
                if rval is not None:
                    sign = "-" if rval < 0 else ""
                    return f"~{sign}${abs(rval):,.2f}*"
            if key == "total_shares":
                total = int(row.get("total_shares") or 0)
                return f"{_open_shares(row):,} (of {total:,})"

        if val is None:
            return "—"

        if key == "stop_loss_price":
            # Mark stops that were back-filled at import from the trade's
            # own realized loss rather than planned by the user — those
            # are -1R by construction, so they shouldn't read as a real
            # planned stop at a glance.
            suffix = "*" if row.get("stop_is_derived") else ""
            return f"{val:.4f}{suffix}"
        if key in ("avg_entry_price", "avg_exit_price"):
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
    """Mirror ``analytics.metrics._fmt_hold`` exactly.

    Without the day tier, a multiday hold rendered here as "265h 35m"
    while the dashboard stat card called the same value "11d 1h".
    """
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        m, s = divmod(seconds, 60)
        return f"{m}m {s}s"
    if seconds < 86400:
        h, rem = divmod(seconds, 3600)
        m, _ = divmod(rem, 60)
        return f"{h}h {m}m"
    d, rem = divmod(seconds, 86400)
    h, _ = divmod(rem, 3600)
    return f"{d}d {h}h"
