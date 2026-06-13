"""Dialog for selecting + reordering the stat cards on the Dashboard."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QLabel, QListWidget,
    QListWidgetItem, QVBoxLayout,
)

from analytics.metrics import MetricDef
from gui.dialogs._geometry import DialogGeometryMixin


class StatCardConfigDialog(DialogGeometryMixin, QDialog):
    GEOMETRY_KEY = "dialog/stat_card_config/geometry"
    """Checkable, drag-reorderable list of available metrics.

    Returns the selected (checked) keys in their displayed order via
    `selected_order()`. The caller is responsible for persisting the result.
    """

    def __init__(
        self,
        current_order: list[str],
        all_metrics: list[MetricDef],
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Configure Stat Cards")
        self.resize(380, 440)

        instructions = QLabel(
            "Check metrics to show them.  Drag rows to reorder.",
            self,
        )
        instructions.setObjectName("hintLabel")

        self.list_widget = QListWidget(self)
        self.list_widget.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.list_widget.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.list_widget.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )

        by_key = {m.key: m for m in all_metrics}
        seen: set[str] = set()

        # Checked items first, in their current saved order.
        for key in current_order:
            mdef = by_key.get(key)
            if mdef is None:
                continue
            self._add_item(mdef, checked=True)
            seen.add(key)

        # Unchecked items (anything in the registry but not in current_order).
        for mdef in all_metrics:
            if mdef.key in seen:
                continue
            self._add_item(mdef, checked=False)

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

    def _add_item(self, mdef: MetricDef, checked: bool) -> None:
        item = QListWidgetItem(mdef.label)
        item.setData(Qt.ItemDataRole.UserRole, mdef.key)
        item.setFlags(
            item.flags()
            | Qt.ItemFlag.ItemIsUserCheckable
            | Qt.ItemFlag.ItemIsDragEnabled
        )
        item.setCheckState(
            Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        )
        self.list_widget.addItem(item)

    def selected_order(self) -> list[str]:
        """Return the checked metric keys in their current row order."""
        result: list[str] = []
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                key = item.data(Qt.ItemDataRole.UserRole)
                if isinstance(key, str):
                    result.append(key)
        return result
