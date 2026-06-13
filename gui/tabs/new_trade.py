"""New Trade tab — bulk paste flow.

Workflow:
    1. User pastes TradeStation order export text into the paste area.
    2. Clicks Parse → parser extracts filled executions, computes import
       hashes, classifies each as NEW or DUPLICATE (against the current DB),
       and buckets parse errors as ERROR.
    3. Preview table renders with row background colors (green/gray/red).
       Tooltip on a DUP row shows the matching existing execution; tooltip
       on an ERROR row shows the parse error + raw line.
    4. User clicks Import → new executions inserted, trades / daily summary
       rebuilt, `import_completed` signal emitted (MainWindow switches to
       Trades tab and refreshes).

Non-filled orders (Rejected, Canceled, etc.) are silently filtered at parse
time and do not appear in the preview, matching Phase 1 parser behavior.
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QPoint, QSettings, Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QMenu, QMessageBox,
    QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
)

from config import AUTO_BACKUPS_DIR, BACKUPS_DIR
from ingest import backups, db_manager
from ingest.tradestation_parser import parse_paste
from models.preview_model import (
    PreviewRow, PreviewTableModel, STATE_DUPLICATE, STATE_ERROR, STATE_NEW,
)
from gui.widgets.zoom_controls import ZoomableTableView, ZoomControls


# Safety cap — a 10 MB paste is >100k order rows, far beyond any
# plausible single copy-paste from TradeStation.
MAX_PASTE_BYTES = 10 * 1024 * 1024


class NewTradeTab(QWidget):
    """Paste → parse → preview → confirm → import."""

    # Emitted after a successful import with the count of new rows inserted.
    import_completed = Signal(int)

    def __init__(self, get_conn: Callable, parent=None):
        super().__init__(parent)
        self._get_conn = get_conn

        # --- widgets --------------------------------------------------------
        self.paste_label = QLabel("Paste TradeStation order export:", self)

        self.paste_area = QPlainTextEdit(self)
        self.paste_area.setPlaceholderText(
            "Paste tab-delimited TradeStation order rows here "
            "(with or without the header row)..."
        )
        self.paste_area.setMinimumHeight(140)

        self.btn_parse = QPushButton("Parse", self)
        self.btn_clear = QPushButton("Clear", self)
        self.btn_import = QPushButton("Import", self)
        self.btn_import.setEnabled(False)

        self.lbl_summary = QLabel("", self)
        self.lbl_summary.setObjectName("hintLabel")

        self.preview_model = PreviewTableModel()
        self.preview_table = ZoomableTableView(self)
        self.preview_table.setModel(self.preview_model)
        self.preview_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.preview_table.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.preview_table.setAlternatingRowColors(False)
        # Double-click (or F2) to edit any editable cell on a NEW row —
        # the model's ``flags`` gates which cells/rows allow editing.
        self.preview_table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        self.preview_table.verticalHeader().setVisible(False)
        self.preview_table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.preview_table.customContextMenuRequested.connect(
            self._on_preview_context_menu
        )

        # Delete key removes the current preview selection.
        self._delete_shortcut = QShortcut(
            QKeySequence(Qt.Key.Key_Delete), self.preview_table
        )
        self._delete_shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
        self._delete_shortcut.activated.connect(
            self._remove_selected_preview_rows
        )

        preview_header = self.preview_table.horizontalHeader()
        preview_header.setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        preview_header.setStretchLastSection(True)

        self.zoom_controls = ZoomControls(self.preview_table, self)

        # --- layout ---------------------------------------------------------
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        layout.addWidget(self.paste_label)
        layout.addWidget(self.paste_area)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)
        btn_row.addWidget(self.btn_parse)
        btn_row.addWidget(self.btn_clear)
        btn_row.addStretch()
        btn_row.addWidget(self.btn_import)
        layout.addLayout(btn_row)

        layout.addWidget(self.lbl_summary)

        preview_row = QHBoxLayout()
        preview_row.addWidget(QLabel("Preview:", self))
        preview_row.addStretch()
        preview_row.addWidget(self.zoom_controls)
        layout.addLayout(preview_row)

        layout.addWidget(self.preview_table, 1)

        # --- connections ----------------------------------------------------
        self.btn_parse.clicked.connect(self._on_parse)
        self.btn_clear.clicked.connect(self._on_clear)
        self.btn_import.clicked.connect(self._on_import)

    # ---- slots -------------------------------------------------------------

    def _on_parse(self) -> None:
        text = self.paste_area.toPlainText()
        if not text.strip():
            self.lbl_summary.setText("Paste area is empty.")
            self.preview_model.set_rows([])
            self.btn_import.setEnabled(False)
            self.btn_import.setText("Import")
            return

        if len(text.encode("utf-8", errors="replace")) > MAX_PASTE_BYTES:
            self.lbl_summary.setText(
                f"Paste exceeds {MAX_PASTE_BYTES // (1024 * 1024)} MB limit "
                "— split into smaller batches."
            )
            self.lbl_summary.setObjectName("errorLabel")
            self.lbl_summary.style().polish(self.lbl_summary)
            self.preview_model.set_rows([])
            self.btn_import.setEnabled(False)
            self.btn_import.setText("Import")
            return

        # Restore normal styling after a prior oversize error.
        if self.lbl_summary.objectName() != "hintLabel":
            self.lbl_summary.setObjectName("hintLabel")
            self.lbl_summary.style().polish(self.lbl_summary)

        execs, parse_errors = parse_paste(text)

        # Dedup check: fast path is the hash index; fallback is a
        # fuzzy lookup (±2 s on entered_at, same symbol/type/price/qty)
        # so a re-export that jittered one second on the timestamp
        # column — TradeStation sometimes does this between two
        # copy-pastes of the same order — still marks as duplicate.
        existing_map: dict[str, dict] = {}
        if execs:
            conn = self._get_conn()
            hashes = [e["import_hash"] for e in execs]
            placeholders = ",".join("?" * len(hashes))
            cur = conn.execute(
                f"""
                SELECT import_hash, entered_at, symbol, type,
                       filled_price, qty_filled
                FROM executions
                WHERE import_hash IN ({placeholders})
                """,
                hashes,
            )
            for r in cur.fetchall():
                existing_map[r["import_hash"]] = dict(r)

            # Fuzzy fallback for any exec whose hash didn't hit.
            for ex in execs:
                if ex["import_hash"] in existing_map:
                    continue
                if ex["entered_at"] is None or ex["filled_price"] is None:
                    continue
                cur = conn.execute(
                    """
                    SELECT import_hash, entered_at, symbol, type,
                           filled_price, qty_filled
                    FROM executions
                    WHERE symbol = ?
                      AND type = ?
                      AND filled_price = ?
                      AND qty_filled = ?
                      AND abs((julianday(entered_at) - julianday(?))
                              * 86400.0) <= 2.0
                    LIMIT 1
                    """,
                    (
                        ex["symbol"], ex["type"],
                        ex["filled_price"], ex["qty_filled"],
                        ex["entered_at"].isoformat(sep=" "),
                    ),
                )
                row = cur.fetchone()
                if row:
                    existing_map[ex["import_hash"]] = dict(row)

        rows: list[PreviewRow] = []
        # Errors first so they're visually obvious at the top.
        for pe in parse_errors:
            rows.append(PreviewRow(
                state=STATE_ERROR,
                error_message=pe.error,
                raw_line=pe.raw,
            ))
        for ex in execs:
            h = ex["import_hash"]
            if h in existing_map:
                rows.append(PreviewRow(
                    state=STATE_DUPLICATE,
                    execution=ex,
                    existing_match=existing_map[h],
                ))
            else:
                rows.append(PreviewRow(state=STATE_NEW, execution=ex))

        self.preview_model.set_rows(rows)

        n_new = sum(1 for r in rows if r.state == STATE_NEW)
        n_dup = sum(1 for r in rows if r.state == STATE_DUPLICATE)
        n_err = sum(1 for r in rows if r.state == STATE_ERROR)
        self.lbl_summary.setText(
            f"{n_new} new  ·  {n_dup} duplicates  ·  {n_err} errors"
        )
        self.btn_import.setEnabled(n_new > 0)
        self.btn_import.setText(f"Import {n_new}" if n_new else "Import")

    def _on_clear(self) -> None:
        self.paste_area.clear()
        self.preview_model.set_rows([])
        self.lbl_summary.setText("")
        self.btn_import.setEnabled(False)
        self.btn_import.setText("Import")

    def _on_import(self) -> None:
        new_execs = [
            r.execution for r in self.preview_model.rows()
            if r.state == STATE_NEW and r.execution is not None
        ]
        if not new_execs:
            return

        # Default $R from settings (0 = disabled). Used by the
        # import-time risk resolver for winners with no manual stop;
        # losers always get risk = |net_pnl| by the resolver's rules.
        default_risk: Optional[float] = None
        try:
            from gui.dialogs.r_multiple_settings import load_default_risk
            settings = QSettings("TradeBook", "TradeBook")
            v = load_default_risk(settings)
            default_risk = v if v and v > 0 else None
        except Exception:
            default_risk = None

        conn = self._get_conn()

        # Pre-import safety snapshot. Failure here is non-fatal — we warn
        # but still let the user proceed (they can always undo via the
        # most recent prior backup).
        try:
            backups.create_pre_import_backup(conn, AUTO_BACKUPS_DIR)
        except Exception as e:
            QMessageBox.warning(
                self,
                "Backup failed",
                "Could not create a pre-import backup:\n\n"
                f"{e}\n\nThe import will continue, but no rollback "
                "snapshot was saved.",
            )

        builder_errors: list[str] = []
        try:
            inserted, _skipped = db_manager.insert_executions(
                conn, new_execs, auto_commit=False,
            )
            _n_trades, builder_errors = db_manager.rebuild_trades(conn)
            # Resolve planned risk into stop-loss prices BEFORE the
            # daily-summary rebuild — keeps the whole import atomic.
            # Losers auto-fill to risk = |net_pnl| (clean -1R);
            # winners fall back to ``default_risk`` if one's set.
            db_manager.apply_import_time_risk(
                conn, {}, default_risk=default_risk,
            )
            db_manager.rebuild_daily_summary(conn)
            conn.commit()
        except Exception as e:
            conn.rollback()
            QMessageBox.critical(
                self,
                "Import failed",
                "An error occurred while importing executions:\n\n"
                f"{type(e).__name__}: {e}\n\n"
                "All changes have been rolled back. Restore the most "
                f"recent backup from {BACKUPS_DIR} if you suspect corruption.",
            )
            return

        # Builder errors no longer abort the import — prior trades are
        # preserved around any offending fill — but the user still
        # needs to know something wasn't grouped cleanly.
        if builder_errors:
            preview = "\n".join(f"  • {e}" for e in builder_errors[:5])
            more = (
                f"\n  … and {len(builder_errors) - 5} more"
                if len(builder_errors) > 5 else ""
            )
            QMessageBox.warning(
                self,
                "Trade builder warnings",
                "The import completed, but the trade builder couldn't "
                "group some fills into a trade (prior trades for those "
                "symbols were kept intact):\n\n"
                f"{preview}{more}\n\n"
                "These fills are still in the executions table — you "
                "can edit them in place or delete them, then re-run an "
                "import to rebuild.",
            )

        self._on_clear()
        self.import_completed.emit(inserted)

    # ---- preview multi-select removal -------------------------------------

    def _on_preview_context_menu(self, pos: QPoint) -> None:
        idx = self.preview_table.indexAt(pos)
        if not idx.isValid():
            return

        sel_indices = self._selected_preview_rows()
        # If the right-clicked row isn't in the selection, treat it as a
        # single-row action and reset the selection so it's visible.
        if idx.row() not in sel_indices:
            self.preview_table.clearSelection()
            self.preview_table.selectRow(idx.row())
            sel_indices = [idx.row()]

        n = len(sel_indices)
        menu = QMenu(self)
        remove_action = menu.addAction(
            f"Remove from preview ({n})" if n > 1 else "Remove from preview"
        )
        chosen = menu.exec(
            self.preview_table.viewport().mapToGlobal(pos)
        )
        if chosen == remove_action:
            self._remove_selected_preview_rows()

    def _selected_preview_rows(self) -> list[int]:
        return sorted({
            ix.row()
            for ix in self.preview_table.selectionModel().selectedRows()
        })

    def _remove_selected_preview_rows(self) -> None:
        indices = self._selected_preview_rows()
        if not indices:
            return
        self.preview_model.remove_indices(indices)
        self._refresh_preview_summary()

    def _refresh_preview_summary(self) -> None:
        rows = self.preview_model.rows()
        n_new = sum(1 for r in rows if r.state == STATE_NEW)
        n_dup = sum(1 for r in rows if r.state == STATE_DUPLICATE)
        n_err = sum(1 for r in rows if r.state == STATE_ERROR)
        if not rows:
            self.lbl_summary.setText("")
        else:
            self.lbl_summary.setText(
                f"{n_new} new  ·  {n_dup} duplicates  ·  {n_err} errors"
            )
        self.btn_import.setEnabled(n_new > 0)
        self.btn_import.setText(f"Import {n_new}" if n_new else "Import")
