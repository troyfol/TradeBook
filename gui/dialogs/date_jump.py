"""Modal date picker — used by the Calendar tab's `Go to date...` button."""
from __future__ import annotations

from datetime import date
from typing import Optional

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QCalendarWidget, QDialog, QDialogButtonBox, QVBoxLayout,
)


class DateJumpDialog(QDialog):
    """Tiny wrapper around QCalendarWidget that returns a `datetime.date`."""

    def __init__(self, initial: Optional[date] = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Go to date")
        self.setModal(True)

        self.calendar = QCalendarWidget(self)
        self.calendar.setGridVisible(True)
        self.calendar.setVerticalHeaderFormat(
            QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader
        )
        self.calendar.setFirstDayOfWeek(Qt.DayOfWeek.Monday)
        if initial is not None:
            self.calendar.setSelectedDate(
                QDate(initial.year, initial.month, initial.day)
            )

        # Double-click on a day = accept immediately.
        self.calendar.activated.connect(lambda _qd: self.accept())

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(self.calendar)
        layout.addWidget(buttons)

    def selected_date(self) -> date:
        qd = self.calendar.selectedDate()
        return date(qd.year(), qd.month(), qd.day())
