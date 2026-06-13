"""Main application window: QTabWidget with seven tabs + geometry persistence.

Geometry is persisted via QSettings on closeEvent and restored on first
show. If no saved geometry exists, the window opens maximized on the
primary monitor. Zoom levels for the Trades and New Trade preview tables
are persisted alongside geometry.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QByteArray, QSettings, Signal
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence
from PySide6.QtWidgets import (
    QDialog, QMainWindow, QMessageBox, QStatusBar, QTabWidget,
)

from config import AUTO_BACKUPS_DIR, BACKUPS_DIR, DB_PATH, MANUAL_BACKUPS_DIR
from gui.dialogs.r_multiple_settings import RMultipleSettingsDialog
from gui.dialogs.recycle_bin import RecycleBinDialog
from gui.dialogs.restore_backup import RestoreBackupDialog
from ingest import backups as backups_mod

from gui.settings_keys import (
    MAIN_WINDOW_GEOMETRY, MAIN_WINDOW_STATE,
    ZOOM_NEW_TRADE_PREVIEW, ZOOM_TRADES,
)
from ingest import db_manager
from gui.tabs.dashboard import DashboardTab
from gui.widgets.chart_palette import init_palette
from gui.tabs.calendar_tab import CalendarTab
from gui.tabs.reports import ReportsTab
from gui.tabs.trades import TradesTab
from gui.tabs.journal import JournalTab
from gui.tabs.briefs import BriefsTab
from gui.tabs.strategies import StrategiesTab
from gui.tabs.new_trade import NewTradeTab

STATUS_MSG_DURATION_MS = 6000


class MainWindow(QMainWindow):
    # Cross-tab broadcast for thumbnail tag preset CRUD. Briefs and
    # Strategies both maintain their own chip rail, so when either tab
    # adds / renames / deletes a preset the other one needs to refresh
    # its rail too. Tabs emit this signal after a successful DB write.
    thumbnail_tag_presets_changed = Signal()

    def __init__(
        self,
        conn: sqlite3.Connection,
        settings: Optional[QSettings] = None,
        parent=None,
    ):
        super().__init__(parent)
        self.conn = conn
        self._settings = settings or QSettings("TradeBook", "TradeBook")
        # Wire the global chart palette to our settings store so the
        # right-click "Customize chart colors" dialog persists across
        # launches.
        init_palette(self._settings)

        self.setWindowTitle("TradeBook")
        self.setStatusBar(QStatusBar(self))
        self._build_menu_bar()

        self.tabs = QTabWidget(self)
        self.dashboard_tab = DashboardTab(
            lambda: self.conn, self._settings, self,
        )
        self.calendar_tab = CalendarTab(
            lambda: self.conn, self._settings, self,
        )
        self.reports_tab = ReportsTab(
            lambda: self.conn, self._settings, self,
        )
        self.trades_tab = TradesTab(lambda: self.conn, self)
        self.journal_tab = JournalTab(
            lambda: self.conn, self._settings, self,
        )
        self.briefs_tab = BriefsTab(
            lambda: self.conn, self._settings, self,
        )
        self.strategies_tab = StrategiesTab(
            lambda: self.conn, self._settings, self,
        )
        self.new_trade_tab = NewTradeTab(lambda: self.conn, self)

        self.tabs.addTab(self.dashboard_tab, "Dashboard")
        self.tabs.addTab(self.calendar_tab, "Calendar")
        self.tabs.addTab(self.reports_tab, "Reports")
        self.tabs.addTab(self.trades_tab, "Trades")
        self.tabs.addTab(self.journal_tab, "Journal")
        self.tabs.addTab(self.briefs_tab, "Briefs")
        self.tabs.addTab(self.strategies_tab, "Strategies")
        self.tabs.addTab(self.new_trade_tab, "New Trade")

        # Land on Dashboard — that's where the user-facing summary
        # lives now that the chart cards have moved in.
        self.tabs.setCurrentWidget(self.dashboard_tab)

        self.setCentralWidget(self.tabs)

        # Post-import signal handoff.
        self.new_trade_tab.import_completed.connect(self._on_import_completed)

        # Calendar drilldown → Trades tab with date filter applied.
        self.calendar_tab.day_drilldown.connect(self._on_calendar_drilldown)

        # First-run "Go to New Trade" button on the empty Trades view.
        self.trades_tab.goto_new_trade_requested.connect(
            lambda: self.tabs.setCurrentWidget(self.new_trade_tab)
        )

        # Brief generated on Journal tab → focus it on the Briefs tab.
        self.journal_tab.brief_saved.connect(self._on_brief_saved)

        # Flush any pending journal save when leaving the Journal tab.
        self.tabs.currentChanged.connect(self._on_main_tab_changed)

        # Broadcast thumbnail-tag preset changes across Briefs and
        # Strategies — both maintain independent chip rails.
        self.thumbnail_tag_presets_changed.connect(
            self.briefs_tab.refresh_thumbnail_tag_presets
        )
        self.thumbnail_tag_presets_changed.connect(
            self.strategies_tab.refresh_thumbnail_tag_presets
        )

        # Restore saved zoom levels for both table views.
        self._restore_zoom_levels()

        self._update_status()

    # ---- public ------------------------------------------------------------

    def show_with_saved_geometry(self) -> None:
        """Show the window, restoring prior geometry or defaulting to maximized."""
        geom = self._settings.value(MAIN_WINDOW_GEOMETRY)
        state = self._settings.value(MAIN_WINDOW_STATE)

        if isinstance(geom, QByteArray) and not geom.isEmpty():
            self.restoreGeometry(geom)
            if isinstance(state, QByteArray) and not state.isEmpty():
                self.restoreState(state)
            self.show()
        else:
            self.showMaximized()

    def refresh_all(self) -> None:
        """Refresh any tabs that read from the DB. Called post-import etc.

        Tabs fetch the trade list independently during normal usage, but
        `refresh_all` is the one-shot path (e.g. after a paste import)
        where every tab needs the same snapshot. Fetch each list once
        here and pass it through so we don't hit the DB 4-5 times for
        identical data.
        """
        all_trades = db_manager.fetch_trades_for_display(self.conn)
        closed_trades = db_manager.fetch_closed_trades(self.conn)
        self.trades_tab.refresh(preloaded_trades=all_trades)
        self.dashboard_tab.refresh(preloaded_closed=closed_trades)
        self.calendar_tab.refresh(preloaded_closed=closed_trades)
        self.reports_tab.refresh(preloaded_closed=closed_trades)
        self.journal_tab.refresh(preloaded_trades=all_trades)
        self.briefs_tab.refresh()
        self.strategies_tab.refresh()
        self._update_status()

    # ---- internals ---------------------------------------------------------

    def _on_calendar_drilldown(self, d) -> None:
        self.trades_tab.set_date_filter(d)
        self.tabs.setCurrentWidget(self.trades_tab)

    def _on_main_tab_changed(self, _idx: int) -> None:
        # Whenever the user navigates *away* from a tab that holds
        # autosaved state, flush it. Journal + Briefs + Strategies all
        # buffer in-memory rich-text edits.
        self.journal_tab.flush_pending_save()
        self.briefs_tab.flush_pending_save()
        self.strategies_tab.flush_pending_save()

    def _on_brief_saved(self, brief_id: int) -> None:
        """A new brief was generated on the Journal tab — switch to the
        Briefs tab and focus the new row."""
        self.briefs_tab.select_brief(int(brief_id))
        self.tabs.setCurrentWidget(self.briefs_tab)

    def _on_import_completed(self, n_new: int) -> None:
        self.refresh_all()
        self.tabs.setCurrentWidget(self.trades_tab)
        if n_new == 0:
            msg = "No new executions imported."
        elif n_new == 1:
            msg = "Imported 1 new execution."
        else:
            msg = f"Imported {n_new} new executions."
        self.statusBar().showMessage(msg, STATUS_MSG_DURATION_MS)

    def _update_status(self) -> None:
        try:
            (n_trades,) = self.conn.execute(
                "SELECT COUNT(*) FROM trades"
            ).fetchone()
            (n_execs,) = self.conn.execute(
                "SELECT COUNT(*) FROM executions"
            ).fetchone()
            # Preserve any transient message already shown (e.g. post-import).
            if not self.statusBar().currentMessage():
                self.statusBar().showMessage(
                    f"{n_trades} trades · {n_execs} executions"
                )
        except sqlite3.Error as e:
            self.statusBar().showMessage(f"DB error: {e}")

    def _restore_zoom_levels(self) -> None:
        z_trades = self._settings.value(ZOOM_TRADES, 0)
        z_preview = self._settings.value(ZOOM_NEW_TRADE_PREVIEW, 0)
        try:
            self.trades_tab.table.set_zoom_level(int(z_trades))
        except (TypeError, ValueError):
            pass
        try:
            self.new_trade_tab.preview_table.set_zoom_level(int(z_preview))
        except (TypeError, ValueError):
            pass

    # ---- menu bar ----------------------------------------------------------

    def _build_menu_bar(self) -> None:
        bar = self.menuBar()
        file_menu = bar.addMenu("&File")

        act_backup_now = QAction("Back up now", self)
        act_backup_now.setShortcut("Ctrl+B")
        act_backup_now.triggered.connect(self._on_backup_now)
        file_menu.addAction(act_backup_now)

        act_restore = QAction("Restore from backup…", self)
        act_restore.triggered.connect(self._on_restore_backup)
        file_menu.addAction(act_restore)

        act_recycle = QAction("Recycle bin…", self)
        act_recycle.triggered.connect(self._on_open_recycle_bin)
        file_menu.addAction(act_recycle)

        file_menu.addSeparator()

        act_r = QAction("R-multiple settings…", self)
        act_r.triggered.connect(self._on_edit_r_settings)
        file_menu.addAction(act_r)

        file_menu.addSeparator()

        act_quit = QAction("Exit", self)
        act_quit.setShortcut(QKeySequence.StandardKey.Quit)
        act_quit.triggered.connect(self.close)
        file_menu.addAction(act_quit)

    def _on_backup_now(self) -> None:
        """Create a user-requested snapshot in the manual folder."""
        # Flush pending editor text so the snapshot includes the user's
        # most recent edits.
        try:
            self.journal_tab.flush_pending_save()
            self.briefs_tab.flush_pending_save()
            self.strategies_tab.flush_pending_save()
        except Exception:
            pass
        try:
            path = backups_mod.create_manual_backup(
                self.conn, MANUAL_BACKUPS_DIR,
            )
        except Exception as e:
            QMessageBox.critical(
                self, "Backup failed",
                f"Could not write a new snapshot:\n\n"
                f"{type(e).__name__}: {e}",
            )
            return
        self.statusBar().showMessage(
            f"Backup saved: {path.name}  (manual — never pruned)",
            6000,
        )

    def _on_restore_backup(self) -> None:
        dlg = RestoreBackupDialog(
            AUTO_BACKUPS_DIR, MANUAL_BACKUPS_DIR, self,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        chosen = dlg.chosen_backup()
        if chosen is None:
            return

        # Pre-flight: confirm the chosen file is readable before we
        # mutate anything. This catches "user picked a backup on a
        # disconnected drive" / "snapshot got corrupted" before we
        # spend a safety snapshot on it.
        try:
            with open(chosen, "rb") as fh:
                fh.read(16)   # SQLite header is 16 bytes
        except OSError as e:
            QMessageBox.critical(
                self, "Restore failed",
                f"Could not read {chosen.name}:\n\n{e}\n\n"
                "Your current database has not been touched.",
            )
            return

        # Flush any pending edits before we close the connection — once
        # the file is swapped under the editor those buffers are lost.
        try:
            self.journal_tab.flush_pending_save()
            self.briefs_tab.flush_pending_save()
            self.strategies_tab.flush_pending_save()
        except Exception:
            pass

        # Safety snapshot of the CURRENT DB lives in the auto folder —
        # it's an automatic side-effect of restore, not a user-curated
        # checkpoint. Capture the path so we can surface it in any
        # error dialog (the user needs to know one exists, otherwise
        # a retry could overwrite it).
        safety_path: Optional[Path] = None
        try:
            safety_path = backups_mod.create_backup(
                self.conn, AUTO_BACKUPS_DIR,
            )
        except Exception as e:
            print(f"[TradeBook] safety snapshot before restore failed: {e}")

        try:
            self.conn.close()
        except sqlite3.Error:
            pass
        try:
            backups_mod.restore_backup(
                Path(chosen), Path(DB_PATH),
                safety_conn=None, backups_dir=None,
            )
        except (OSError, sqlite3.Error) as e:
            safety_note = (
                f"\n\nYour pre-restore state was snapshotted to\n"
                f"{safety_path}\nbefore the restore was attempted."
                if safety_path is not None
                else "\n\nNo safety snapshot was created."
            )
            QMessageBox.critical(
                self, "Restore failed",
                f"Could not restore {chosen.name}:\n\n"
                f"{type(e).__name__}: {e}"
                f"{safety_note}",
            )
            # Best-effort: re-open the (unmodified) DB so the app stays usable.
            from ingest import db_manager
            self.conn = db_manager.connect(DB_PATH)
            db_manager.init_schema(self.conn)
            self.refresh_all()
            return

        # Re-open the freshly-restored DB and refresh every tab.
        from ingest import db_manager
        self.conn = db_manager.connect(DB_PATH)
        db_manager.init_schema(self.conn)
        self.refresh_all()
        msg = f"Restored from {chosen.name}."
        if safety_path is not None:
            msg += (
                f"\n\nA safety snapshot of your prior state was saved to\n"
                f"{safety_path.name}\nin the auto-backup folder."
            )
        QMessageBox.information(self, "Restore complete", msg)

    def _on_open_recycle_bin(self) -> None:
        dlg = RecycleBinDialog(lambda: self.conn, self)
        dlg.exec()
        if dlg.restored_trade_ids():
            # Restoring brings rows back to `trades`; every tab needs
            # to re-pull.
            self.refresh_all()

    def _on_edit_r_settings(self) -> None:
        dlg = RMultipleSettingsDialog(self._settings, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            # Re-render the Reports tab so the By R-Multiple sub-tab
            # picks up the new default risk immediately.
            self.reports_tab.refresh()

    # ---- Qt events ---------------------------------------------------------

    def closeEvent(self, event: QCloseEvent) -> None:
        # Flush any pending autosave before window state hits disk.
        self.journal_tab.flush_pending_save()
        self.journal_tab.save_settings()
        self.briefs_tab.flush_pending_save()
        self.briefs_tab.save_settings()
        self.strategies_tab.flush_pending_save()
        self.strategies_tab.save_settings()
        self._settings.setValue(MAIN_WINDOW_GEOMETRY, self.saveGeometry())
        self._settings.setValue(MAIN_WINDOW_STATE, self.saveState())
        self._settings.setValue(
            ZOOM_TRADES, self.trades_tab.table.zoom_level()
        )
        self._settings.setValue(
            ZOOM_NEW_TRADE_PREVIEW, self.new_trade_tab.preview_table.zoom_level()
        )
        self._settings.sync()
        super().closeEvent(event)
