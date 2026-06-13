"""SQLite backup snapshots with retention pruning.

A backup is a `.db` copy created via SQLite's online backup API (so it's
safe to take while the app holds an open connection). Snapshots live in
`BACKUPS_DIR` with names like `tradebook_20260411_142530.db`. The most
recent `MAX_BACKUPS` are kept and older ones are pruned.

Two natural call sites:
    * App startup: one safety snapshot per launch (deduped — if a backup
      from the last `MIN_INTERVAL_SECONDS` already exists, skip).
    * Pre-import in NewTradeTab: snapshot before mutating the DB so a bad
      paste can be rolled back by restoring the file.

The module is UI-agnostic. Callers pass an open `sqlite3.Connection` and
the destination directory; failures are returned as exceptions and the
caller decides whether to surface a status message.
"""
from __future__ import annotations

import datetime as _dt
import sqlite3
from pathlib import Path
from typing import Optional

# Auto retention: the app-taken snapshots (startup + pre-import + pre-
# restore) get capped to the most recent N. Age-based keep is disabled
# for auto by default — users who want long-term rollbacks should take
# manual snapshots (see `create_manual_backup`), which are never pruned.
MAX_AUTO_BACKUPS = 5
# Backwards-compat alias — `prune_backups`'s default cap. Older callers
# that don't pass `keep=` land here.
MAX_BACKUPS = MAX_AUTO_BACKUPS
MAX_BACKUP_AGE_DAYS = 0

# A startup snapshot taken less than this many seconds after the previous
# one is skipped — avoids spamming snapshots when the user relaunches
# the app rapidly.
MIN_INTERVAL_SECONDS = 60

# File naming
_FILENAME_PREFIX = "tradebook_"
_FILENAME_SUFFIX = ".db"
_TIMESTAMP_FORMAT = "%Y%m%d_%H%M%S"


def _format_timestamp(dt: _dt.datetime) -> str:
    return dt.strftime(_TIMESTAMP_FORMAT)


def _backup_filename(now: Optional[_dt.datetime] = None) -> str:
    now = now or _dt.datetime.now()
    return f"{_FILENAME_PREFIX}{_format_timestamp(now)}{_FILENAME_SUFFIX}"


def list_backups(backups_dir: Path) -> list[Path]:
    """Return existing backups sorted oldest → newest by filename.

    Filenames embed an ISO-ish timestamp so lexical sort == chronological.
    """
    if not backups_dir.exists():
        return []
    out = [
        p for p in backups_dir.iterdir()
        if p.is_file()
        and p.name.startswith(_FILENAME_PREFIX)
        and p.name.endswith(_FILENAME_SUFFIX)
    ]
    out.sort(key=lambda p: p.name)
    return out


def _seconds_since(path: Path, now: _dt.datetime) -> float:
    try:
        mtime = _dt.datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return float("inf")
    return (now - mtime).total_seconds()


def create_backup(
    conn: sqlite3.Connection,
    backups_dir: Path,
    *,
    now: Optional[_dt.datetime] = None,
) -> Path:
    """Snapshot `conn` to a new file in `backups_dir`. Returns the path.

    Uses SQLite's online backup API so it's safe while writers are active.
    Creates the directory if it doesn't exist.
    """
    backups_dir.mkdir(parents=True, exist_ok=True)
    target = backups_dir / _backup_filename(now)

    # If a same-second collision exists (rare but possible in tests),
    # bump until we find a free name.
    bump = 1
    while target.exists():
        target = backups_dir / (
            f"{_FILENAME_PREFIX}{_format_timestamp(now or _dt.datetime.now())}"
            f"_{bump}{_FILENAME_SUFFIX}"
        )
        bump += 1

    # The online backup API needs a destination connection.
    dest = sqlite3.connect(str(target))
    try:
        conn.backup(dest)
    finally:
        dest.close()
    return target


def prune_backups(
    backups_dir: Path,
    *,
    keep: int = MAX_BACKUPS,
    max_age_days: int = MAX_BACKUP_AGE_DAYS,
    now: Optional[_dt.datetime] = None,
) -> list[Path]:
    """Delete backups that fall outside retention. Returns deleted paths.

    A backup survives if *either* (a) it's among the ``keep`` newest, or
    (b) its mtime is within ``max_age_days``. The union means the user
    can roll back by count or by time — whichever is broader. Setting
    ``max_age_days <= 0`` disables the age rule and falls back to pure
    count-based pruning.
    """
    backups = list_backups(backups_dir)
    if not backups:
        return []
    now = now or _dt.datetime.now()
    age_cutoff = (
        now - _dt.timedelta(days=max_age_days)
        if max_age_days and max_age_days > 0 else None
    )

    # Indexes of the ``keep`` most recent entries always survive.
    keep_indexes: set[int] = set(range(max(0, len(backups) - keep), len(backups)))
    deleted: list[Path] = []
    for i, p in enumerate(backups):
        if i in keep_indexes:
            continue
        if age_cutoff is not None:
            try:
                mtime = _dt.datetime.fromtimestamp(p.stat().st_mtime)
            except OSError:
                # If we can't stat the file, don't delete it either — a
                # transient FS hiccup shouldn't lose a snapshot.
                continue
            if mtime >= age_cutoff:
                continue
        try:
            p.unlink()
            deleted.append(p)
        except OSError:
            # Best-effort: a locked file shouldn't kill the import flow.
            continue
    return deleted


