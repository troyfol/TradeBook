"""User-run cleanup: back up the dist DB, then delete every brief.

Usage (from Trade_Tracker/ with the project venv active):
    python scripts/wipe_dist_briefs.py

What it does:
    1. Open ``dist/data/tradebook.db``.
    2. Take a manual snapshot to ``dist/backups/manual/`` (never pruned
       — survives all future rebuilds and auto-retention sweeps).
    3. ``DELETE FROM briefs`` so the next launch starts with an empty
       Briefs tab.

Touches nothing else: trades, executions, journal entries, tags,
attachments, hidden_trades, daily_summary, deleted_trades, strategies,
and QSettings (registry on Windows) are all left exactly as they were.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Resolve project root from this script's location.
_THIS = Path(__file__).resolve()
_PROJECT = _THIS.parent.parent
sys.path.insert(0, str(_PROJECT))

from ingest import backups, db_manager  # noqa: E402

DB_PATH = _PROJECT / "dist" / "data" / "tradebook.db"
MANUAL_DIR = _PROJECT / "dist" / "backups" / "manual"


def main() -> int:
    if not DB_PATH.exists():
        print(f"DB not found: {DB_PATH}", file=sys.stderr)
        return 1
    MANUAL_DIR.mkdir(parents=True, exist_ok=True)

    conn = db_manager.connect(DB_PATH)
    try:
        n_before = conn.execute(
            "SELECT COUNT(*) FROM briefs"
        ).fetchone()[0]
        print(f"briefs before: {n_before}")
        if n_before == 0:
            print("nothing to delete; backup not taken.")
            return 0

        titles = [
            r[0] for r in conn.execute(
                "SELECT title FROM briefs ORDER BY updated_at DESC"
            ).fetchall()
        ]
        print("titles to be deleted:")
        for t in titles:
            print(f"  - {t}")

        backup_path = backups.create_manual_backup(conn, MANUAL_DIR)
        print(f"\nmanual backup written: {backup_path}")

        conn.execute("DELETE FROM briefs")
        conn.commit()
        n_after = conn.execute(
            "SELECT COUNT(*) FROM briefs"
        ).fetchone()[0]
        print(f"briefs after: {n_after}")
        print("\nIf you need to undo: restore the backup via the app's "
              "File menu -> Restore from backup, then pick the manual "
              "snapshot above.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
