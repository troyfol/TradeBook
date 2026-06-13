"""Date range selector bar — presets + custom QDateEdit range."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from PySide6.QtCore import QDate, Qt, Signal
from PySide6.QtWidgets import (
    QDateEdit, QHBoxLayout, QLabel, QPushButton, QWidget,
)

from gui.settings_keys import (
    DASHBOARD_DATE_FROM as _SETTING_FROM,
    DASHBOARD_DATE_PRESET as _SETTING_PRESET,
    DASHBOARD_DATE_TO as _SETTING_TO,
)

PRESET_ALL = "All"
PRESET_TODAY = "Today"
PRESET_WEEK = "One Week"
PRESET_MONTH = "One Month"
PRESET_YEAR = "One Year"
PRESET_CUSTOM = "Custom"

# Legacy labels kept as silent aliases so QSettings values saved by
# earlier versions still restore sensibly. Any unknown / stale label
# falls back to PRESET_ALL in _restore_from_settings.
_LEGACY_ALIASES = {
    "YTD": PRESET_YEAR,   # closest semantic replacement
    "Month": PRESET_MONTH,
    "Week": PRESET_WEEK,
}

PRESETS = [PRESET_ALL, PRESET_TODAY, PRESET_WEEK, PRESET_MONTH, PRESET_YEAR]


class DateRangeBar(QWidget):
    """Emits `range_changed(start, end)` whenever the active range changes."""

    range_changed = Signal(object, object)  # (date, date)

    def __init__(self, settings=None, parent=None):
        super().__init__(parent)
        self._settings = settings

        self._preset_buttons: dict[str, QPushButton] = {}
        for label in PRESETS:
            btn = QPushButton(label, self)
            btn.setCheckable(True)
            btn.setAutoExclusive(False)
            btn.clicked.connect(
                lambda _checked=False, name=label: self._apply_preset(name)
            )
            self._preset_buttons[label] = btn

        today = date.today()
        self.edit_from = QDateEdit(self)
        self.edit_from.setCalendarPopup(True)
        self.edit_from.setDisplayFormat("yyyy-MM-dd")
        self.edit_from.setDate(QDate(today.year, today.month, today.day))

        self.edit_to = QDateEdit(self)
        self.edit_to.setCalendarPopup(True)
        self.edit_to.setDisplayFormat("yyyy-MM-dd")
        self.edit_to.setDate(QDate(today.year, today.month, today.day))

        self.btn_apply = QPushButton("Apply", self)
        self.btn_apply.clicked.connect(self._apply_custom)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        for label in PRESETS:
            layout.addWidget(self._preset_buttons[label])
        layout.addSpacing(16)
        layout.addWidget(QLabel("Custom:", self))
        layout.addWidget(self.edit_from)
        layout.addWidget(QLabel("to", self))
        layout.addWidget(self.edit_to)
        layout.addWidget(self.btn_apply)
        layout.addStretch()

        self._start: date = date(1970, 1, 1)
        self._end: date = today
        # Tracks the (start, end) most recently broadcast through
        # range_changed so we can suppress redundant re-emissions when a
        # preset is re-clicked or an Apply is fired without a real
        # change. Downstream consumers (Dashboard.refresh) do heavy
        # work per emission, so this dedupe is meaningful even though
        # the comparison is trivial.
        self._last_emitted: tuple[Optional[date], Optional[date]] = (
            None, None,
        )

        self._restore_from_settings()

    # ---- public -----------------------------------------------------------

    def current_range(self) -> tuple[date, date]:
        return self._start, self._end

    # ---- preset / custom logic --------------------------------------------

    def _set_active_preset(self, label: Optional[str]) -> None:
        for name, btn in self._preset_buttons.items():
            btn.setChecked(name == label)

    def _apply_preset(self, label: str, emit: bool = True) -> None:
        today = date.today()
        if label == PRESET_ALL:
            start, end = date(1970, 1, 1), today
        elif label == PRESET_TODAY:
            start, end = today, today
        elif label == PRESET_WEEK:
            start, end = today - timedelta(days=7), today
        elif label == PRESET_MONTH:
            start, end = today - timedelta(days=30), today
        elif label == PRESET_YEAR:
            start, end = today - timedelta(days=365), today
        else:
            return

        self._start = start
        self._end = end
        self._set_active_preset(label)
        # Sync the custom date edits to reflect the active preset range.
        self.edit_from.blockSignals(True)
        self.edit_to.blockSignals(True)
        self.edit_from.setDate(QDate(start.year, start.month, start.day))
        self.edit_to.setDate(QDate(end.year, end.month, end.day))
        self.edit_from.blockSignals(False)
        self.edit_to.blockSignals(False)

        if self._settings is not None:
            self._settings.setValue(_SETTING_PRESET, label)

        if emit:
            self._maybe_emit(start, end)

    def _apply_custom(self) -> None:
        q_from = self.edit_from.date()
        q_to = self.edit_to.date()
        start = date(q_from.year(), q_from.month(), q_from.day())
        end = date(q_to.year(), q_to.month(), q_to.day())
        if start > end:
            return  # ignore invalid range

        self._start = start
        self._end = end
        self._set_active_preset(None)

        if self._settings is not None:
            self._settings.setValue(_SETTING_PRESET, PRESET_CUSTOM)
            self._settings.setValue(_SETTING_FROM, start.isoformat())
            self._settings.setValue(_SETTING_TO, end.isoformat())

        self._maybe_emit(start, end)

    def _maybe_emit(self, start: date, end: date) -> None:
        """Emit ``range_changed`` only when the range actually changed.

        Re-clicking the active preset, or clicking Apply with the dates
        already in place, is a no-op rather than a full refresh.
        """
        if (start, end) == self._last_emitted:
            return
        self._last_emitted = (start, end)
        self.range_changed.emit(start, end)

    def _restore_from_settings(self) -> None:
        if self._settings is None:
            self._apply_preset(PRESET_ALL, emit=False)
            return
        saved = self._settings.value(_SETTING_PRESET, PRESET_ALL)
        if saved == PRESET_CUSTOM:
            f_str = self._settings.value(_SETTING_FROM)
            t_str = self._settings.value(_SETTING_TO)
            if isinstance(f_str, str) and isinstance(t_str, str):
                try:
                    start = date.fromisoformat(f_str)
                    end = date.fromisoformat(t_str)
                    self._start = start
                    self._end = end
                    self.edit_from.setDate(QDate(start.year, start.month, start.day))
                    self.edit_to.setDate(QDate(end.year, end.month, end.day))
                    self._set_active_preset(None)
                    return
                except ValueError:
                    pass
        if saved in _LEGACY_ALIASES:
            saved = _LEGACY_ALIASES[saved]
        if saved in PRESETS:
            self._apply_preset(saved, emit=False)
        else:
            self._apply_preset(PRESET_ALL, emit=False)
