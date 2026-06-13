"""Strategies tab — long-form image-heavy playbooks.

Layout mirrors the Briefs tab but adds three navigation aids built for
managing very large pages with lots of screenshots:

    * Outline pane on the right — a clickable list of every H1/H2/H3
      heading in the document. Click → editor scrolls to that block.
    * Image thumbnail strip below the editor — click any thumbnail
      to scroll to that screenshot. The thumbnails come straight from
      the document's image fragments (no extra DB queries).
    * Collapse / expand all headings — toolbar buttons that fold
      every heading-introduced section so you can see the document
      skeleton, then expand back when you want to read.

Like briefs, strategies use the same ``JournalEditor`` so attachments
work identically (paste image, drag file, Ctrl+wheel scale, Annotate).
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import (
    QAbstractTableModel, QDate, QModelIndex, QPoint, QSettings, Qt, Signal,
)
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView, QDateEdit, QDialog, QFileDialog, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QLineEdit, QMenu, QMessageBox,
    QPushButton, QSplitter, QStackedWidget, QTableView, QToolButton,
    QVBoxLayout, QWidget,
)

from export import exporters
from gui.dialogs.draw_dialog import DrawDialog
from gui.dialogs.export_document import ExportDocumentDialog
from gui.settings_keys import (
    STRATEGIES_LAST_EXPORT_DIR, STRATEGIES_LAST_ID, STRATEGIES_SHOW_OUTLINE,
    STRATEGIES_SHOW_THUMBS, STRATEGIES_SPLITTER, STRATEGIES_THUMB_SIZE_INDEX,
)
from gui.widgets.find_bar import FindBar
from gui.widgets.journal_editor import JournalEditor
from gui.widgets.rich_text_toolbar import RichTextToolbar
from gui.widgets.strategy_navigator import (
    DEFAULT_THUMB_SIZE_INDEX, ImageThumbStrip, OutlineWidget, THUMB_SIZES,
    scroll_editor_to_block,
)
from gui.widgets.thumbnail_tag_menu import (
    ManageThumbnailTagsDialog, ThumbTag, build_thumbnail_tag_submenu,
)
from ingest import db_manager

_DOC_TYPE = "strategy"


# Date filter sentinels — match Briefs / Journal conventions.
_UNBOUNDED_FROM = date(1990, 1, 1)
_UNBOUNDED_TO = date(2100, 12, 31)

PRESET_ALL = "All"
PRESET_TODAY = "Today"
PRESET_WEEK = "One Week"
PRESET_MONTH = "One Month"
PRESET_YEAR = "One Year"
DATE_PRESETS = [PRESET_ALL, PRESET_TODAY, PRESET_WEEK, PRESET_MONTH, PRESET_YEAR]


def _utc_to_local(dt: datetime) -> datetime:
    """Same UTC→local conversion as the Briefs tab — SQLite emits
    naive UTC for ``CURRENT_TIMESTAMP`` and the user thinks in local
    time."""
    if dt.tzinfo is not None:
        return dt.astimezone().replace(tzinfo=None)
    return dt.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)


def _local_date_of(v) -> Optional[date]:
    if isinstance(v, datetime):
        return _utc_to_local(v).date()
    if isinstance(v, date):
        return v
    if isinstance(v, str) and v:
        try:
            parsed = datetime.fromisoformat(v.replace(" ", "T", 1))
        except ValueError:
            return None
        return _utc_to_local(parsed).date()
    return None


# ---- list model -----------------------------------------------------------


class _StrategyListModel(QAbstractTableModel):
    _COLS = ("Title", "Last edited")

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list[dict] = []

    def set_rows(self, rows: list[dict]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(self._COLS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if (
            orientation == Qt.Orientation.Horizontal
            and role == Qt.ItemDataRole.DisplayRole
        ):
            return self._COLS[section]
        return None

    def data(self, idx: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not idx.isValid() or not (0 <= idx.row() < len(self._rows)):
            return None
        row = self._rows[idx.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            if idx.column() == 0:
                return row.get("title") or "(untitled)"
            if idx.column() == 1:
                v = row.get("updated_at")
                if isinstance(v, datetime):
                    return _utc_to_local(v).strftime("%Y-%m-%d %H:%M")
                return str(v or "")
        return None

    def row_at(self, r: int) -> Optional[dict]:
        if 0 <= r < len(self._rows):
            return self._rows[r]
        return None

    def find_row_by_id(self, sid: int) -> int:
        for i, r in enumerate(self._rows):
            if int(r["id"]) == int(sid):
                return i
        return -1


# ---- tab ------------------------------------------------------------------


class StrategiesTab(QWidget):
    """Long-form playbooks with outline + thumb strip + section folds."""

    strategy_updated = Signal(int)

    def __init__(
        self,
        get_conn: Callable[[], sqlite3.Connection],
        settings: Optional[QSettings] = None,
        parent=None,
    ):
        super().__init__(parent)
        self._get_conn = get_conn
        self._settings = settings

        self._current_id: Optional[int] = None
        self._suspend_autosave = False
        self._content_dirty = False
        self._title_dirty = False
        # Track which heading blocks are currently folded so the
        # toolbar's expand-all action knows what to flip back.
        self._folded_block_nos: set[int] = set()
        # Per-strategy opt-in thumbnail set, loaded from the DB on
        # selection change and mutated by the right-click menus on the
        # editor and the strip.
        self._thumbnail_ids: set[int] = set()
        # Thumbnail tag state. ``_available_tags`` is the global preset
        # pool (shared with Briefs via thumbnail_tag_presets_changed);
        # ``_tag_map`` is the per-strategy attachment_id → {tag_id}
        # mapping, reloaded on every selection change.
        self._available_tags: list[ThumbTag] = []
        self._tag_map: dict[int, set[int]] = {}

        # ---- top filter row ---------------------------------------------
        self.search_edit = QLineEdit(self)
        self.search_edit.setPlaceholderText(
            "Search strategies (title + content)…",
        )
        self.search_edit.textChanged.connect(self._apply_filters_and_render)

        today = date.today()
        self.date_from = QDateEdit(self)
        self.date_from.setCalendarPopup(True)
        self.date_from.setDisplayFormat("yyyy-MM-dd")
        self.date_from.setMinimumDate(QDate(1990, 1, 1))
        self.date_from.setMaximumDate(QDate(2100, 12, 31))
        self.date_from.setDate(QDate(1990, 1, 1))
        self.date_from.dateChanged.connect(self._on_date_edit_changed)

        self.date_to = QDateEdit(self)
        self.date_to.setCalendarPopup(True)
        self.date_to.setDisplayFormat("yyyy-MM-dd")
        self.date_to.setMinimumDate(QDate(1990, 1, 1))
        self.date_to.setMaximumDate(QDate(2100, 12, 31))
        self.date_to.setDate(QDate(today.year, today.month, today.day))
        self.date_to.dateChanged.connect(self._on_date_edit_changed)

        self.preset_buttons: dict[str, QPushButton] = {}
        for label in DATE_PRESETS:
            btn = QPushButton(label, self)
            btn.setCheckable(True)
            btn.setAutoExclusive(False)
            btn.clicked.connect(
                lambda _checked=False, name=label: self._apply_date_preset(name)
            )
            self.preset_buttons[label] = btn

        self.btn_new = QPushButton("+ New strategy", self)
        self.btn_new.clicked.connect(self._on_new_empty)

        self.btn_reset = QPushButton("Reset", self)
        self.btn_reset.clicked.connect(self._on_reset_filters)

        filter_row = QHBoxLayout()
        filter_row.setContentsMargins(0, 0, 0, 0)
        filter_row.addWidget(self.search_edit, 2)
        filter_row.addWidget(QLabel("From:", self))
        filter_row.addWidget(self.date_from)
        filter_row.addWidget(QLabel("To:", self))
        filter_row.addWidget(self.date_to)
        filter_row.addWidget(self.btn_reset)
        filter_row.addSpacing(8)
        filter_row.addWidget(self.btn_new)

        preset_row = QHBoxLayout()
        preset_row.setContentsMargins(0, 0, 0, 0)
        preset_row.setSpacing(4)
        preset_row.addWidget(QLabel("Quick range:", self))
        for label in DATE_PRESETS:
            preset_row.addWidget(self.preset_buttons[label])
        preset_row.addStretch()

        # ---- strategy list (left pane) ---------------------------------
        self.model = _StrategyListModel()
        self.table = QTableView(self)
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows,
        )
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection,
        )
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers,
        )
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        self.table.selectionModel().currentRowChanged.connect(
            self._on_current_row_changed,
        )
        self.table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu,
        )
        self.table.customContextMenuRequested.connect(
            self._on_list_context_menu,
        )

        # ---- editor pane ------------------------------------------------
        self.title_edit = QLineEdit(self)
        self.title_edit.setPlaceholderText("Strategy title…")
        self.title_edit.textEdited.connect(self._on_title_edited)

        self.editor = JournalEditor(
            self._get_conn,
            lambda: self._editor_token(),
            self,
            settings=self._settings,
        )
        self.editor.textChanged.connect(self._on_content_edited)
        self.editor.cursorPositionChanged.connect(self._refresh_navigator)

        self.format_toolbar = RichTextToolbar(
            self.editor, self, settings=self._settings,
        )

        # Outline + thumbnail strip toggles.
        self.btn_toggle_outline = QToolButton(self)
        self.btn_toggle_outline.setText("☰ Outline")
        self.btn_toggle_outline.setCheckable(True)
        self.btn_toggle_outline.setToolTip(
            "Show / hide the outline pane on the right"
        )
        self.btn_toggle_outline.toggled.connect(self._on_toggle_outline)

        self.btn_toggle_thumbs = QToolButton(self)
        self.btn_toggle_thumbs.setText("🖼 Thumbnails")
        self.btn_toggle_thumbs.setCheckable(True)
        self.btn_toggle_thumbs.setToolTip(
            "Show / hide the screenshot thumbnail strip"
        )
        self.btn_toggle_thumbs.toggled.connect(self._on_toggle_thumbs)

        # Collapse / expand-all heading sections.
        self.btn_collapse_all = QToolButton(self)
        self.btn_collapse_all.setText("⊟ Collapse all")
        self.btn_collapse_all.setToolTip(
            "Fold every heading-introduced section so the document "
            "skeleton fits on one screen"
        )
        self.btn_collapse_all.clicked.connect(self._on_collapse_all)

        self.btn_expand_all = QToolButton(self)
        self.btn_expand_all.setText("⊞ Expand all")
        self.btn_expand_all.setToolTip(
            "Reveal every previously collapsed section"
        )
        self.btn_expand_all.clicked.connect(self._on_expand_all)

        self.btn_draw = QPushButton("✏ Draw", self)
        self.btn_draw.setToolTip(
            "Open a blank canvas; the drawing is inserted into this "
            "strategy as an attachment when you click OK."
        )
        self.btn_draw.clicked.connect(self._on_draw_clicked)

        self.btn_delete = QPushButton("Delete strategy", self)
        self.btn_delete.clicked.connect(self._on_delete_clicked)

        actions_row = QHBoxLayout()
        actions_row.setContentsMargins(0, 0, 0, 0)
        actions_row.setSpacing(6)
        actions_row.addWidget(self.format_toolbar, 1)
        actions_row.addWidget(self.btn_toggle_outline)
        actions_row.addWidget(self.btn_toggle_thumbs)
        actions_row.addWidget(self.btn_collapse_all)
        actions_row.addWidget(self.btn_expand_all)
        actions_row.addWidget(self.btn_draw)
        actions_row.addWidget(self.btn_delete)

        self.find_bar = FindBar(self.editor, self)

        # Outline + thumbnail navigation widgets.
        self.outline = OutlineWidget(self)
        self.outline.set_editor(self.editor)
        self.outline.heading_clicked.connect(self._on_heading_clicked)

        self.thumb_strip = ImageThumbStrip(self)
        self.thumb_strip.set_editor(self.editor)
        self.thumb_strip.image_clicked.connect(self._on_image_clicked)
        self.thumb_strip.thumb_remove_requested.connect(
            self._on_thumb_remove_requested,
        )
        self.thumb_strip.size_changed.connect(self._on_thumb_size_changed)
        # Thumbnail-tag plumbing (chip rail toggles + per-thumb
        # right-click submenu). Add/manage requests bubble up here
        # because the strip is DB-agnostic by design.
        self.thumb_strip.attachment_tags_changed.connect(
            self._on_attachment_tag_toggled,
        )
        self.thumb_strip.add_tag_requested.connect(
            self._on_add_tag_via_strip,
        )
        self.thumb_strip.manage_tags_requested.connect(
            self._open_manage_tags_dialog,
        )

        # Inject the "Add/Remove from thumbnails" entries into the
        # editor's image right-click menu. Builder is recreated on
        # every right-click so it sees the current set.
        self.editor.image_context_menu_builder = self._build_image_menu

        # Editor + thumbnail strip stacked vertically.
        editor_col = QWidget(self)
        editor_col_layout = QVBoxLayout(editor_col)
        editor_col_layout.setContentsMargins(0, 0, 0, 0)
        editor_col_layout.setSpacing(4)
        editor_col_layout.addWidget(self.title_edit)
        editor_col_layout.addLayout(actions_row)
        editor_col_layout.addWidget(self.find_bar)
        editor_col_layout.addWidget(self.editor, 1)
        editor_col_layout.addWidget(self.thumb_strip)

        # Editor column + outline pane in a horizontal splitter so the
        # user can resize the outline.
        self.right_splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.right_splitter.addWidget(editor_col)
        self.right_splitter.addWidget(self.outline)
        self.right_splitter.setStretchFactor(0, 4)
        self.right_splitter.setStretchFactor(1, 1)

        right_widget = QWidget(self)
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(8, 4, 4, 4)
        right_layout.setSpacing(6)
        right_layout.addWidget(self.right_splitter, 1)

        self.empty_label = QLabel(
            "Select a strategy on the left, or click + New strategy.",
            self,
        )
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setObjectName("emptyState")

        self.right_stack = QStackedWidget(self)
        self.right_stack.addWidget(right_widget)
        self.right_stack.addWidget(self.empty_label)
        self.right_stack.setCurrentIndex(1)

        # Outer left/right splitter (list ↔ editor pane).
        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.addWidget(self.table)
        self.splitter.addWidget(self.right_stack)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 4)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(8)
        outer.addLayout(filter_row)
        outer.addLayout(preset_row)
        outer.addWidget(self.splitter, 1)

        self._restore_settings()
        self.refresh_thumbnail_tag_presets()
        self.refresh()

    # ---- public API -------------------------------------------------------

    def refresh(self) -> None:
        self._apply_filters_and_render()

    def refresh_thumbnail_tag_presets(self) -> None:
        """Re-pull the global thumbnail tag preset list and push it
        into the strip. Called on construction and via the MainWindow
        ``thumbnail_tag_presets_changed`` broadcast so a preset added
        in Briefs shows up here immediately."""
        rows = db_manager.fetch_thumbnail_tags(self._get_conn())
        self._available_tags = [
            ThumbTag(id=r["id"], name=r["name"]) for r in rows
        ]
        self.thumb_strip.set_available_tags(self._available_tags)

    def select_strategy(self, sid: int) -> None:
        self.refresh()
        row = self.model.find_row_by_id(sid)
        if row >= 0:
            self.table.selectRow(row)

    def flush_pending_save(self) -> None:
        self._save_current()

    def _editor_token(self) -> Optional[int]:
        return self._current_id

    # ---- filters ----------------------------------------------------------

    def _apply_filters_and_render(self) -> None:
        conn = self._get_conn()
        rows = db_manager.fetch_strategies(conn)

        query = self.search_edit.text().strip()
        if query:
            match_ids = db_manager.search_strategies(conn, query)
            rows = [r for r in rows if r["id"] in match_ids]

        d_from = self.date_from.date().toPython()
        d_to = self.date_to.date().toPython()
        if d_from > _UNBOUNDED_FROM or d_to < _UNBOUNDED_TO:
            def _in_range(r: dict) -> bool:
                d = _local_date_of(r.get("updated_at"))
                if d is None:
                    return True
                return d_from <= d <= d_to
            rows = [r for r in rows if _in_range(r)]

        prev_id = self._current_id
        self._suspend_autosave = True
        try:
            self.model.set_rows(rows)
        finally:
            self._suspend_autosave = False

        if prev_id is not None:
            row = self.model.find_row_by_id(prev_id)
            if row >= 0:
                self.table.selectRow(row)
                return
        self._current_id = None
        self.right_stack.setCurrentIndex(1)

    def _on_reset_filters(self) -> None:
        today = date.today()
        self._suspend_autosave = True
        try:
            self.search_edit.clear()
            self.date_from.setDate(QDate(1990, 1, 1))
            self.date_to.setDate(QDate(today.year, today.month, today.day))
            self._set_active_preset(PRESET_ALL)
        finally:
            self._suspend_autosave = False
        self._apply_filters_and_render()

    def _set_active_preset(self, label: Optional[str]) -> None:
        for name, btn in self.preset_buttons.items():
            btn.setChecked(name == label)

    def _on_date_edit_changed(self, _qd) -> None:
        if not self._suspend_autosave:
            self._set_active_preset(None)
        self._apply_filters_and_render()

    def _apply_date_preset(self, label: str) -> None:
        today = date.today()
        if label == PRESET_ALL:
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

    # ---- selection / autosave --------------------------------------------

    def _on_current_row_changed(self, current, _previous) -> None:
        if self._suspend_autosave:
            return
        self._save_current()
        if not current.isValid():
            self._current_id = None
            self.right_stack.setCurrentIndex(1)
            return
        row = self.model.row_at(current.row())
        if row is None:
            self._current_id = None
            self.right_stack.setCurrentIndex(1)
            return
        self._load(int(row["id"]))

    def _load(self, sid: int) -> None:
        self._suspend_autosave = True
        try:
            strat = db_manager.fetch_strategy(self._get_conn(), sid)
            if strat is None:
                self._current_id = None
                self.right_stack.setCurrentIndex(1)
                return
            self._current_id = sid
            self.title_edit.setText(strat.get("title") or "")
            self.editor.setHtml(strat.get("content_html") or "")
            self._content_dirty = False
            self._title_dirty = False
            self._folded_block_nos.clear()
            self.thumb_strip.clear_cache()
            self._thumbnail_ids = db_manager.fetch_strategy_thumbnail_ids(
                self._get_conn(), sid,
            )
            self.thumb_strip.set_thumbnail_filter(self._thumbnail_ids)
            self.editor.set_thumbnail_ids(self._thumbnail_ids)
            # Load this strategy's tag assignments and push to the
            # strip so the chip rail filters against them.
            self._tag_map = db_manager.fetch_doc_thumb_tag_map(
                self._get_conn(), _DOC_TYPE, sid,
            )
            self.thumb_strip.set_doc_tag_map(self._tag_map)
            self.right_stack.setCurrentIndex(0)
            if self._settings is not None:
                self._settings.setValue(STRATEGIES_LAST_ID, int(sid))
        finally:
            self._suspend_autosave = False
        self._refresh_navigator(force=True)

    def _on_title_edited(self, _text: str) -> None:
        if self._suspend_autosave:
            return
        self._title_dirty = True

    def _on_content_edited(self) -> None:
        if self._suspend_autosave:
            return
        self._content_dirty = True
        self._refresh_navigator()

    def _save_current(self) -> None:
        if self._current_id is None:
            return
        if not (self._content_dirty or self._title_dirty):
            return
        title = self.title_edit.text().strip() or "(untitled)"
        html = self.editor.toHtml()
        db_manager.update_strategy(
            self._get_conn(),
            self._current_id,
            title=title if self._title_dirty else None,
            content_html=html if self._content_dirty else None,
        )
        self._title_dirty = False
        self._content_dirty = False
        self.strategy_updated.emit(self._current_id)

        current_id = self._current_id
        self._apply_filters_and_render()
        row = self.model.find_row_by_id(current_id)
        if row >= 0:
            self._suspend_autosave = True
            try:
                self.table.selectRow(row)
            finally:
                self._suspend_autosave = False

    # ---- new / draw / delete ---------------------------------------------

    def _on_new_empty(self) -> None:
        self._save_current()
        name, ok = QInputDialog.getText(
            self, "New strategy", "Title:",
            QLineEdit.EchoMode.Normal,
            "Untitled",
        )
        if not ok:
            return
        name = (name or "").strip()
        if not name:
            return
        sid = db_manager.insert_strategy(self._get_conn(), name, "")
        self.refresh()
        row = self.model.find_row_by_id(sid)
        if row >= 0:
            self.table.selectRow(row)
            self.editor.setFocus()
        self.strategy_updated.emit(sid)

    def _on_draw_clicked(self) -> None:
        if self._current_id is None:
            return
        dlg = DrawDialog.blank(parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        if not dlg.has_strokes():
            return
        img = dlg.result_image()
        if img.isNull():
            return
        self.editor.insert_drawing(img)
        self._content_dirty = True
        self._save_current()

    def _on_delete_clicked(self) -> None:
        if self._current_id is None:
            return
        title = self.title_edit.text().strip() or "(untitled)"
        reply = QMessageBox.question(
            self, "Delete strategy",
            f"Delete “{title}”? This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        sid = self._current_id
        self._suspend_autosave = True
        try:
            self._content_dirty = False
            self._title_dirty = False
            self._current_id = None
            db_manager.delete_strategy(self._get_conn(), sid)
        finally:
            self._suspend_autosave = False
        self.right_stack.setCurrentIndex(1)
        self._apply_filters_and_render()

    # ---- right-click export on list --------------------------------------

    def _on_list_context_menu(self, pos: QPoint) -> None:
        idx = self.table.indexAt(pos)
        if not idx.isValid():
            return
        row = self.model.row_at(idx.row())
        if row is None:
            return
        menu = QMenu(self)
        export_action = menu.addAction("Export…")
        delete_action = menu.addAction("Delete")
        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))
        if chosen == export_action:
            self._export(int(row["id"]))
        elif chosen == delete_action:
            if int(row["id"]) != self._current_id:
                self.table.selectRow(idx.row())
            self._on_delete_clicked()

    def _export(self, sid: int) -> None:
        if sid == self._current_id:
            self._save_current()
        strat = db_manager.fetch_strategy(self._get_conn(), sid)
        if strat is None:
            return
        default_name = (strat.get("title") or f"strategy_{sid}").strip()
        dlg = ExportDocumentDialog(default_name, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        fmt = dlg.selected_format()
        name = dlg.filename() or default_name
        ext = dlg.selected_extension()

        start_dir = ""
        if self._settings is not None:
            start_dir = str(
                self._settings.value(STRATEGIES_LAST_EXPORT_DIR, "")
            )
        start_path = (
            str(Path(start_dir) / f"{name}{ext}") if start_dir
            else f"{name}{ext}"
        )
        filter_str = (
            f"{dict(exporters.EXPORT_FORMATS)[fmt]} (*{ext});;All files (*.*)"
        )
        out_path, _sel = QFileDialog.getSaveFileName(
            self, "Export strategy", start_path, filter_str,
        )
        if not out_path:
            return
        try:
            exporters.export_document(
                strat.get("content_html") or "",
                strat.get("title") or name,
                fmt, out_path,
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
                STRATEGIES_LAST_EXPORT_DIR, str(Path(out_path).parent),
            )

    # ---- navigator wiring -------------------------------------------------

    def _refresh_navigator(self, *, force: bool = False) -> None:
        # Cheap to call on every cursor move; outline rebuild walks
        # blocks linearly. ``force`` is used after document swaps so
        # the strip is re-rendered with fresh thumbnails.
        if not self.outline.isVisible() and not force:
            self.thumb_strip.refresh()
            return
        self.outline.refresh()
        self.thumb_strip.refresh()

    def _on_heading_clicked(self, block_no: int) -> None:
        scroll_editor_to_block(self.editor, block_no)
        self.editor.setFocus()

    def _on_image_clicked(self, block_no: int) -> None:
        scroll_editor_to_block(self.editor, block_no)
        self.editor.setFocus()

    def _on_toggle_outline(self, checked: bool) -> None:
        self.outline.setVisible(checked)
        if checked:
            self.outline.refresh()
        if self._settings is not None:
            self._settings.setValue(STRATEGIES_SHOW_OUTLINE, bool(checked))

    def _on_toggle_thumbs(self, checked: bool) -> None:
        self.thumb_strip.setVisible(checked)
        if checked:
            self.thumb_strip.refresh()
        if self._settings is not None:
            self._settings.setValue(STRATEGIES_SHOW_THUMBS, bool(checked))

    # ---- per-strategy thumbnail set --------------------------------------

    def _build_image_menu(self, menu, att_id: int) -> None:
        """JournalEditor callback — append the thumbnail toggle action
        and the Tags submenu before its "Annotate image…" entry."""
        if self._current_id is None:
            return
        is_pinned = att_id in self._thumbnail_ids
        if is_pinned:
            act = menu.addAction("Remove from thumbnails")
            act.triggered.connect(
                lambda _checked=False, a=att_id: self._set_thumbnail_state(
                    a, included=False,
                )
            )
        else:
            act = menu.addAction("Add to thumbnails")
            act.triggered.connect(
                lambda _checked=False, a=att_id: self._set_thumbnail_state(
                    a, included=True,
                )
            )
        # Tag submenu — disabled when the image isn't yet pinned.
        current = self._tag_map.get(int(att_id), set())
        build_thumbnail_tag_submenu(
            menu,
            is_pinned=is_pinned,
            current_tag_ids=current,
            all_tags=self._available_tags,
            on_toggle=lambda tid, state, a=int(att_id):
                self._toggle_attachment_tag(a, tid, state),
            on_add_new=lambda name, a=int(att_id):
                self._add_tag_and_assign(name, a),
            on_manage=self._open_manage_tags_dialog,
        )

    def _on_thumb_remove_requested(self, att_id: int) -> None:
        self._set_thumbnail_state(int(att_id), included=False)

    def _set_thumbnail_state(self, att_id: int, *, included: bool) -> None:
        if self._current_id is None:
            return
        if included:
            if att_id in self._thumbnail_ids:
                return
            self._thumbnail_ids.add(int(att_id))
        else:
            if att_id not in self._thumbnail_ids:
                return
            self._thumbnail_ids.discard(int(att_id))
            # Unpinning drops the image from the strip — purge its tag
            # assignments so a future re-pin starts clean instead of
            # inheriting forgotten state.
            db_manager.clear_attachment_tags(
                self._get_conn(), _DOC_TYPE, self._current_id,
                int(att_id),
            )
            self._tag_map.pop(int(att_id), None)
            self.thumb_strip.update_attachment_tags(int(att_id), set())
        db_manager.set_strategy_thumbnail_ids(
            self._get_conn(), self._current_id, self._thumbnail_ids,
        )
        self.thumb_strip.set_thumbnail_filter(self._thumbnail_ids)
        self.editor.set_thumbnail_ids(self._thumbnail_ids)

    # ---- thumbnail tag mutations -----------------------------------------

    def _toggle_attachment_tag(
        self, att_id: int, tag_id: int, state: bool,
    ) -> None:
        """Add or remove one tag on one image, persist, and sync UI."""
        if self._current_id is None:
            return
        current = set(self._tag_map.get(int(att_id), set()))
        if state:
            current.add(int(tag_id))
        else:
            current.discard(int(tag_id))
        db_manager.set_attachment_tags(
            self._get_conn(), _DOC_TYPE, self._current_id,
            int(att_id), current,
        )
        if current:
            self._tag_map[int(att_id)] = current
        else:
            self._tag_map.pop(int(att_id), None)
        self.thumb_strip.update_attachment_tags(int(att_id), current)

    def _on_attachment_tag_toggled(
        self, att_id: int, tag_id: int, state: bool,
    ) -> None:
        self._toggle_attachment_tag(int(att_id), int(tag_id), bool(state))

    def _add_tag_and_assign(self, name: str, att_id: int) -> None:
        """Create a new preset and immediately assign it to ``att_id``."""
        if self._current_id is None:
            return
        try:
            tag_id = db_manager.add_thumbnail_tag(self._get_conn(), name)
        except ValueError:
            return
        self.refresh_thumbnail_tag_presets()
        self._toggle_attachment_tag(int(att_id), int(tag_id), True)
        self._broadcast_preset_change()

    def _on_add_tag_via_strip(self, name: str, att_id: int) -> None:
        self._add_tag_and_assign(name, int(att_id))

    def _open_manage_tags_dialog(self) -> None:
        """Show the rename/delete dialog, then reconcile local state
        once on close — single broadcast instead of one-per-mutation.
        Cascade-delete may have dropped link rows for the currently-
        open strategy, so we re-fetch the tag map on mutation."""
        dlg = ManageThumbnailTagsDialog(self, self._get_conn())
        dlg.exec()
        if not dlg.mutated:
            return
        if self._current_id is not None:
            self._tag_map = db_manager.fetch_doc_thumb_tag_map(
                self._get_conn(), _DOC_TYPE, self._current_id,
            )
            self.thumb_strip.set_doc_tag_map(self._tag_map)
        self.refresh_thumbnail_tag_presets()
        self._broadcast_preset_change()

    def _broadcast_preset_change(self) -> None:
        """Tell the other tab (Briefs) to re-pull its preset list."""
        win = self.window()
        sig = getattr(win, "thumbnail_tag_presets_changed", None)
        if sig is not None:
            sig.emit()

    def _on_thumb_size_changed(self, index: int) -> None:
        if self._settings is None:
            return
        self._settings.setValue(STRATEGIES_THUMB_SIZE_INDEX, int(index))

    # ---- collapse / expand -----------------------------------------------

    def _on_collapse_all(self) -> None:
        """Hide every block that follows a heading until the next
        same-or-higher-level heading. Heading blocks themselves stay
        visible so the document skeleton remains readable."""
        doc = self.editor.document()
        block = doc.firstBlock()
        while block.isValid():
            level = block.blockFormat().headingLevel()
            if level >= 1:
                # Walk forward, hiding everything up to the next
                # heading at or above this level.
                follower = block.next()
                while follower.isValid():
                    flevel = follower.blockFormat().headingLevel()
                    if flevel >= 1 and flevel <= level:
                        break
                    if follower.isVisible():
                        follower.setVisible(False)
                        self._folded_block_nos.add(follower.blockNumber())
                    follower = follower.next()
            block = block.next()
        # Force a re-layout so the hidden blocks visually disappear.
        doc.markContentsDirty(0, doc.characterCount())
        self.editor.viewport().update()

    def _on_expand_all(self) -> None:
        if not self._folded_block_nos:
            return
        doc = self.editor.document()
        for n in list(self._folded_block_nos):
            blk = doc.findBlockByNumber(n)
            if blk.isValid():
                blk.setVisible(True)
        self._folded_block_nos.clear()
        doc.markContentsDirty(0, doc.characterCount())
        self.editor.viewport().update()

    # ---- settings round-trip ---------------------------------------------

    def _restore_settings(self) -> None:
        if self._settings is None:
            return
        sp = self._settings.value(STRATEGIES_SPLITTER)
        if sp is not None:
            try:
                self.splitter.restoreState(sp)
            except (TypeError, ValueError):
                pass
        # Default to outline + thumbs ON for new users — the whole
        # point of this tab is the navigation, so make it visible.
        show_outline = self._settings.value(STRATEGIES_SHOW_OUTLINE, True)
        show_thumbs = self._settings.value(STRATEGIES_SHOW_THUMBS, True)
        for raw, btn, target in (
            (show_outline, self.btn_toggle_outline, self.outline),
            (show_thumbs, self.btn_toggle_thumbs, self.thumb_strip),
        ):
            checked = self._coerce_bool(raw, default=True)
            btn.setChecked(checked)
            target.setVisible(checked)

        # Thumbnail size — clamp to a valid index in case the user
        # downgraded after a future build added more presets.
        raw_idx = self._settings.value(
            STRATEGIES_THUMB_SIZE_INDEX, DEFAULT_THUMB_SIZE_INDEX,
        )
        try:
            idx = int(raw_idx)
        except (TypeError, ValueError):
            idx = DEFAULT_THUMB_SIZE_INDEX
        idx = max(0, min(idx, len(THUMB_SIZES) - 1))
        self.thumb_strip.set_size_index(idx)

    @staticmethod
    def _coerce_bool(v, *, default: bool) -> bool:
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.strip().lower() in ("true", "1", "yes", "on")
        if isinstance(v, (int, float)):
            return bool(v)
        return default

    def save_settings(self) -> None:
        if self._settings is None:
            return
        self._settings.setValue(
            STRATEGIES_SPLITTER, self.splitter.saveState(),
        )
