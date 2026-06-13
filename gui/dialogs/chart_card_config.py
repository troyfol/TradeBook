"""Dialog for selecting + reordering the chart cards on the Dashboard.

Mirrors ``StatCardConfigDialog`` — same drag-to-reorder + check-to-show
UX — but operates on ``ChartCardDef`` rows instead of ``MetricDef``.
Returns the selected (checked) keys in their displayed order via
``selected_order()``. The caller is responsible for persisting the
result.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QLabel, QListWidget,
    QListWidgetItem, QVBoxLayout,
)

from gui.dialogs._geometry import DialogGeometryMixin
from gui.widgets.chart_registry import ChartCardDef


class ChartCardConfigDialog(DialogGeometryMixin, QDialog):
    GEOMETRY_KEY = "dialog/chart_card_config/geometry"

    def __init__(
        self,
        current_order: list[str],
        all_charts: list[ChartCardDef],
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Configure Charts")
        self.resize(440, 480)

        instructions = QLabel(
            "Check the charts you want shown on the Dashboard. "
            "Drag rows to reorder.",
            self,
        )
        instructions.setObjectName("hintLabel")
        instructions.setWordWrap(True)

        self.list_widget = QListWidget(self)
        self.list_widget.setDragDropMode(
            QAbstractItemView.DragDropMode.InternalMove,
        )
        self.list_widget.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.list_widget.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection,
        )

        by_key = {c.key: c for c in all_charts}
        seen: set[str] = set()

        # Checked items first, in their saved order.
        for key in current_order:
            cdef = by_key.get(key)
            if cdef is None:
                continue
            self._add_item(cdef, checked=True)
            seen.add(key)

        # Unchecked items (anything in the registry but not in current_order).
        for cdef in all_charts:
            if cdef.key in seen:
                continue
            self._add_item(cdef, checked=False)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(instructions)
        layout.addWidget(self.list_widget, 1)
        layout.addWidget(buttons)

        self._restore_geometry()

    def _add_item(self, cdef: ChartCardDef, checked: bool) -> None:
        item = QListWidgetItem(cdef.label)
        item.setData(Qt.ItemDataRole.UserRole, cdef.key)
        if cdef.description:
            item.setToolTip(cdef.description)
        item.setFlags(
            item.flags()
            | Qt.ItemFlag.ItemIsUserCheckable
            | Qt.ItemFlag.ItemIsDragEnabled
        )
        item.setCheckState(
            Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked,
        )
        self.list_widget.addItem(item)

    def selected_order(self) -> list[str]:
        result: list[str] = []
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                key = item.data(Qt.ItemDataRole.UserRole)
                if isinstance(key, str):
                    result.append(key)
        return result
