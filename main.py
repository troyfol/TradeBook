"""TradeBook entry point — creates the app, loads the DB, shows the main window.

Frozen-build note: paths come from `config`, which auto-detects whether
we're running from source or as a PyInstaller --onefile exe. The data
folder lives next to the exe (`config.PROJECT_ROOT`); the QSS lives
inside the bundle (`config.RESOURCE_ROOT`).
"""
from __future__ import annotations

import sqlite3
import sys

import pyqtgraph as pg
from PySide6.QtCore import QLockFile, QSettings, Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from config import (
    APP_VERSION, AUTO_BACKUPS_DIR, DATA_DIR, DB_PATH, RESOURCE_ROOT,
    ensure_user_dirs, migrate_loose_backups_to_manual, purge_temp_dir,
)
from ingest import backups, db_manager
from gui.main_window import MainWindow
from gui.styles.theme import apply_theme
from gui.widgets.chart_palette import get_palette, init_palette

QSS_PATH = RESOURCE_ROOT / "gui" / "styles" / "dark_theme.qss"

# Global pyqtgraph antialias is safe to set early; bg/fg are set per-plot
# from the palette after init_palette() runs.
pg.setConfigOption("antialias", True)
# OpenGL on Windows is fragile across DPI changes (widgets get
# recreated and pyqtgraph's GL context can crash). Force the raster
# backend — performance penalty is negligible at our chart sizes.
pg.setConfigOption("useOpenGL", False)

# High-DPI rounding policy: PassThrough keeps fractional scale factors
# (e.g. 1.5x) instead of rounding to 2x, so dragging between a 1080p
# and a 4K monitor doesn't trigger a layout-resize storm that has
# crashed widget reparenting in the past.
QApplication.setHighDpiScaleFactorRoundingPolicy(
    Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
)


def main() -> int:
    # Make sure the user-data folders exist before anything tries to
    # write to them. On a fresh --onefile install this creates `data/`
    # and `backups/` next to the exe on first launch.
    ensure_user_dirs()
    # One-time move of any pre-split loose `backups/*.db` files into
    # `backups/manual/` so the strict 5-backup auto cap doesn't delete
    # the user's existing snapshots on first launch after the split.
    try:
        n_migrated = migrate_loose_backups_to_manual()
        if n_migrated:
            print(
                f"[TradeBook] migrated {n_migrated} pre-split backup(s) "
                "into backups/manual/"
            )
    except OSError as e:
        print(f"[TradeBook] backup migration skipped: {e}")
    # Prior sessions may have left scratch files (attachment dumps for
    # QDesktopServices) lying around — wipe them before the new session
    # so they don't accumulate over time.
    purge_temp_dir()

    app = QApplication(sys.argv)
    app.setOrganizationName("TradeBook")
    app.setApplicationName("TradeBook")
    app.setApplicationVersion(APP_VERSION)

    # Bind chart palette to settings BEFORE stylesheet load so derived
    # tones use the user's customized colors on first paint.
    init_palette(QSettings())
    pal = get_palette()
    pg.setConfigOption("background", pal.background)
    pg.setConfigOption("foreground", pal.label)
    apply_theme(app, QSS_PATH)

    # Prevent two instances from writing to the same DB. QLockFile
    # handles stale locks (left behind by crashes) automatically: if the
    # recorded PID is no longer alive, ``tryLock`` acquires anyway.
    lock_path = str(DATA_DIR / "tradebook.lock")
    lock_file = QLockFile(lock_path)
    lock_file.setStaleLockTime(30_000)  # 30 s — plenty for a cold start
    if not lock_file.tryLock(100):
        QMessageBox.warning(
            None,
            "TradeBook is already running",
            "Another copy of TradeBook is using this data folder.\n\n"
            "Close the other window before launching again, or delete "
            f"{lock_path} if you're certain no other instance is alive.",
        )
        return 1

    conn = db_manager.connect(DB_PATH)
    db_manager.init_schema(conn)
    # Best-effort recycle-bin trim — drop soft-deleted rows older than
    # the 30-day retention window.
    try:
        db_manager.purge_old_deleted_trades(conn)
    except sqlite3.Error as e:
        print(f"[TradeBook] recycle-bin purge skipped: {e}")

    # Best-effort startup backup. Failures here should never block launch.
    try:
        backups.maybe_create_startup_backup(conn, AUTO_BACKUPS_DIR)
    except OSError as e:
        print(f"[TradeBook] startup backup skipped: {e}")

    window = MainWindow(conn)
    window.show_with_saved_geometry()

    try:
        rc = app.exec()
    finally:
        # Release the single-instance lock so a restart after this
        # process exits can acquire it cleanly.
        lock_file.unlock()
        # Best-effort scratch-dir cleanup on clean shutdown. The next
        # launch also purges, so any temp files pinned by external
        # viewers will be picked up later.
        try:
            purge_temp_dir()
        except OSError:
            pass
    return rc


if __name__ == "__main__":
    sys.exit(main())
