"""Generate Brief dialog — confirmation step before a brief is built.

Shows:
    * A preview of the title derived from the Journal tab's current
      filter state (live-editable — the user can override it)
    * ``☐ Include images`` checkbox
    * Output checkboxes: ``☐ Save to Briefs tab``, ``☐ Export as file``
      (at least one must be checked)
    * If "Export as file" is checked, a format dropdown for the file

The dialog itself does NOT generate or export — it just gathers the
user's choices. The caller (JournalTab) performs the work.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QVBoxLayout,
)

from analytics.briefs import BriefFilters, build_title
from export import exporters
from gui.dialogs._geometry import DialogGeometryMixin


@dataclass
class BriefGenerationChoice:
    title: str
    include_images: bool
    save_to_briefs: bool
    export_to_file: bool
    export_format: str


class GenerateBriefDialog(DialogGeometryMixin, QDialog):
    GEOMETRY_KEY = "dialog/generate_brief/geometry"

    def __init__(
        self,
        filters: BriefFilters,
        *,
        trade_count: int,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Generate Brief")
        self.setModal(True)

        # --- title field ---------------------------------------------------
        default_title = build_title(filters)
        self._title_edit = QLineEdit(default_title, self)
        self._title_edit.selectAll()

        self._summary_label = QLabel(
            f"Matching trades with journal entries: {trade_count}",
            self,
        )
        self._summary_label.setObjectName("hintLabel")

        # --- options -------------------------------------------------------
        self._chk_images = QCheckBox("Include images in the brief", self)
        self._chk_images.setChecked(bool(filters.include_images))

        self._chk_save = QCheckBox("Save to Briefs tab", self)
        self._chk_save.setChecked(True)

        self._chk_export = QCheckBox("Export as file…", self)
        self._chk_export.setChecked(False)

        self._format_combo = QComboBox(self)
        for fmt, label in exporters.EXPORT_FORMATS:
            self._format_combo.addItem(label, fmt)
        self._format_combo.setEnabled(False)
        self._chk_export.toggled.connect(self._format_combo.setEnabled)

        # --- layout --------------------------------------------------------
        form = QFormLayout()
        form.addRow(QLabel("Title:"), self._title_edit)

        options_col = QVBoxLayout()
        options_col.addWidget(self._chk_images)
        options_col.addWidget(self._chk_save)
        export_row = QHBoxLayout()
        export_row.setContentsMargins(0, 0, 0, 0)
        export_row.addWidget(self._chk_export)
        export_row.addWidget(self._format_combo, 1)
        options_col.addLayout(export_row)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        self._buttons.accepted.connect(self._on_accept)
        self._buttons.rejected.connect(self.reject)

        outer = QVBoxLayout(self)
        outer.addLayout(form)
        outer.addWidget(self._summary_label)
        outer.addSpacing(8)
        outer.addLayout(options_col)
        outer.addStretch()
        outer.addWidget(self._buttons)

        # Disable OK when zero matches — nothing useful to generate.
        if trade_count == 0:
            self._buttons.button(
                QDialogButtonBox.StandardButton.Ok
            ).setEnabled(False)
            self._summary_label.setText(
                "No matching trades have journal entries — adjust your "
                "filters or add notes first."
            )

        self.resize(500, 220)
        self._restore_geometry()

    # ---- accessors --------------------------------------------------------

    def choice(self) -> BriefGenerationChoice:
        return BriefGenerationChoice(
            title=self._title_edit.text().strip() or build_title(
                BriefFilters(include_images=self._chk_images.isChecked())
            ),
            include_images=self._chk_images.isChecked(),
            save_to_briefs=self._chk_save.isChecked(),
            export_to_file=self._chk_export.isChecked(),
            export_format=str(self._format_combo.currentData()),
        )

    # ---- accept guard -----------------------------------------------------

    def _on_accept(self) -> None:
        if not (self._chk_save.isChecked() or self._chk_export.isChecked()):
            QMessageBox.information(
                self,
                "No output selected",
                "Choose at least one output: save to the Briefs tab, "
                "export as a file, or both.",
            )
            return
        if not self._title_edit.text().strip():
            QMessageBox.information(
                self,
                "Empty title",
                "Please enter a title for the brief.",
            )
            return
        self.accept()
