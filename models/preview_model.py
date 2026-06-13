"""Table model for the New Trade paste preview.

Each row is a PreviewRow in one of three states:
    new        — will be inserted on Import
    duplicate  — already in executions (by import_hash); will be skipped
    error      — parse failure; will be skipped

Row background colors communicate state at a glance; tooltips on DUP rows
surface the matching existing execution so the user can verify a suspicious
dedup hit, and tooltips on ERROR rows expose the parse error and raw line.

Phase 12: cells on NEW rows are editable inline (double-click). Edits
update the underlying execution dict and refresh its ``import_hash``
so the Import flow's dedup stays correct. Risk is NOT entered here —
that's a trade-level concept handled on the Trades tab after the
trade builder has grouped fills into their logical trades.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor

from ingest.tradestation_parser import compute_import_hash

STATE_NEW = "new"
STATE_DUPLICATE = "duplicate"
STATE_ERROR = "error"

_COLOR_NEW = QColor("#0d4f2c")       # muted dark green
_COLOR_DUPLICATE = QColor("#3a3a3a")  # neutral gray
_COLOR_ERROR = QColor("#5a1e1e")      # muted dark red


@dataclass
class PreviewRow:
    state: str
    execution: Optional[dict] = None      # parsed execution dict (new / duplicate)
    error_message: Optional[str] = None   # parse error text (error)
    raw_line: Optional[str] = None        # original raw line (error)
    existing_match: Optional[dict] = None  # matching DB row (duplicate)


class PreviewTableModel(QAbstractTableModel):
    COLUMNS: list[tuple[str, str]] = [
        ("status", "Status"),
        ("entered_at", "Date"),
        ("type", "Type"),
        ("raw_symbol", "Symbol"),
        ("order_status", "Order Status"),
        ("filled_price", "Filled Price"),
        ("qty_filled", "Qty Filled"),
        ("commission", "Commission"),
    ]

    # Cells users can edit inline on NEW rows. (Status / order_status
    # are computed; editing them would just confuse dedup semantics.)
    _EDITABLE = {
        "entered_at", "type", "raw_symbol", "filled_price",
        "qty_filled", "commission",
    }

    _RIGHT_ALIGNED = {"filled_price", "qty_filled", "commission"}
    _STATUS_LABEL = {STATE_NEW: "NEW", STATE_DUPLICATE: "DUP", STATE_ERROR: "ERROR"}

    def __init__(self, rows: list[PreviewRow] | None = None, parent=None):
        super().__init__(parent)
        self._rows: list[PreviewRow] = list(rows) if rows else []

    # ---- data management --------------------------------------------------

    def set_rows(self, rows: list[PreviewRow]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def row_at(self, index: int) -> PreviewRow | None:
        if 0 <= index < len(self._rows):
            return self._rows[index]
        return None

    def rows(self) -> list[PreviewRow]:
        return list(self._rows)

    def remove_indices(self, indices: list[int]) -> int:
        """Remove rows at the given indices. Returns the number removed."""
        if not indices:
            return 0
        keep = [
            r for i, r in enumerate(self._rows) if i not in set(indices)
        ]
        removed = len(self._rows) - len(keep)
        if removed == 0:
            return 0
        self.beginResetModel()
        self._rows = keep
        self.endResetModel()
        return removed

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
        row = self._rows[index.row()]
        if row.state != STATE_NEW:
            return base
        key = self.COLUMNS[index.column()][0]
        if key in self._EDITABLE:
            return base | Qt.ItemFlag.ItemIsEditable
        return base

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        key = self.COLUMNS[index.column()][0]

        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            if role == Qt.ItemDataRole.EditRole:
                return self._edit_value(row, key)
            return self._display(row, key)

        if role == Qt.ItemDataRole.BackgroundRole:
            if row.state == STATE_NEW:
                return _COLOR_NEW
            if row.state == STATE_DUPLICATE:
                return _COLOR_DUPLICATE
            if row.state == STATE_ERROR:
                return _COLOR_ERROR
            return None

        if role == Qt.ItemDataRole.TextAlignmentRole:
            if key in self._RIGHT_ALIGNED:
                return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        if role == Qt.ItemDataRole.ToolTipRole:
            return self._tooltip(row)

        return None

    def setData(
        self,
        index: QModelIndex,
        value: Any,
        role: int = Qt.ItemDataRole.EditRole,
    ) -> bool:
        if role != Qt.ItemDataRole.EditRole or not index.isValid():
            return False
        row = self._rows[index.row()]
        if row.state != STATE_NEW:
            return False
        key = self.COLUMNS[index.column()][0]
        if key not in self._EDITABLE:
            return False
        ex = row.execution or {}

        parsed = self._parse_edit(key, value)
        if parsed is _INVALID:
            return False

        if key == "raw_symbol":
            new_raw = str(parsed or "").upper().strip()
            ex["raw_symbol"] = new_raw
            # Treat the full edited string as the root (most manual
            # entries won't have extensions); parse_symbol handles
            # the edge case where the user retyped an extension-y
            # form like "BRK.B".
            from ingest.tradestation_parser import parse_symbol
            root, ext, ext_type = parse_symbol(new_raw)
            ex["symbol"] = root
            ex["symbol_extension"] = ext
            ex["extension_type"] = ext_type
        elif key == "qty_filled":
            ex["qty_filled"] = int(parsed or 0)
            # Keep the parent ``quantity`` aligned — dedup hashes
            # on `quantity` and the two are otherwise independent.
            if ex.get("quantity") is None:
                ex["quantity"] = int(parsed or 0)
        else:
            ex[key] = parsed

        try:
            ex["import_hash"] = compute_import_hash(ex)
        except (KeyError, ValueError):
            pass
        row.execution = ex
        self.dataChanged.emit(index, index)
        return True

    # ---- formatting / tooltips --------------------------------------------

    @classmethod
    def _display(cls, row: PreviewRow, key: str) -> str:
        if key == "status":
            return cls._STATUS_LABEL.get(row.state, row.state.upper())

        if row.state == STATE_ERROR:
            # For errors, stuff the (truncated) raw line into the Date column
            # so the user has visual context; other columns stay blank.
            if key == "entered_at" and row.raw_line:
                line = row.raw_line.strip()
                return (line[:60] + "…") if len(line) > 60 else line
            return ""

        ex = row.execution or {}
        val = ex.get(key)
        if val is None:
            return ""

        if key == "entered_at":
            if isinstance(val, datetime):
                return val.strftime("%Y-%m-%d %H:%M:%S")
            return str(val)
        if key == "filled_price":
            return f"{val:.4f}"
        if key == "commission":
            sign = "-" if val < 0 else ""
            return f"{sign}${abs(val):.2f}"
        if key == "qty_filled":
            return f"{int(val):,}"
        return str(val)

    @classmethod
    def _edit_value(cls, row: PreviewRow, key: str) -> Any:
        """Return a raw value (not a formatted string) for the inline editor."""
        ex = row.execution or {}
        val = ex.get(key)
        if key == "entered_at" and isinstance(val, datetime):
            return val.strftime("%Y-%m-%d %H:%M:%S")
        return val

    @staticmethod
    def _tooltip(row: PreviewRow) -> str | None:
        if row.state == STATE_DUPLICATE and row.existing_match:
            m = row.existing_match
            entered = m.get("entered_at")
            if isinstance(entered, datetime):
                entered = entered.strftime("%Y-%m-%d %H:%M:%S")
            return (
                "Duplicate of existing execution:\n"
                f"  Entered: {entered}\n"
                f"  Symbol:  {m.get('symbol')}\n"
                f"  Type:    {m.get('type')}\n"
                f"  Price:   {m.get('filled_price')}\n"
                f"  Qty:     {m.get('qty_filled')}"
            )
        if row.state == STATE_ERROR:
            return f"Parse error: {row.error_message}\n\nRaw line:\n{row.raw_line}"
        return None

    # ---- edit parsing -----------------------------------------------------

    @classmethod
    def _parse_edit(cls, key: str, value: Any) -> Any:
        """Convert a raw edit value into the right Python type, or
        ``_INVALID`` if the input is unparseable (the edit is rejected)."""
        s = str(value).strip() if value is not None else ""
        if key == "entered_at":
            if not s:
                return _INVALID
            for fmt in (
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M",
                "%Y-%m-%dT%H:%M:%S",
                "%m/%d/%Y %H:%M:%S",
                "%m/%d/%Y %H:%M",
            ):
                try:
                    return datetime.strptime(s, fmt)
                except ValueError:
                    continue
            return _INVALID
        if key in ("filled_price", "commission"):
            try:
                return float(s)
            except ValueError:
                return _INVALID
        if key == "qty_filled":
            try:
                return int(float(s))
            except ValueError:
                return _INVALID
        return s


# Sentinel returned by ``_parse_edit`` to signal a rejected value.
_INVALID = object()