def maybe_create_startup_backup(
    conn: sqlite3.Connection,
    backups_dir: Path,
    *,
    min_interval_seconds: int = MIN_INTERVAL_SECONDS,
    now: Optional[_dt.datetime] = None,
) -> Optional[Path]:
    """Create a startup backup unless a recent one already exists.

    Returns the new path, or None if a fresh-enough backup was found.
    Prunes the auto directory to ``MAX_AUTO_BACKUPS`` after a successful
    create.
    """
    now = now or _dt.datetime.now()
    existing = list_backups(backups_dir)
    if existing:
        newest = existing[-1]
        if _seconds_since(newest, now) < min_interval_seconds:
            return None
    path = create_backup(conn, backups_dir, now=now)
    prune_backups(backups_dir, keep=MAX_AUTO_BACKUPS, max_age_days=0)
    return path


def create_pre_import_backup(
    conn: sqlite3.Connection,
    backups_dir: Path,
    *,
    now: Optional[_dt.datetime] = None,
) -> Path:
    """Unconditional snapshot taken before a destructive import.

    Prunes to ``MAX_AUTO_BACKUPS`` so pre-import snapshots can't fill
    the auto folder unbounded.
    """
    path = create_backup(conn, backups_dir, now=now)
    prune_backups(backups_dir, keep=MAX_AUTO_BACKUPS, max_age_days=0)
    return path


def create_manual_backup(
    conn: sqlite3.Connection,
    manual_dir: Path,
    *,
    now: Optional[_dt.datetime] = None,
) -> Path:
    """User-triggered snapshot. Always creates, never prunes.

    Separate from the auto directory so a user's explicit "save a
    backup before I try this" snapshot survives the auto-retention
    cap indefinitely.
    """
    return create_backup(conn, manual_dir, now=now)


def list_all_backups(
    auto_dir: Path, manual_dir: Path,
) -> list[tuple[Path, str]]:
    """Return ``[(path, kind)]`` across both folders, newest first.

    ``kind`` is ``"auto"`` or ``"manual"`` — useful for surface-level
    differentiation in the restore dialog without forcing the caller
    to know the folder layout.
    """
    out: list[tuple[Path, str]] = []
    for p in list_backups(auto_dir):
        out.append((p, "auto"))
    for p in list_backups(manual_dir):
        out.append((p, "manual"))
    # Sort newest first by mtime (filename sort also works since the
    # timestamp is embedded, but mtime stays correct across file
    # copies that preserve mtime).
    def _mtime(pair: tuple[Path, str]) -> float:
        try:
            return pair[0].stat().st_mtime
        except OSError:
            return 0.0
    out.sort(key=_mtime, reverse=True)
    return out


def restore_backup(
    backup_path: Path,
    target_db_path: Path,
    *,
    safety_conn: Optional[sqlite3.Connection] = None,
    backups_dir: Optional[Path] = None,
) -> Optional[Path]:
    """Replace ``target_db_path`` with the contents of ``backup_path``.

    If ``safety_conn`` + ``backups_dir`` are supplied, a pre-restore
    snapshot of the *current* DB is taken first and its path is
    returned, so the user has a one-step undo if the restore brought
    back the wrong state.

    The caller must close any open connection to ``target_db_path``
    before invoking this — Windows in particular won't let us
    overwrite a file that's still mapped by SQLite.
    """
    backup_path = Path(backup_path)
    target_db_path = Path(target_db_path)
    if not backup_path.exists():
        raise FileNotFoundError(str(backup_path))

    safety_path: Optional[Path] = None
    if safety_conn is not None and backups_dir is not None:
        try:
            safety_path = create_backup(safety_conn, backups_dir)
        except sqlite3.Error:
            safety_path = None  # best-effort

    target_db_path.parent.mkdir(parents=True, exist_ok=True)
    # shutil.copy2 preserves mtime so the snapshot's age stays honest
    # for retention; using copy (not move) means the source backup
    # remains in place for further rollbacks.
    import shutil
    shutil.copy2(str(backup_path), str(target_db_path))
    return safety_path
