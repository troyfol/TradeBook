"""Journal tab — split pane: trade list ⟷ rich-text editor + tags.

Layout:
    [Search…] [From] [To] [☐ Has journal only] [Tag filter ▼] [Reset]
    +-----------+----------------------------------------+
    | Trade     |  AAPL  Long  Entry $1.50 → Exit $1.80 |
    | list      |  Entered 2026-04-11 09:30  ·  +$30.00 |
    | (compact) |----------------------------------------
    |           |  [JournalEditor — rich-text]          |
    |           |                                       |
    |           |----------------------------------------
    |           |  [TagChipStrip] [+ Tag]               |
    +-----------+----------------------------------------+

Persistence rules:
    * Notes + tags autosave on selection change, tab switch, and window
      close. Empty notes delete the journal_entries row entirely.
    * Tags survive `rebuild_trades` because the rebuild snapshots them
      keyed by (symbol, entry_time, direction).
    * Splitter geometry, search/filter state, and the last-active trade
      id are persisted via QSettings.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QDate, QPoint, QSettings, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDateEdit, QDialog, QFileDialog,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMenu, QMessageBox,
    QPushButton, QSplitter, QStackedWidget, QTableView, QVBoxLayout, QWidget,
)

from analytics.briefs import BriefFilters
from analytics import briefs as briefs_mod
from export import exporters

# Date-filter quick-select labels, shared with the Briefs tab so the
# filter UX is identical across both. "All" maps the From sentinel back
# to 1990-01-01 (i.e. unbounded lower) with the To pinned at today.
PRESET_ALL = "All"
PRESET_TODAY = "Today"
PRESET_WEEK = "One Week"
PRESET_MONTH = "One Month"
PRESET_YEAR = "One Year"
DATE_PRESETS = [PRESET_ALL, PRESET_TODAY, PRESET_WEEK, PRESET_MONTH, PRESET_YEAR]
from gui.dialogs.draw_dialog import DrawDialog
from gui.dialogs.export_document import ExportDocumentDialog
from gui.dialogs.generate_brief import GenerateBriefDialog
from gui.dialogs.tag_picker import TagPickerDialog
from gui.settings_keys import (
    BRIEFS_LAST_EXPORT_DIR as SETTINGS_LAST_EXPORT_DIR,
    JOURNAL_HAS_JOURNAL_ONLY as SETTINGS_HAS_JOURNAL_ONLY,
    JOURNAL_LAST_TRADE_ID as SETTINGS_LAST_TRADE_ID,
    JOURNAL_SPLITTER as SETTINGS_SPLITTER,
)
from gui.widgets.find_bar import FindBar
from gui.widgets.journal_editor import JournalEditor
from gui.widgets.report_filter_bar import _CheckableSymbolCombo
from gui.widgets.rich_text_toolbar import RichTextToolbar
from gui.widgets.tag_chip_strip import TagChipStrip
from ingest import db_manager
from models.journal_trade_model import JournalTradeModel


class JournalTab(QWidget):
    # Emitted whenever a journal entry is saved (so other tabs can refresh
    # their 📝 indicator state if they end up showing one).
    journal_saved = Signal(int)

    # Emitted after a brief is generated + saved to the Briefs tab. Carries
    # the new brief's row id so the Briefs tab can refresh + select it.
    brief_saved = Signal(int)

    def __init__(
        self,
        get_conn: Callable[[], sqlite3.Connection],
        settings: Optional[QSettings] = None,
        parent=None,
    ):
        super().__init__(parent)
        self._get_conn = get_conn
        self._settings = settings

        # Selection state.
        self._current_trade_id: Optional[int] = None
        self._suspend_autosave = False
        self._tag_filter_ids: list[int] = []
        self._all_trades: list[dict] = []  # before filter
        # Cached set of trade_ids that have journal entries. Kept in
        # sync incrementally in `_save_current_notes` so autosave doesn't
        # need to re-query the whole `journal_entries` table on each
        # keystroke-triggered flush.
        self._journal_ids: set[int] = set()

        # ---- top filter bar -----------------------------------------------
        self.search_edit = QLineEdit(self)
        self.search_edit.setPlaceholderText("Search journal text…")
        self.search_edit.textChanged.connect(self._on_filters_changed)

        # "From:" still uses 1990-01-01 as the unbounded sentinel. "To:"
        # defaults to today so the journal opens centred on the present
        # rather than showing future-padded data.
        today = date.today()
        self.date_from = QDateEdit(self)
        self.date_from.setCalendarPopup(True)
        self.date_from.setDisplayFormat("yyyy-MM-dd")
        self.date_from.setSpecialValueText("From…")
        self.date_from.setMinimumDate(QDate(1990, 1, 1))
        self.date_from.setDate(QDate(1990, 1, 1))  # = "no lower bound"
        self.date_from.dateChanged.connect(self._on_date_edit_changed)

        self.date_to = QDateEdit(self)
        self.date_to.setCalendarPopup(True)
        self.date_to.setDisplayFormat("yyyy-MM-dd")
        self.date_to.setMinimumDate(QDate(1990, 1, 1))
        self.date_to.setMaximumDate(QDate(2100, 12, 31))
        self.date_to.setDate(QDate(today.year, today.month, today.day))
        self.date_to.dateChanged.connect(self._on_date_edit_changed)

        # Preset buttons — applied in addition to (not instead of) the
        # custom From/To pickers. Each one sets both edits and triggers
        # the standard filter-changed pipeline.
        self.preset_buttons: dict[str, QPushButton] = {}
        for label in DATE_PRESETS:
            btn = QPushButton(label, self)
            btn.setCheckable(True)
            btn.setAutoExclusive(False)
            btn.clicked.connect(
                lambda _checked=False, name=label: self._apply_date_preset(name)
            )
            self.preset_buttons[label] = btn

        self.chk_has_journal = QCheckBox("Has journal only", self)
        self.chk_has_journal.toggled.connect(self._on_filters_changed)

        self.btn_tag_filter = QPushButton("Tag filter…", self)
        self.btn_tag_filter.clicked.connect(self._on_tag_filter_clicked)

        # Ticker multi-select — mirrors the Reports tab pattern so the
        # Journal filter set (and the Brief generation filters) stays
        # consistent with the rest of the app.
        self.combo_symbols = _CheckableSymbolCombo(self)
        self.combo_symbols.setMinimumWidth(140)
        self.combo_symbols.selection_changed.connect(self._on_filters_changed)

        self.btn_generate_brief = QPushButton("Generate Brief…", self)
        self.btn_generate_brief.setToolTip(
            "Compile the filtered journal entries into a single brief "
            "document — save to the Briefs tab and/or export as a file."
        )
        self.btn_generate_brief.clicked.connect(self._on_generate_brief)

        self.btn_reset = QPushButton("Reset", self)
        self.btn_reset.clicked.connect(self._on_reset_filters)

        filter_row = QHBoxLayout()
        filter_row.setContentsMargins(0, 0, 0, 0)
        filter_row.addWidget(self.search_edit, 2)
        filter_row.addWidget(QLabel("From:", self))
        filter_row.addWidget(self.date_from)
        filter_row.addWidget(QLabel("To:", self))
        filter_row.addWidget(self.date_to)
        filter_row.addWidget(QLabel("Symbols:", self))
        filter_row.addWidget(self.combo_symbols)
        filter_row.addWidget(self.chk_has_journal)
        filter_row.addWidget(self.btn_tag_filter)
        filter_row.addWidget(self.btn_reset)
        filter_row.addSpacing(8)
        filter_row.addWidget(self.btn_generate_brief)

        # Quick-select date presets on a second row to keep the main
        # filter row from getting too crowded.
        preset_row = QHBoxLayout()
        preset_row.setContentsMargins(0, 0, 0, 0)
        preset_row.setSpacing(4)
        preset_row.addWidget(QLabel("Quick range:", self))
        for label in DATE_PRESETS:
            preset_row.addWidget(self.preset_buttons[label])
        preset_row.addStretch()

        # ---- left pane: trade list ----------------------------------------
        self.model = JournalTradeModel()
        self.table = QTableView(self)
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        self.table.selectionModel().currentRowChanged.connect(
            self._on_current_row_changed
        )
        self.table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.table.customContextMenuRequested.connect(
            self._on_list_context_menu
        )

        # ---- right pane: editor + chips -----------------------------------
        self.title_label = QLabel("", self)
        self.title_label.setObjectName("sectionTitle")
        self.subtitle_label = QLabel("", self)
        self.subtitle_label.setObjectName("hintLabel")

        self.editor = JournalEditor(
            self._get_conn,
            lambda: self._current_trade_id,
            self,
            settings=self._settings,
        )
        self.editor.textChanged.connect(self._on_text_changed)

        # Rich-text formatting toolbar — bold / italic / font size /
        # colors / lists / set-as-default. Sits above the editor with
        # the existing Draw button tacked on the right.
        self.format_toolbar = RichTextToolbar(
            self.editor, self, settings=self._settings,
        )

        self.btn_draw = QPushButton("✏ Draw", self)
        self.btn_draw.setToolTip(
            "Open a blank canvas; the drawing is inserted into this entry "
            "as an attachment when you click OK."
        )
        self.btn_draw.clicked.connect(self._on_draw_clicked)
        editor_toolbar = QHBoxLayout()
        editor_toolbar.setContentsMargins(0, 0, 0, 0)
        editor_toolbar.setSpacing(6)
        editor_toolbar.addWidget(self.format_toolbar, 1)
        editor_toolbar.addWidget(self.btn_draw)

        self.chip_strip = TagChipStrip(self)
        self.chip_strip.add_requested.connect(self._on_pick_tags)
        self.chip_strip.tags_changed.connect(self._on_tags_changed)

        # Find/Replace bar — hidden until Ctrl+F.
        self.find_bar = FindBar(self.editor, self)

        right_widget = QWidget(self)
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(8, 4, 4, 4)
        right_layout.setSpacing(6)
        right_layout.addWidget(self.title_label)
        right_layout.addWidget(self.subtitle_label)
        right_layout.addLayout(editor_toolbar)
        right_layout.addWidget(self.find_bar)
        right_layout.addWidget(self.editor, 1)
        right_layout.addWidget(self.chip_strip)

        # Empty-state label that takes the right pane when no trade selected.
        self.empty_label = QLabel(
            "Select a trade on the left to view or edit its journal entry.",
            self,
        )
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setObjectName("emptyState")

        self.right_stack = QStackedWidget(self)
        self.right_stack.addWidget(right_widget)   # index 0
        self.right_stack.addWidget(self.empty_label)  # index 1
        self.right_stack.setCurrentIndex(1)

        # ---- splitter -----------------------------------------------------
        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.addWidget(self.table)
        self.splitter.addWidget(self.right_stack)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 3)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(8)
        outer.addLayout(filter_row)
        outer.addLayout(preset_row)
        outer.addWidget(self.splitter, 1)

        # Track unsaved-edit state for autosave on selection change.
        self._notes_dirty = False

        self._restore_settings()
        self.refresh()

    # ---- public -----------------------------------------------------------

    def refresh(
        self, preloaded_trades: Optional[list[dict]] = None,
    ) -> None:
        """Reload trades, tag-list, then re-apply filters.

        Pass ``preloaded_trades`` to reuse a fetch the caller already did.
        """
        self.chip_strip.set_known_tags(
            db_manager.fetch_all_tags(self._get_conn())
        )
        conn = self._get_conn()
        if preloaded_trades is not None:
            self._all_trades = preloaded_trades
        else:
            self._all_trades = db_manager.fetch_trades_for_display(conn)
        # Re-seed the journal-id cache from ground truth; from here
        # `_save_current_notes` maintains it incrementally.
        self._journal_ids = db_manager.trade_ids_with_journal(conn)
        # Keep the ticker picker in sync with the current trade universe.
        symbols = sorted({
            (t.get("symbol") or "").upper()
            for t in self._all_trades
            if t.get("symbol")
        })
        self._suspend_autosave = True
        try:
            self.combo_symbols.set_symbols(symbols)
        finally:
            self._suspend_autosave = False
        self._apply_filters_and_render()

    def flush_pending_save(self) -> None:
        """Force-save any unsaved notes — called by MainWindow on close."""
        self._save_current_notes()

    # ---- filtering --------------------------------------------------------

    def _on_filters_changed(self) -> None:
        self._apply_filters_and_render()

    def _on_date_edit_changed(self, _qd) -> None:
        # A manual edit invalidates whichever preset was highlighted.
        if not self._suspend_autosave:
            self._set_active_preset(None)
        self._apply_filters_and_render()

    def _on_reset_filters(self) -> None:
        today = date.today()
        self._suspend_autosave = True
        try:
            self.search_edit.clear()
            self.date_from.setDate(QDate(1990, 1, 1))
            self.date_to.setDate(QDate(today.year, today.month, today.day))
            self.chk_has_journal.setChecked(False)
            self._tag_filter_ids = []
            self.btn_tag_filter.setText("Tag filter…")
            self.combo_symbols.clear_selection()
            self._set_active_preset(PRESET_ALL)
        finally:
            self._suspend_autosave = False
        self._apply_filters_and_render()

    def _set_active_preset(self, label: Optional[str]) -> None:
        for name, btn in self.preset_buttons.items():
            btn.setChecked(name == label)

    def _apply_date_preset(self, label: str) -> None:
        """Set From/To from a quick-select label and re-filter."""
        today = date.today()
        if label == PRESET_ALL:
            # Truly unbounded — any trade with a parseable entry/exit
            # date passes. Cheaper than running a SELECT MIN(entry_time)
            # and stays correct as new trades come in.
            start, end = date(1990, 1, 1), date(2100, 12, 31)
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
        self._suspend_autosave = True
        try:
            self.date_from.setDate(QDate(start.year, start.month, start.day))
            self.date_to.setDate(QDate(end.year, end.month, end.day))
            self._set_active_preset(label)
        finally:
            self._suspend_autosave = False
        self._apply_filters_and_render()

    def _on_tag_filter_clicked(self) -> None:
        dlg = TagPickerDialog(self._get_conn, self._tag_filter_ids, self)
        dlg.setWindowTitle("Filter by tag")
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._tag_filter_ids = dlg.selected_ids()
            n = len(self._tag_filter_ids)
            self.btn_tag_filter.setText(
                "Tag filter…" if n == 0
                else f"Tag filter: {n}"
            )
            self.refresh()  # tag list may have new/deleted entries

    def _apply_filters_and_render(self) -> None:
        conn = self._get_conn()
        # Cached — kept in sync by `_save_current_notes`; see __init__.
        journal_ids = self._journal_ids

        # Each id-based filter contributes a constraint set; we intersect
        # them all up front and then run a single pass over the trade list
        # combining the id filter with the (in-memory) date range. This
        # replaces the previous 4-pass list-rebuild chain.
        allowed_ids: Optional[set[int]] = None

        def _intersect(s: set[int]) -> None:
            nonlocal allowed_ids
            allowed_ids = set(s) if allowed_ids is None else (allowed_ids & s)

        if self.chk_has_journal.isChecked():
            _intersect(journal_ids)

        query = self.search_edit.text().strip()
        if query:
            _intersect(db_manager.search_journal_entries(conn, query))

        if self._tag_filter_ids:
            _intersect(
                db_manager.trade_ids_with_any_tag(conn, self._tag_filter_ids),
            )

        d_from = self.date_from.date().toPython()
        d_to = self.date_to.date().toPython()
        date_active = d_from > date(1990, 1, 1) or d_to < date(2100, 12, 31)

        def _in_range(t: dict) -> bool:
            ed = self._exit_date(t) or self._entry_date(t)
            if ed is None:
                return False
            return d_from <= ed <= d_to

        selected_symbols = {
            s.upper() for s in self.combo_symbols.selected_symbols()
        }

        trades = [
            t for t in self._all_trades
            if (allowed_ids is None or t["id"] in allowed_ids)
            and (not date_active or _in_range(t))
            and (
                not selected_symbols
                or (t.get("symbol") or "").upper() in selected_symbols
            )
        ]

        # Preserve selection if possible.
        prev_id = self._current_trade_id
        self._suspend_autosave = True
        try:
            self.model.set_trades(trades, journal_ids)
        finally:
            self._suspend_autosave = False

        if prev_id is not None:
            row = self.model.find_row_by_id(prev_id)
            if row >= 0:
                self.table.selectRow(row)
                return
        # No prior selection still in view — clear right pane.
        self._current_trade_id = None
        self.right_stack.setCurrentIndex(1)

    @staticmethod
    def _exit_date(t: dict) -> Optional[date]:
        v = t.get("exit_time")
        if v is None:
            return None
        return v.date() if isinstance(v, datetime) else None

    @staticmethod
    def _entry_date(t: dict) -> Optional[date]:
        v = t.get("entry_time")
        if v is None:
            return None
        return v.date() if isinstance(v, datetime) else None

    # ---- selection / autosave --------------------------------------------

    def _on_current_row_changed(self, current, _previous) -> None:
        if self._suspend_autosave:
            return
        # Save the previous entry first.
        self._save_current_notes()
        if not current.isValid():
            self._current_trade_id = None
            self.right_stack.setCurrentIndex(1)
            return
        row = self.model.row_at(current.row())
        if row is None:
            self._current_trade_id = None
            self.right_stack.setCurrentIndex(1)
            return
        self._load_trade(row)

    def _on_text_changed(self) -> None:
        if self._suspend_autosave:
            return
        self._notes_dirty = True

    def _on_tags_changed(self, ids: list[int]) -> None:
        if self._suspend_autosave or self._current_trade_id is None:
            return
        db_manager.set_trade_tags(
            self._get_conn(), self._current_trade_id, ids,
        )

    def _load_trade(self, trade: dict) -> None:
        self._suspend_autosave = True
        try:
            self._current_trade_id = trade["id"]
            self._notes_dirty = False
            html = db_manager.fetch_journal_entry(
                self._get_conn(), trade["id"],
            )
            self.editor.setHtml(html or "")
            tag_ids = db_manager.fetch_trade_tag_ids(
                self._get_conn(), trade["id"],
            )
            self.chip_strip.set_selected_ids(tag_ids)
            self._set_title(trade)
            self.right_stack.setCurrentIndex(0)
            if self._settings is not None:
                self._settings.setValue(
                    SETTINGS_LAST_TRADE_ID, int(trade["id"]),
                )
        finally:
            self._suspend_autosave = False

    def _set_title(self, t: dict) -> None:
        sym = t.get("symbol", "")
        direction = t.get("direction", "")
        entry = t.get("avg_entry_price")
        exit_ = t.get("avg_exit_price")
        net = t.get("net_pnl")
        title = f"{sym}  ·  {direction}"
        if entry is not None:
            title += f"  ·  Entry ${entry:.4f}"
        if exit_ is not None:
            title += f" → Exit ${exit_:.4f}"
        self.title_label.setText(title)

        et = t.get("entry_time")
        et_str = (
            et.strftime("%Y-%m-%d %H:%M") if isinstance(et, datetime) else ""
        )
        sub = f"Entered {et_str}"
        if net is not None:
            sign = "-" if net < 0 else ""
            sub += f"  ·  Net {sign}${abs(net):,.2f}"
        self.subtitle_label.setText(sub)

    def _save_current_notes(self) -> None:
        if (
            self._current_trade_id is None
            or not self._notes_dirty
        ):
            return
        html = self.editor.toHtml()
        db_manager.save_journal_entry(
            self._get_conn(), self._current_trade_id, html,
        )
        self._notes_dirty = False
        self.journal_saved.emit(self._current_trade_id)

        # Incrementally keep the cached journal-id set in sync: a save
        # with non-blank content adds the id; a blank save deletes the
        # underlying row (see `save_journal_entry`) so we remove it.
        tid = self._current_trade_id
        if (html or "").strip():
            self._journal_ids.add(tid)
        else:
            self._journal_ids.discard(tid)

        # Refresh the 📝 indicator on the just-saved row using the
        # cached set — no DB round-trip.
        row = self.model.find_row_by_id(tid)
        if row >= 0:
            self._suspend_autosave = True
            try:
                self.model.set_trades(self.model._rows, self._journal_ids)
            finally:
                self._suspend_autosave = False
            self.table.selectRow(row)

    # ---- draw -------------------------------------------------------------

    def _on_draw_clicked(self) -> None:
        if self._current_trade_id is None:
            return
        dlg = DrawDialog.blank(parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        if not dlg.has_strokes():
            return  # blank submission → don't litter the DB
        img = dlg.result_image()
        if img.isNull():
            return
        self.editor.insert_drawing(img)
        self._notes_dirty = True
        self._save_current_notes()

    # ---- tag picker -------------------------------------------------------

    def _on_pick_tags(self) -> None:
        if self._current_trade_id is None:
            return
        current_ids = self.chip_strip.selected_ids()
        dlg = TagPickerDialog(self._get_conn, current_ids, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            new_ids = dlg.selected_ids()
            db_manager.set_trade_tags(
                self._get_conn(), self._current_trade_id, new_ids,
            )
            # Reload tag master list (new tags may have been created).
            self.chip_strip.set_known_tags(
                db_manager.fetch_all_tags(self._get_conn())
            )
            self.chip_strip.set_selected_ids(new_ids)

    # ---- settings round-trip ---------------------------------------------

    def _restore_settings(self) -> None:
        if self._settings is None:
            return
        sp = self._settings.value(SETTINGS_SPLITTER)
        if sp is not None:
            try:
                self.splitter.restoreState(sp)
            except (TypeError, ValueError):
                pass
        try:
            checked = self._settings.value(SETTINGS_HAS_JOURNAL_ONLY, False)
            if isinstance(checked, str):
                checked = checked.lower() in ("true", "1")
            self.chk_has_journal.setChecked(bool(checked))
        except (TypeError, ValueError):
            pass

    def save_settings(self) -> None:
        if self._settings is None:
            return
        self._settings.setValue(SETTINGS_SPLITTER, self.splitter.saveState())
        self._settings.setValue(
            SETTINGS_HAS_JOURNAL_ONLY, self.chk_has_journal.isChecked(),
        )

    # ---- brief generation -------------------------------------------------

    def _current_filters(self, *, include_images: bool = False) -> BriefFilters:
        """Build a BriefFilters snapshot from the current filter bar."""
        d_from = self.date_from.date().toPython()
        d_to = self.date_to.date().toPython()
        symbols = [s.upper() for s in self.combo_symbols.selected_symbols()]
        tag_names: list[str] = []
        if self._tag_filter_ids:
            name_by_id = {
                t["id"]: t["name"]
                for t in db_manager.fetch_all_tags(self._get_conn())
            }
            tag_names = [
                name_by_id[i] for i in self._tag_filter_ids if i in name_by_id
            ]
        return BriefFilters(
            date_from=d_from,
            date_to=d_to,
            symbols=symbols,
            tag_ids=list(self._tag_filter_ids),
            tag_names=tag_names,
            search_text=self.search_edit.text().strip(),
            include_images=include_images,
        )

    def _on_generate_brief(self) -> None:
        # Flush any in-flight notes first so the generator sees fresh text.
        self._save_current_notes()
        base_filters = self._current_filters(include_images=False)

        # Peek at the match count so the dialog can surface it before the
        # user commits.
        title, _html_preview, matching_trades = briefs_mod.generate_brief(
            self._get_conn(), base_filters,
        )

        dlg = GenerateBriefDialog(
            base_filters,
            trade_count=len(matching_trades),
            parent=self,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        choice = dlg.choice()

        # Regenerate with the final "include images" flag chosen by the user.
        final_filters = self._current_filters(
            include_images=choice.include_images,
        )
        title_final, html_final, _ = briefs_mod.generate_brief(
            self._get_conn(), final_filters,
        )
        # User-override wins over the derived title.
        if choice.title and choice.title != title_final:
            title_final = choice.title

        brief_id: Optional[int] = None
        if choice.save_to_briefs:
            brief_id = db_manager.insert_brief(
                self._get_conn(), title_final, html_final,
            )

        if choice.export_to_file:
            self._export_brief_to_file(
                title_final, html_final, choice.export_format,
            )

        if brief_id is not None:
            self.brief_saved.emit(brief_id)
            QMessageBox.information(
                self, "Brief generated",
                f"Saved “{title_final}” to the Briefs tab.",
            )
        elif choice.export_to_file:
            # File-only path: the export helper already showed a dialog on
            # failure; nothing else to do.
            pass

    def _export_brief_to_file(
        self, title: str, html: str, fmt: str,
    ) -> None:
        ext = exporters.extension_for_format(fmt)
        start_dir = ""
        if self._settings is not None:
            start_dir = str(self._settings.value(SETTINGS_LAST_EXPORT_DIR, ""))
        start_path = str(Path(start_dir) / f"{title}{ext}") if start_dir else (
            f"{title}{ext}"
        )
        filter_str = (
            f"{dict(exporters.EXPORT_FORMATS)[fmt]} (*{ext});;All files (*.*)"
        )
        out_path, _sel = QFileDialog.getSaveFileName(
            self, "Export brief", start_path, filter_str,
        )
        if not out_path:
            return
        try:
            exporters.export_document(
                html, title, fmt, out_path,
                fetch_attachment=exporters.make_attachment_fetcher(
                    self._get_conn(),
                ),
                include_images=True,
            )
        except Exception as e:
            QMessageBox.critical(
                self, "Export failed",
                f"Could not write {out_path}:\n\n{type(e).__name__}: {e}",
            )
            return
        if self._settings is not None:
            self._settings.setValue(
                SETTINGS_LAST_EXPORT_DIR, str(Path(out_path).parent),
            )

    # ---- context menu on the trade list: Export… -------------------------

    def _on_list_context_menu(self, pos: QPoint) -> None:
        idx = self.table.indexAt(pos)
        if not idx.isValid():
            return
        row = self.model.row_at(idx.row())
        if row is None:
            return
        trade_id = int(row["id"])
        has_entry = db_manager.has_journal_entry(self._get_conn(), trade_id)

        menu = QMenu(self)
        export_action = menu.addAction("Export…")
        export_action.setEnabled(has_entry)
        if not has_entry:
            export_action.setToolTip("No journal entry to export")
        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))
        if chosen == export_action:
            self._export_journal_entry(row)

    def _export_journal_entry(self, trade: dict) -> None:
        """Right-click export of a single trade's journal entry."""
        # Flush any pending edit on this trade before reading from the DB.
        if self._current_trade_id == int(trade["id"]):
            self._save_current_notes()

        html = db_manager.fetch_journal_entry(
            self._get_conn(), int(trade["id"]),
        )
        if not html:
            return

        # Default filename: {SYMBOL}_{entry_date}_{direction}
        symbol = (trade.get("symbol") or "UNKNOWN").upper()
        direction = trade.get("direction") or ""
        et = trade.get("entry_time")
        entry_date = (
            et.date().isoformat() if isinstance(et, datetime) else ""
        )
        parts = [p for p in (symbol, entry_date, direction) if p]
        default_name = "_".join(parts) or f"journal_{trade['id']}"

        dlg = ExportDocumentDialog(default_name, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        fmt = dlg.selected_format()
        name = dlg.filename() or default_name
        ext = dlg.selected_extension()

        start_dir = ""
        if self._settings is not None:
            start_dir = str(self._settings.value(SETTINGS_LAST_EXPORT_DIR, ""))
        start_path = (
            str(Path(start_dir) / f"{name}{ext}") if start_dir
            else f"{name}{ext}"
        )
        filter_str = (
            f"{dict(exporters.EXPORT_FORMATS)[fmt]} (*{ext});;All files (*.*)"
        )
        out_path, _sel = QFileDialog.getSaveFileName(
            self, "Export journal entry", start_path, filter_str,
        )
        if not out_path:
            return

        title = f"{symbol} — {direction} — {entry_date}" if parts else name
        try:
            exporters.export_document(
                html, title, fmt, out_path,
                fetch_attachment=exporters.make_attachment_fetcher(
                    self._get_conn(),
                ),
                include_images=True,
            )
        except Exception as e:
            QMessageBox.critical(
                self, "Export failed",
                f"Could not write {out_path}:\n\n{type(e).__name__}: {e}",
            )
            return
        if self._settings is not None:
            self._settings.setValue(
                SETTINGS_LAST_EXPORT_DIR, str(Path(out_path).parent),
            )
