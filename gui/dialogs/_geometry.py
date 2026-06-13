"""Mixin that adds QSettings-backed geometry persistence to a QDialog.

Why a mixin: dialogs that the user resizes (DrawDialog) or that have
useful default sizes the user might want to tweak (TagPickerDialog,
StatCardConfigDialog, BinConfigDialog) should remember their last
geometry across sessions. Doing this consistently in one place beats
sprinkling `saveGeometry`/`restoreGeometry` into every dialog.

Usage:
    class MyDialog(DialogGeometryMixin, QDialog):
        GEOMETRY_KEY = "dialog/my_dialog/geometry"

        def __init__(self, ..., parent=None):
            super().__init__(parent)
            ...
            self._restore_geometry()  # call AFTER widgets are laid out

The mixin overrides `done()` to persist the geometry on every close
(accept or reject). Restore is explicit so subclasses can decide when
their layout is final enough to take effect.
"""
from __future__ import annotations

from PySide6.QtCore import QByteArray, QSettings


class DialogGeometryMixin:
    """Adds save/restore-on-done geometry persistence to a QDialog subclass."""

    #: Subclasses MUST override with a unique QSettings key.
    GEOMETRY_KEY: str = ""

    def _settings(self) -> QSettings:
        # We rely on QApplication.setOrganizationName / setApplicationName
        # being called in main.py so this default constructor finds the
        # same settings file the rest of the app uses.
        return QSettings("TradeBook", "TradeBook")

    def _restore_geometry(self) -> None:
        if not self.GEOMETRY_KEY:
            return
        geom = self._settings().value(self.GEOMETRY_KEY)
        if isinstance(geom, QByteArray) and not geom.isEmpty():
            self.restoreGeometry(geom)  # type: ignore[attr-defined]

    def _save_geometry(self) -> None:
        if not self.GEOMETRY_KEY:
            return
        self._settings().setValue(
            self.GEOMETRY_KEY,
            self.saveGeometry(),  # type: ignore[attr-defined]
        )

    def done(self, result: int) -> None:  # type: ignore[override]
        self._save_geometry()
        super().done(result)  # type: ignore[misc]
