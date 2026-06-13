"""Report filter bar — date range + symbol multi-select + direction + W/L
+ hold-time bounds. Emits `filters_changed(FilterSpec)` whenever the user
applies a change.

Compact two-row layout so it fits above the report sub-tabs without
crowding the chart area.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable, Optional

from PySide6.QtCore import QDate, QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QDateEdit, QHBoxLayout, QLabel, QListView, QPushButton,
    QSizePolicy, QSpinBox, QToolButton, QVBoxLayout, QWidget,
)
from PySide6.QtGui import QStandardItem, QStandardItemModel

from analytics.reports import FilterSpec

PRESET_ALL = "All"
PRESET_TODAY = "Today"
PRESET_WEEK = "One Week"
PRESET_MONTH = "One Month"
PRESET_YEAR = "One Year"

PRESETS = [PRESET_ALL, PRESET_TODAY, PRESET_WEEK, PRESET_MONTH, PRESET_YEAR]


class _CheckableSymbolCombo(QComboBox):
    """A QComboBox whose popup shows checkable items. Selected symbols are
    summarized in the line edit text. Empty selection = "All symbols"."""

    selection_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setEditable(False)
        self._model = QStandardItemModel(self)
        self.setModel(self._model)
        self.setView(QListView(self))
        self.view().pressed.connect(self._on_item_pressed)
        self._all_symbols: list[str] = []
        self._refresh_label()

    def set_symbols(self, symbols: Iterable[str]) -> None:
        prev = set(self.selected_symbols())
        self._all_symbols = sorted({s.upper() for s in symbols if s})
        self._model.clear()
        for s in self._all_symbols:
            item = QStandardItem(s)
            item.setFlags(
                Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable
            )
            item.setData(
                Qt.CheckState.Checked if s in prev else Qt.CheckState.Unchecked,
                Qt.ItemDataRole.CheckStateRole,
            )
            self._model.appendRow(item)
        self._refresh_label()

    def selected_symbols(self) -> list[str]:
        out: list[str] = []
        for i in range(self._model.rowCount()):
            it = self._model.item(i)
            if it.checkState() == Qt.CheckState.Checked:
                out.append(it.text())
        return out

    def clear_selection(self) -> None:
        for i in range(self._model.rowCount()):
            self._model.item(i).setCheckState(Qt.CheckState.Unchecked)
        self._refresh_label()
        self.selection_changed.emit()

    def _on_item_pressed(self, index) -> None:
        item = self._model.itemFromIndex(index)
        if item is None:
            return
        new_state = (
            Qt.CheckState.Unchecked
            if item.checkState() == Qt.CheckState.Checked
            else Qt.CheckState.Checked
        )
        item.setCheckState(new_state)
        self._refresh_label()
        self.selection_changed.emit()

    def _refresh_label(self) -> None:
        sel = self.selected_symbols()
        if not sel:
            text = "All symbols"
        elif len(sel) <= 3:
            text = ", ".join(sel)
        else:
            text = f"{len(sel)} symbols"
        # QComboBox shows the current item's text. We hijack by inserting a
        # transient row 0 displaying the summary, then setting it as current.
        self.blockSignals(True)
        # Use the line-edit-style display via setItemText / current index gymnastics:
        # simplest path is to override the displayed text via setPlaceholderText
        # — but QComboBox.setCurrentText only works on editable combos.
        # We instead show the summary as a tooltip + the first model row label.
        self.setToolTip(text)
        self.blockSignals(False)
        # Use the (non-editable) combo's view text by relying on currentText
        # via a synthetic placeholder item at index -1: too fiddly. Just set
        # the visible text via the line edit override below.
        self.update()

    # Show summary text in the closed combo by painting it manually.
    def paintEvent(self, event):  # noqa: N802
        from PySide6.QtWidgets import QStylePainter, QStyleOptionComboBox, QStyle
        painter = QStylePainter(self)
        opt = QStyleOptionComboBox()
        self.initStyleOption(opt)
        sel = self.selected_symbols()
        if not sel:
            opt.currentText = "All symbols"
        elif len(sel) <= 3:
            opt.currentText = ", ".join(sel)
        else:
            opt.currentText = f"{len(sel)} symbols"
        painter.drawComplexControl(QStyle.ComplexControl.CC_ComboBox, opt)
        painter.drawControl(QStyle.ControlElement.CE_ComboBoxLabel, opt)


class ReportFilterBar(QWidget):
    """Compact filter bar that emits a `FilterSpec` whenever changed.

    Symbol list must be refreshed externally via `set_symbols(...)`
    whenever the underlying trade data changes.
    """

    filters_changed = Signal(object)  # FilterSpec

    def __init__(self, parent=None):
        super().__init__(parent)

        # ---- Row 1: presets + custom dates ---------------------------------
        today = date.today()

        self._preset_buttons: dict[str, QPushButton] = {}
        for label in PRESETS:
            btn = QPushButton(label, self)
            btn.setCheckable(True)
            btn.setAutoExclusive(False)
            btn.clicked.connect(
                lambda _checked=False, name=label: self._apply_preset(name)
            )
            self._preset_buttons[label] = btn

        self.edit_from = QDateEdit(self)
        self.edit_from.setCalendarPopup(True)
        self.edit_from.setDisplayFormat("yyyy-MM-dd")
        self.edit_from.setDate(QDate(today.year, 1, 1))

        self.edit_to = QDateEdit(self)
        self.edit_to.setCalendarPopup(True)
        self.edit_to.setDisplayFormat("yyyy-MM-dd")
        self.edit_to.setDate(QDate(today.year, today.month, today.day))

        self.btn_apply_dates = QPushButton("Apply Dates", self)
        self.btn_apply_dates.clicked.connect(self._on_apply_dates)

        row1 = QHBoxLayout()
        row1.setSpacing(4)
        for label in PRESETS:
            row1.addWidget(self._preset_buttons[label])
        row1.addSpacing(12)
        row1.addWidget(QLabel("From:", self))
        row1.addWidget(self.edit_from)
        row1.addWidget(QLabel("To:", self))
        row1.addWidget(self.edit_to)
        row1.addWidget(self.btn_apply_dates)
        row1.addStretch()

        # ---- Row 2: symbols / direction / W-L / hold-time ------------------
        self.combo_symbols = _CheckableSymbolCombo(self)
        self.combo_symbols.setMinimumWidth(160)
        self.combo_symbols.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed,
        )
        self.combo_symbols.selection_changed.connect(self._emit_filters)

        self.btn_clear_symbols = QToolButton(self)
        self.btn_clear_symbols.setText("✕")
        self.btn_clear_symbols.setToolTip("Clear symbol filter")
        self.btn_clear_symbols.clicked.connect(self.combo_symbols.clear_selection)

        self.combo_direction = QComboBox(self)
        self.combo_direction.addItem("All directions", None)
        self.combo_direction.addItem("Long only", "Long")
        self.combo_direction.addItem("Short only", "Short")
        self.combo_direction.currentIndexChanged.connect(self._emit_filters)

        self.combo_result = QComboBox(self)
        self.combo_result.addItem("Wins + Losses", None)
        self.combo_result.addItem("Wins only", "win")
        self.combo_result.addItem("Losses only", "loss")
        self.combo_result.currentIndexChanged.connect(self._emit_filters)

        self.spin_min_hold = QSpinBox(self)
        self.spin_min_hold.setRange(0, 7 * 24 * 60)
        self.spin_min_hold.setSuffix(" min")
        self.spin_min_hold.setSpecialValueText("min: any")
        self.spin_min_hold.setValue(0)
        self.spin_min_hold.editingFinished.connect(self._emit_filters)

        self.spin_max_hold = QSpinBox(self)
        self.spin_max_hold.setRange(0, 7 * 24 * 60)
        self.spin_max_hold.setSuffix(" min")
        self.spin_max_hold.setSpecialValueText("max: any")
        self.spin_max_hold.setValue(0)
        self.spin_max_hold.editingFinished.connect(self._emit_filters)

        self.btn_reset = QPushButton("Reset Filters", self)
        self.btn_reset.clicked.connect(self.reset)

        row2 = QHBoxLayout()
        row2.setSpacing(6)
        row2.addWidget(QLabel("Symbols:", self))
        row2.addWidget(self.combo_symbols)
        row2.addWidget(self.btn_clear_symbols)
        row2.addSpacing(8)
        row2.addWidget(self.combo_direction)
        row2.addWidget(self.combo_result)
        row2.addSpacing(8)
        row2.addWidget(QLabel("Hold:", self))
        row2.addWidget(self.spin_min_hold)
        row2.addWidget(QLabel("–", self))
        row2.addWidget(self.spin_max_hold)
        row2.addSpacing(8)
        row2.addWidget(self.btn_reset)
        row2.addStretch()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)
        outer.addLayout(row1)
        outer.addLayout(row2)

        # Default range = All time.
        self._start: Optional[date] = None
        self._end: Optional[date] = None
        # Debounce timer — coalesces bursts of filter changes (e.g.
        # checking five symbols in rapid succession) into one downstream
        # re-render. Single-shot so it resets on each new change.
        self._emit_timer = QTimer(self)
        self._emit_timer.setSingleShot(True)
        self._emit_timer.setInterval(120)
        self._emit_timer.timeout.connect(self._emit_filters_now)
        self._apply_preset(PRESET_ALL, emit=False)

    # ---- public API --------------------------------------------------------

    def set_symbols(self, symbols: Iterable[str]) -> None:
        self.combo_symbols.set_symbols(symbols)

    def current_spec(self) -> FilterSpec:
        syms = self.combo_symbols.selected_symbols()
        return FilterSpec(
            start=self._start,
            end=self._end,
            symbols=syms or None,
            direction=self.combo_direction.currentData(),
            result=self.combo_result.currentData(),
            min_hold_seconds=self.spin_min_hold.value() * 60,
            max_hold_seconds=self.spin_max_hold.value() * 60,
        )

    def reset(self) -> None:
        self.combo_symbols.clear_selection()
        self.combo_direction.setCurrentIndex(0)
        self.combo_result.setCurrentIndex(0)
        self.spin_min_hold.setValue(0)
        self.spin_max_hold.setValue(0)
        self._apply_preset(PRESET_ALL)
        # Reset is a deliberate single-click action — there's no reason
        # to wait for the debounce window. Flushing here also keeps
        # tests that drive reset() synchronously happy.
        self.flush_pending()

    def flush_pending(self) -> None:
        """Force any pending debounced emission to fire immediately.

        Used by callers (and tests) that need synchronous state after a
        mutation — the chip-combo debounce is for user-pace bursts, not
        for programmatic control flow.
        """
        if self._emit_timer.isActive():
            self._emit_timer.stop()
            self._emit_filters_now()

    # ---- internals ---------------------------------------------------------

    def _set_active_preset(self, label: Optional[str]) -> None:
        for name, btn in self._preset_buttons.items():
            btn.setChecked(name == label)

    def _apply_preset(self, label: str, emit: bool = True) -> None:
        today = date.today()
        if label == PRESET_ALL:
            start, end = None, None
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

        self._start, self._end = start, end
        self._set_active_preset(label)

        if start is not None:
            self.edit_from.blockSignals(True)
            self.edit_to.blockSignals(True)
            self.edit_from.setDate(QDate(start.year, start.month, start.day))
            self.edit_to.setDate(QDate(end.year, end.month, end.day))
            self.edit_from.blockSignals(False)
            self.edit_to.blockSignals(False)

        if emit:
            self._emit_filters()

    def _on_apply_dates(self) -> None:
        q_from = self.edit_from.date()
        q_to = self.edit_to.date()
        start = date(q_from.year(), q_from.month(), q_from.day())
        end = date(q_to.year(), q_to.month(), q_to.day())
        if start > end:
            return
        self._start = start
        self._end = end
        self._set_active_preset(None)
        self._emit_filters()

    def _emit_filters(self) -> None:
        # Debounce — the actual emission happens once the user stops
        # clicking for ~120ms. Restarting the timer on every change
        # gives the coalesce behaviour without losing the final state.
        self._emit_timer.start()

    def _emit_filters_now(self) -> None:
        self.filters_changed.emit(self.current_spec())
