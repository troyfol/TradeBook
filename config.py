"""Paths and constants for the TradeBook application.

Path layout:
    PROJECT_ROOT  — writable; holds `data/` (DB) and `backups/`. When
                    running from source this is the repo root. When
                    running as a PyInstaller --onefile exe this is the
                    directory containing the .exe, so the user can find
                    and back up their data folder next to the binary.

    RESOURCE_ROOT — read-only; holds bundled resources like the QSS
                    stylesheet. From source this is the repo root; in a
                    --onefile build it's the temporary extraction
                    directory (`sys._MEIPASS`).

`ensure_user_dirs()` should be called from `main()` BEFORE any code
opens the DB so the data + backups directories are created on first run.
"""
from __future__ import annotations

import sys
from pathlib import Path


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _project_root() -> Path:
    """Writable user-data root.

    Frozen --onefile: directory containing the .exe.
    Source: directory containing this file (the repo root).
    """
    if _is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _resource_root() -> Path:
    """Read-only resource root for files bundled into the exe.

    Frozen --onefile: PyInstaller's temp extraction dir (`sys._MEIPASS`).
    Source: same as the project root.
    """
    if _is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


# Application version. Shown in the window title and stamped into the
# exe's file properties via version_info.txt.
#
# 2.0.0 — audit release: exits timestamped by fill time rather than
#         order-entry time, partial fills on non-Filled rows reach the
#         trade builder, over-sells salvage their P&L, imports are
#         genuinely atomic, unused daily_summary dropped.
# 2.1.0 — position flips are resolved up front, once per block of
#         shares, and the answer sticks: stop the import, delete the
#         extra shares, leave it open (flipped), or close it to P&L.
#         Resolved fills stay visibly contested in Manage Executions,
#         which is now scoped to the trades you have selected.
APP_VERSION = "2.1.0"

PROJECT_ROOT = _project_root()
RESOURCE_ROOT = _resource_root()

DATA_DIR = PROJECT_ROOT / "data"
BACKUPS_DIR = PROJECT_ROOT / "backups"
# Snapshots taken by the app on startup / before destructive ops live
# here and are capped to the most recent N (see `backups.MAX_AUTO_BACKUPS`).
AUTO_BACKUPS_DIR = BACKUPS_DIR / "auto"
# Snapshots taken by the user via "Back up now" — never pruned.
MANUAL_BACKUPS_DIR = BACKUPS_DIR / "manual"
DB_PATH = DATA_DIR / "tradebook.db"
# Short-lived scratch files live here (e.g. attachments dumped to disk
# so QDesktopServices can hand them to the OS's default viewer). Cleared
# every launch — see ``purge_temp_dir``.
TEMP_DIR = DATA_DIR / ".cache"


def ensure_user_dirs() -> None:
    """Create the writable user-data directories if they don't exist.

    Called once at startup. Safe to call repeatedly.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    AUTO_BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    MANUAL_BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    TEMP_DIR.mkdir(parents=True, exist_ok=True)


def migrate_loose_backups_to_manual() -> int:
    """One-time migration: move any loose ``backups/*.db`` snapshots
    (created before the auto/manual split) into ``backups/manual/`` so
    they're preserved under the unlimited-retention rules.

    Returns the number of files migrated. Safe to call on every launch
    — it's a no-op once the top-level directory is clean.
    """
    if not BACKUPS_DIR.exists():
        return 0
    MANUAL_BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    moved = 0
    for p in BACKUPS_DIR.iterdir():
        # Only move files we're sure are snapshots; leave other
        # directories and unrelated files alone.
        if (
            p.is_file()
            and p.suffix == ".db"
            and p.name.startswith("tradebook_")
        ):
            try:
                p.replace(MANUAL_BACKUPS_DIR / p.name)
                moved += 1
            except OSError:
                continue
    return moved


def purge_temp_dir() -> None:
    """Best-effort wipe of the scratch directory contents.

    Called on startup (so prior-session temp files don't linger) and on
    clean shutdown. Files the OS still has open — e.g. viewers started
    by ``QDesktopServices.openUrl`` — are skipped silently.
    """
    if not TEMP_DIR.exists():
        return
    for p in TEMP_DIR.iterdir():
        try:
            if p.is_file() or p.is_symlink():
                p.unlink()
            elif p.is_dir():
                import shutil
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            continue


# TradeStation timestamp format: "04/10/26 08:02:18 AM"
TS_DATETIME_FORMAT = "%m/%d/%y %I:%M:%S %p"

# Order types
TYPE_BUY = "Buy"
TYPE_SELL = "Sell"
TYPE_SELL_SHORT = "Sell Short"
TYPE_BUY_TO_COVER = "Buy to Cover"

ENTRY_TYPES = {TYPE_BUY, TYPE_SELL_SHORT}
EXIT_TYPES = {TYPE_SELL, TYPE_BUY_TO_COVER}

# Order statuses
STATUS_FILLED = "Filled"

# Directions
DIR_LONG = "Long"
DIR_SHORT = "Short"
