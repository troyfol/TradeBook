"""Export dialog — asks for a filename + target format, shown before
the actual system file-save dialog. Used for right-click export of
journal entries and briefs.

Flow:
    1. User right-clicks an item and picks Export.
    2. This dialog opens, pre-filled with the default name and a
       format dropdown covering all four supported formats.
    3. On accept, the caller opens a ``QFileDialog`` seeded with the
       chosen name + extension, then calls ``export.export_document``.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit,
    QVBoxLayout,
)

from export import exporters
from gui.dialogs._geometry import DialogGeometryMixin


class ExportDocumentDialog(DialogGeometryMixin, QDialog):
    """Filename + format picker for document export."""

    GEOMETRY_KEY = "dialog/export_document/geometry"

    def __init__(
        self,
        default_name: str,
        *,
        default_format: str = exporters.FORMAT_DOCX,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Export…")
        self.setModal(True)

        self._name_edit = QLineEdit(default_name, self)
        self._name_edit.selectAll()

        self._format_combo = QComboBox(self)
        for fmt, label in exporters.EXPORT_FORMATS:
            self._format_combo.addItem(label, fmt)
        idx = self._format_combo.findData(default_format)
        if idx >= 0:
            self._format_combo.setCurrentIndex(idx)

        form = QFormLayout()
        form.addRow(QLabel("Filename:"), self._name_edit)
        form.addRow(QLabel("Format:"), self._format_combo)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        outer = QVBoxLayout(self)
        outer.addLayout(form)
        outer.addWidget(buttons)

        self.resize(440, 120)
        self._restore_geometry()

    # ---- accessors --------------------------------------------------------

    def filename(self) -> str:
        """Return the user-entered filename, stripped of a known
        extension if they added one (we re-append based on format)."""
        name = self._name_edit.text().strip()
        for fmt, _label in exporters.EXPORT_FORMATS:
            ext = exporters.extension_for_format(fmt)
            if name.lower().endswith(ext):
                return name[: -len(ext)]
        return name

    def selected_format(self) -> str:
        return str(self._format_combo.currentData())

    def selected_extension(self) -> str:
        return exporters.extension_for_format(self.selected_format())
