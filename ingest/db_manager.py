"""SQLite management: schema, dedup insert, trade rebuild, daily summary.

Scope:
    - init_schema: create all tables idempotently + seed default tags
    - insert_executions: INSERT OR IGNORE with dedup via import_hash
    - rebuild_trades: drop & recreate trades / trade_executions from current
      executions, snapshotting journal entries + tag links by
      (symbol, entry_time, direction) so they survive the rebuild (Phase 7).
    - rebuild_daily_summary: aggregate closed trades by exit date.
    - Phase 7 CRUD: journal entries, attachments (BLOB w/ sha256 dedup),
      tags, trade_tags, journal-text search.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Iterable, Optional

from ingest.trade_builder import build_trades, BuiltTrade


# Default tags — pre-seeded on first init_schema. Idempotent: existing
# DBs only gain rows that aren't already there, so a user who renamed
# or deleted the prior default set won't see them resurrected.
DEFAULT_TAGS: list[tuple[str, str]] = [
    ("Breakout", "#42A5F5"),
    ("EP_Earnings", "#66BB6A"),
    ("EP_Other", "#26C6DA"),
    ("Parabolic Long", "#FFA726"),
    ("Parabolic Short", "#EF5350"),
]


# Default thumbnail tag presets — seeded once into ``thumbnail_tags``
# when the table is empty. The user can rename or delete any of these
# without them resurrecting on the next launch (the seed only runs
# against a wholly empty table, mirroring seed_default_strategies).
DEFAULT_THUMBNAIL_TAGS: list[str] = ["60M", "Daily", "Weekly", "Monthly"]


# --- sqlite3 date/datetime adapters -----------------------------------------
# Python 3.12+ deprecates the default date/timestamp converters. Register
# explicit ones so DATE and TIMESTAMP columns round-trip cleanly.

def _adapt_date(d: _dt.date) -> str:
    return d.isoformat()


def _adapt_datetime(dt: _dt.datetime) -> str:
    return dt.isoformat(sep=" ")


def _convert_date(b: bytes) -> _dt.date:
    return _dt.date.fromisoformat(b.decode("utf-8"))


def _convert_timestamp(b: bytes) -> _dt.datetime:
    s = b.decode("utf-8")
    # Tolerate both "YYYY-MM-DD HH:MM:SS[.ffffff]" and ISO "T" separator.
    return _dt.datetime.fromisoformat(s.replace(" ", "T", 1) if " " in s else s)


sqlite3.register_adapter(_dt.date, _adapt_date)
sqlite3.register_adapter(_dt.datetime, _adapt_datetime)
sqlite3.register_converter("DATE", _convert_date)
sqlite3.register_converter("TIMESTAMP", _convert_timestamp)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS executions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entered_at TIMESTAMP NOT NULL,
    type TEXT NOT NULL,
    symbol TEXT NOT NULL,
    raw_symbol TEXT NOT NULL,
    symbol_extension TEXT,
    extension_type TEXT,
    order_status TEXT NOT NULL,
    stop_price REAL,
    limit_price REAL,
    filled_price REAL NOT NULL,
    quantity INTEGER NOT NULL,
    filled_canceled_at TIMESTAMP,
    qty_filled INTEGER NOT NULL,
    qty_left INTEGER NOT NULL,
    commission REAL NOT NULL DEFAULT 0,
    source_file TEXT,
    import_hash TEXT UNIQUE NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_executions_symbol_entered
    ON executions(symbol, entered_at);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    entry_time TIMESTAMP NOT NULL,
    exit_time TIMESTAMP,
    avg_entry_price REAL NOT NULL,
    avg_exit_price REAL,
    total_shares INTEGER NOT NULL,
    total_commission REAL NOT NULL DEFAULT 0,
    gross_pnl REAL,
    net_pnl REAL,
    hold_duration_seconds INTEGER,
    is_open BOOLEAN NOT NULL DEFAULT 0,
    is_manual BOOLEAN NOT NULL DEFAULT 0,
    -- Partial-close tracking for open positions that have been scaled out
    -- of. closed_shares = how many of total_shares are already exited while
    -- the trade is still open (equals total_shares once fully closed);
    -- realized_pnl / realized_net_pnl are the P&L on that closed slice,
    -- shown for process visibility but kept out of closed-trade analytics
    -- (net_pnl / exit_time stay NULL) until the whole position is flat.
    closed_shares INTEGER NOT NULL DEFAULT 0,
    realized_pnl REAL,
    realized_net_pnl REAL,
    -- Optional planned-stop price for R-multiple analytics. Null = no
    -- stop recorded for this trade.
    stop_loss_price REAL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_trades_entry_time ON trades(entry_time);
CREATE INDEX IF NOT EXISTS idx_trades_symbol    ON trades(symbol);
-- rebuild_trades filters / deletes by is_manual on every import; without
-- this index, large trade tables paid a full scan per rebuild cycle.
CREATE INDEX IF NOT EXISTS idx_trades_is_manual ON trades(is_manual);

CREATE TABLE IF NOT EXISTS trade_executions (
    trade_id INTEGER NOT NULL REFERENCES trades(id) ON DELETE CASCADE,
    execution_id INTEGER NOT NULL REFERENCES executions(id) ON DELETE CASCADE,
    PRIMARY KEY (trade_id, execution_id)
);

CREATE TABLE IF NOT EXISTS journal_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id INTEGER NOT NULL UNIQUE REFERENCES trades(id) ON DELETE CASCADE,
    notes TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    color TEXT NOT NULL DEFAULT '#888888'
);

CREATE TABLE IF NOT EXISTS trade_tags (
    trade_id INTEGER NOT NULL REFERENCES trades(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (trade_id, tag_id)
);

-- Phase 7: journal attachments stored as BLOBs, deduped by sha256.
-- Referenced from journal HTML via `attachment:N` URLs (resolved via
-- QTextDocument.loadResource in the JournalEditor widget).
CREATE TABLE IF NOT EXISTS journal_attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256 TEXT NOT NULL UNIQUE,
    mime TEXT NOT NULL,
    filename TEXT,
    bytes BLOB NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_attachments_sha ON journal_attachments(sha256);

CREATE TABLE IF NOT EXISTS daily_summary (
    date DATE PRIMARY KEY,
    gross_pnl REAL NOT NULL DEFAULT 0,
    net_pnl REAL NOT NULL DEFAULT 0,
    trade_count INTEGER NOT NULL DEFAULT 0,
    win_count INTEGER NOT NULL DEFAULT 0,
    loss_count INTEGER NOT NULL DEFAULT 0,
    cumulative_pnl REAL NOT NULL DEFAULT 0
);

-- Hidden trades: keyed by (symbol, entry_time, direction) so hide state
-- survives rebuild_trades drops. Matches the deterministic-identity tuple
-- we'll adopt in Phase 7 for journal preservation.
CREATE TABLE IF NOT EXISTS hidden_trades (
    symbol TEXT NOT NULL,
    entry_time TIMESTAMP NOT NULL,
    direction TEXT NOT NULL,
    hidden_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (symbol, entry_time, direction)
);

-- Phase 9: generated briefs — user-editable documents that aggregate
-- journal text (and optionally images) across a filtered set of trades.
-- HTML stored here uses the same `attachment:N` URL convention as
-- journal_entries so the same editor widget renders both.
CREATE TABLE IF NOT EXISTS briefs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    content_html TEXT NOT NULL DEFAULT '',
    thumbnail_ids TEXT,   -- JSON list of attachment ids; NULL = none
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_briefs_updated_at
    ON briefs(updated_at DESC);

-- Strategy pages — long-form, image-heavy playbooks for individual
-- setups. Same shape as briefs (HTML + attachment:N image refs) but
-- stored separately so the two surfaces don't cross-contaminate.
-- Default seed = one row per DEFAULT_TAGS entry (idempotent on
-- re-launch via seed_default_strategies).
CREATE TABLE IF NOT EXISTS strategies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    content_html TEXT NOT NULL DEFAULT '',
    thumbnail_ids TEXT,   -- JSON list of attachment ids; NULL = none
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_strategies_updated_at
    ON strategies(updated_at DESC);

-- Thumbnail tags — preset pool shared across Briefs and Strategies.
-- The presets themselves are global (one row per name); the per-image
-- assignment is per (doc_type, doc_id, attachment_id) so the same
-- image attachment opted into thumbnails on both a brief and a
-- strategy can carry an independent tag set on each. ON DELETE CASCADE
-- on tag_id means deleting a preset transparently drops every
-- assignment that referenced it.
CREATE TABLE IF NOT EXISTS thumbnail_tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS thumbnail_tag_links (
    doc_type TEXT NOT NULL CHECK(doc_type IN ('brief','strategy')),
    doc_id INTEGER NOT NULL,
    attachment_id INTEGER NOT NULL,
    tag_id INTEGER NOT NULL
        REFERENCES thumbnail_tags(id) ON DELETE CASCADE,
    PRIMARY KEY (doc_type, doc_id, attachment_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_thumb_tag_links_doc
    ON thumbnail_tag_links(doc_type, doc_id);

-- Phase 11: recycle bin — when a trade is deleted, the full row plus
-- its linked executions, journal entry, and tag links are JSON-snapshot
-- in here. Rows older than the retention window are purged on startup.
-- Restoring a row inserts a brand-new trade (new id) populated from
-- the snapshot, so referential integrity stays clean.
CREATE TABLE IF NOT EXISTS deleted_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deleted_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    entry_time TIMESTAMP NOT NULL,
    net_pnl REAL,                       -- denormalized for list display
    is_manual BOOLEAN NOT NULL DEFAULT 0,
    snapshot_json TEXT NOT NULL         -- {trade, executions, journal, tag_ids}
);
CREATE INDEX IF NOT EXISTS idx_deleted_trades_deleted_at
    ON deleted_trades(deleted_at DESC);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(db_path),
        detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _column_exists(
    conn: sqlite3.Connection, table: str, column: str,
) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == column for r in rows)


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Apply lightweight in-place migrations for existing databases.

    SQLite's ``ALTER TABLE … ADD COLUMN`` is the only schema delta we
    use — it's cheap, never rewrites the table, and a fresh DB is
    already correct via ``SCHEMA_SQL``.
    """
    if not _column_exists(conn, "trades", "stop_loss_price"):
        conn.execute("ALTER TABLE trades ADD COLUMN stop_loss_price REAL")
    # Partial-close tracking (scale-outs on still-open positions). Constant
    # defaults keep the ADD COLUMN cheap and leave existing rows valid —
    # they get correct values on the next rebuild_trades cycle.
    if not _column_exists(conn, "trades", "closed_shares"):
        conn.execute(
            "ALTER TABLE trades ADD COLUMN closed_shares "
            "INTEGER NOT NULL DEFAULT 0"
        )
    if not _column_exists(conn, "trades", "realized_pnl"):
        conn.execute("ALTER TABLE trades ADD COLUMN realized_pnl REAL")
    if not _column_exists(conn, "trades", "realized_net_pnl"):
        conn.execute("ALTER TABLE trades ADD COLUMN realized_net_pnl REAL")
    # Per-document thumbnail selection — JSON-encoded list of
    # attachment ids that should appear in the thumbnail strip. NULL /
    # missing / empty list means "no thumbnails", matching the new
    # opt-in behaviour. Stored as TEXT so existing rows don't need
    # rewriting.
    if not _column_exists(conn, "briefs", "thumbnail_ids"):
        conn.execute("ALTER TABLE briefs ADD COLUMN thumbnail_ids TEXT")
    if not _column_exists(conn, "strategies", "thumbnail_ids"):
        conn.execute("ALTER TABLE strategies ADD COLUMN thumbnail_ids TEXT")
    # Existing DBs predate idx_trades_is_manual — CREATE INDEX IF NOT
    # EXISTS is idempotent and cheap on an indexed-already path, so we
    # can run it unconditionally rather than introspecting the catalog.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_trades_is_manual "
        "ON trades(is_manual)"
    )
    conn.commit()


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    _migrate_schema(conn)
    seed_default_tags(conn)
    seed_default_strategies(conn)
    seed_default_thumbnail_tags(conn)
    conn.commit()


def seed_default_tags(conn: sqlite3.Connection) -> None:
    """Insert default tags if not already present (idempotent)."""
    cur = conn.cursor()
    for name, color in DEFAULT_TAGS:
        cur.execute(
            "INSERT OR IGNORE INTO tags (name, color) VALUES (?, ?)",
            (name, color),
        )
    conn.commit()


def seed_default_strategies(conn: sqlite3.Connection) -> None:
    """Insert one empty strategy page per DEFAULT_TAGS entry, but only
    when the strategies table is completely empty.

    Re-running ``init_schema`` on a populated DB never resurrects pages
    the user has deleted — the table-level "is empty" check is the only
    trigger. New users (and DBs that never had strategies) get a row
    per default tag so they have a starting set to write into.
    """
    (existing,) = conn.execute(
        "SELECT COUNT(*) FROM strategies"
    ).fetchone()
    if existing:
        return
    cur = conn.cursor()
    for name, _color in DEFAULT_TAGS:
        cur.execute(
            "INSERT INTO strategies (title, content_html) VALUES (?, ?)",
            (name, ""),
        )
    conn.commit()


def insert_executions(
    conn: sqlite3.Connection,
    executions: Iterable[dict],
    source: str = "paste",
    *,
    auto_commit: bool = True,
) -> tuple[int, int]:
    """INSERT OR IGNORE new executions. Returns (inserted, skipped).

    Set ``auto_commit=False`` when the caller needs to run additional
    mutations (e.g. ``rebuild_trades``) in the same transaction and
    will call ``conn.commit()`` / ``conn.rollback()`` itself.
    """
    inserted = 0
    skipped = 0
    cur = conn.cursor()
    for ex in executions:
        cur.execute(
            """
            INSERT OR IGNORE INTO executions (
                entered_at, type, symbol, raw_symbol, symbol_extension,
                extension_type, order_status, stop_price, limit_price,
                filled_price, quantity, filled_canceled_at, qty_filled,
                qty_left, commission, source_file, import_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ex["entered_at"], ex["type"], ex["symbol"], ex["raw_symbol"],
                ex["symbol_extension"], ex["extension_type"], ex["order_status"],
                ex["stop_price"], ex["limit_price"], ex["filled_price"],
                ex["quantity"], ex["filled_canceled_at"], ex["qty_filled"],
                ex["qty_left"], ex["commission"], source, ex["import_hash"],
            ),
        )
        if cur.rowcount == 1:
            inserted += 1
        else:
            skipped += 1
    if auto_commit:
        conn.commit()
    return inserted, skipped


def _fetch_executions_as_dicts(conn: sqlite3.Connection) -> list[dict]:
    cur = conn.execute(
        """
        SELECT id, entered_at, type, symbol, raw_symbol, symbol_extension,
               extension_type, order_status, stop_price, limit_price,
               filled_price, quantity, filled_canceled_at, qty_filled,
               qty_left, commission, import_hash
        FROM executions
        WHERE order_status = 'Filled'
        ORDER BY entered_at, id
        """
    )
    rows = []
    for r in cur.fetchall():
        rows.append({
            "id": r["id"],
            "entered_at": r["entered_at"],
            "type": r["type"],
            "symbol": r["symbol"],
            "raw_symbol": r["raw_symbol"],
            "symbol_extension": r["symbol_extension"],
            "extension_type": r["extension_type"],
            "order_status": r["order_status"],
            "stop_price": r["stop_price"],
            "limit_price": r["limit_price"],
            "filled_price": r["filled_price"],
            "quantity": r["quantity"],
            "filled_canceled_at": r["filled_canceled_at"],
            "qty_filled": r["qty_filled"],
            "qty_left": r["qty_left"],
            "commission": r["commission"],
            "import_hash": r["import_hash"],
        })
    return rows


def rebuild_trades(conn: sqlite3.Connection) -> tuple[int, list[str]]:
    """Rebuild the trades + trade_executions tables from current executions.

    Drops non-manual trades and rebuilds them from `executions`. Manual
    trades (is_manual=1) are preserved. Journal entries and tag links on
    derived trades survive the drop because we snapshot them keyed by
    (symbol, entry_time, direction) and re-link after rebuild — same
    deterministic identity tuple as `hidden_trades`.

    Returns (trade_count, errors).
    """
    executions = _fetch_executions_as_dicts(conn)

    # Map import_hash → execution_id for trade_executions linking
    hash_to_id = {ex["import_hash"]: ex["id"] for ex in executions}

    trades, errors = build_trades(executions)

    cur = conn.cursor()

    # ---- snapshot derived-trade journal + tags before drop ----
    journal_snapshot: dict[tuple, tuple[str, _dt.datetime]] = {}
    for r in cur.execute(
        """
        SELECT t.symbol, t.entry_time, t.direction, j.notes, j.created_at
        FROM journal_entries j
        JOIN trades t ON t.id = j.trade_id
        WHERE t.is_manual = 0
        """
    ).fetchall():
        key = (r["symbol"], r["entry_time"], r["direction"])
        journal_snapshot[key] = (r["notes"], r["created_at"])

    tag_snapshot: dict[tuple, list[int]] = {}
    for r in cur.execute(
        """
        SELECT t.symbol, t.entry_time, t.direction, tt.tag_id
        FROM trade_tags tt
        JOIN trades t ON t.id = tt.trade_id
        WHERE t.is_manual = 0
        """
    ).fetchall():
        key = (r["symbol"], r["entry_time"], r["direction"])
        tag_snapshot.setdefault(key, []).append(r["tag_id"])

    # Phase 12: stop-loss survives rebuild_trades too — same
    # (symbol, entry_time, direction) identity tuple.
    stop_snapshot: dict[tuple, Optional[float]] = {}
    for r in cur.execute(
        """
        SELECT symbol, entry_time, direction, stop_loss_price
        FROM trades
        WHERE is_manual = 0 AND stop_loss_price IS NOT NULL
        """
    ).fetchall():
        key = (r["symbol"], r["entry_time"], r["direction"])
        stop_snapshot[key] = r["stop_loss_price"]

    # Delete derived trades (manual trades have no rows in trade_executions
    # but are distinguished by is_manual = 1)
    cur.execute("DELETE FROM trades WHERE is_manual = 0")
    # trade_executions rows for deleted trades are removed via ON DELETE CASCADE
    # journal_entries / trade_tags also cascade-drop here.

    for t in trades:
        cur.execute(
            """
            INSERT INTO trades (
                symbol, direction, entry_time, exit_time,
                avg_entry_price, avg_exit_price, total_shares,
                total_commission, gross_pnl, net_pnl,
                hold_duration_seconds, is_open, is_manual,
                closed_shares, realized_pnl, realized_net_pnl
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
            """,
            (
                t.symbol, t.direction, t.entry_time, t.exit_time,
                t.avg_entry_price, t.avg_exit_price, t.total_shares,
                t.total_commission, t.gross_pnl, t.net_pnl,
                t.hold_duration_seconds, 1 if t.is_open else 0,
                t.closed_shares, t.realized_pnl, t.realized_net_pnl,
            ),
        )
        trade_id = cur.lastrowid
        for h in t.execution_hashes:
            exec_id = hash_to_id.get(h)
            if exec_id is None:
                continue
            cur.execute(
                "INSERT OR IGNORE INTO trade_executions (trade_id, execution_id) VALUES (?, ?)",
                (trade_id, exec_id),
            )

        # ---- restore journal + tag links + stop-loss if this trade
        # matches a snapshot ----
        key = (t.symbol, t.entry_time, t.direction)
        if key in journal_snapshot:
            notes, created_at = journal_snapshot[key]
            cur.execute(
                """
                INSERT INTO journal_entries (trade_id, notes, created_at, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (trade_id, notes, created_at),
            )
        if key in tag_snapshot:
            for tag_id in tag_snapshot[key]:
                cur.execute(
                    "INSERT OR IGNORE INTO trade_tags (trade_id, tag_id) VALUES (?, ?)",
                    (trade_id, tag_id),
                )
        if key in stop_snapshot and stop_snapshot[key] is not None:
            cur.execute(
                "UPDATE trades SET stop_loss_price = ? WHERE id = ?",
                (stop_snapshot[key], trade_id),
            )

    conn.commit()
    return len(trades), errors


def rebuild_daily_summary(conn: sqlite3.Connection) -> int:
    """Rebuild the daily_summary table from closed trades.

    Groups by DATE(exit_time). Running cumulative P&L is computed in a
    second pass over the aggregated rows.
    """
    cur = conn.cursor()
    cur.execute("DELETE FROM daily_summary")
    cur.execute(
        """
        SELECT DATE(exit_time)                             AS date,
               COALESCE(SUM(gross_pnl), 0)                 AS gross_pnl,
               COALESCE(SUM(net_pnl), 0)                   AS net_pnl,
               COUNT(*)                                    AS trade_count,
               SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END) AS win_count,
               SUM(CASE WHEN net_pnl < 0 THEN 1 ELSE 0 END) AS loss_count
        FROM trades
        WHERE exit_time IS NOT NULL AND net_pnl IS NOT NULL
        GROUP BY DATE(exit_time)
        ORDER BY DATE(exit_time)
        """
    )
    rows = cur.fetchall()

    cumulative = 0.0
    for r in rows:
        cumulative += r["net_pnl"] or 0.0
        cur.execute(
            """
            INSERT INTO daily_summary
                (date, gross_pnl, net_pnl, trade_count,
                 win_count, loss_count, cumulative_pnl)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (r["date"], r["gross_pnl"], r["net_pnl"], r["trade_count"],
             r["win_count"], r["loss_count"], cumulative),
        )
    conn.commit()
    return len(rows)


# --- display / mutation helpers (Phase 2) -----------------------------------

def fetch_closed_trades(conn: sqlite3.Connection) -> list[dict]:
    """Return closed trades (net_pnl not null) ordered by exit_time ascending.

    Used by analytics / dashboard. Ignores the hidden_trades filter since
    hide is a UI-level concept specific to the Trades tab.
    """
    sql = """
        SELECT *
        FROM trades
        WHERE exit_time IS NOT NULL AND net_pnl IS NOT NULL
        ORDER BY exit_time ASC, id ASC
    """
    return [dict(r) for r in conn.execute(sql).fetchall()]


def fetch_open_trades(conn: sqlite3.Connection) -> list[dict]:
    """Return currently-open trades ordered by entry_time ascending.

    A trade is considered open when ``exit_time IS NULL`` — covers both
    derived trades whose state machine never closed and manual trades
    explicitly entered with no exit. Hidden trades are omitted because
    "open positions" is a live operational view, not a journal browse.
    """
    sql = """
        SELECT t.*
        FROM trades t
        LEFT JOIN hidden_trades h
          ON h.symbol = t.symbol
         AND h.entry_time = t.entry_time
         AND h.direction = t.direction
        WHERE t.exit_time IS NULL
          AND h.symbol IS NULL
        ORDER BY t.entry_time ASC, t.id ASC
    """
    return [dict(r) for r in conn.execute(sql).fetchall()]


def fetch_trades_for_display(
    conn: sqlite3.Connection,
    include_hidden: bool = False,
) -> list[dict]:
    """Return trade rows ordered newest-first, optionally including hidden."""
    if include_hidden:
        sql = """
            SELECT t.*, 0 AS is_hidden
            FROM trades t
            ORDER BY t.entry_time DESC, t.id DESC
        """
    else:
        sql = """
            SELECT t.*, 0 AS is_hidden
            FROM trades t
            LEFT JOIN hidden_trades h
              ON h.symbol = t.symbol
             AND h.entry_time = t.entry_time
             AND h.direction = t.direction
            WHERE h.symbol IS NULL
            ORDER BY t.entry_time DESC, t.id DESC
        """
    return [dict(r) for r in conn.execute(sql).fetchall()]


def hide_trade(
    conn: sqlite3.Connection,
    symbol: str,
    entry_time: _dt.datetime,
    direction: str,
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO hidden_trades (symbol, entry_time, direction)
        VALUES (?, ?, ?)
        """,
        (symbol, entry_time, direction),
    )
    conn.commit()


def unhide_trade(
    conn: sqlite3.Connection,
    symbol: str,
    entry_time: _dt.datetime,
    direction: str,
) -> None:
    conn.execute(
        """
        DELETE FROM hidden_trades
        WHERE symbol = ? AND entry_time = ? AND direction = ?
        """,
        (symbol, entry_time, direction),
    )
    conn.commit()


def delete_trade(conn: sqlite3.Connection, trade_id: int) -> bool:
    """Hard-delete a trade.

    For derived trades: deletes the linked executions (which drops the
    corresponding trade row via cascade), then rebuilds trades + daily
    summary so the remaining state is consistent.

    For manual trades: deletes the trade row directly (no executions).

    Returns True if a trade was actually deleted.
    """
    row = conn.execute(
        "SELECT id, is_manual FROM trades WHERE id = ?", (trade_id,)
    ).fetchone()
    if row is None:
        return False

    if row["is_manual"]:
        conn.execute("DELETE FROM trades WHERE id = ?", (trade_id,))
        conn.commit()
        rebuild_daily_summary(conn)
        return True

    exec_ids = [
        r[0] for r in conn.execute(
            "SELECT execution_id FROM trade_executions WHERE trade_id = ?",
            (trade_id,),
        ).fetchall()
    ]
    if exec_ids:
        placeholders = ",".join("?" * len(exec_ids))
        conn.execute(
            f"DELETE FROM executions WHERE id IN ({placeholders})",
            exec_ids,
        )
    conn.commit()
    rebuild_trades(conn)
    rebuild_daily_summary(conn)
    return True


def delete_trades(
    conn: sqlite3.Connection, trade_ids: list[int],
) -> int:
    """Bulk-delete trades. Single rebuild pass at the end.

    Manual trades (`is_manual=1`) are hard-deleted directly. Derived
    trades have their underlying executions removed; the subsequent
    `rebuild_trades` drops the now-orphaned trade rows.

    Returns the number of trade rows actually matched (and removed).
    """
    if not trade_ids:
        return 0

    placeholders = ",".join("?" * len(trade_ids))
    rows = conn.execute(
        f"SELECT id, is_manual FROM trades WHERE id IN ({placeholders})",
        trade_ids,
    ).fetchall()
    if not rows:
        return 0

    manual_ids = [int(r["id"]) for r in rows if r["is_manual"]]
    derived_ids = [int(r["id"]) for r in rows if not r["is_manual"]]

    cur = conn.cursor()
    if manual_ids:
        ph = ",".join("?" * len(manual_ids))
        cur.execute(f"DELETE FROM trades WHERE id IN ({ph})", manual_ids)

    if derived_ids:
        ph = ",".join("?" * len(derived_ids))
        exec_ids = [
            int(r[0]) for r in cur.execute(
                f"""
                SELECT execution_id FROM trade_executions
                WHERE trade_id IN ({ph})
                """,
                derived_ids,
            ).fetchall()
        ]
        if exec_ids:
            ph2 = ",".join("?" * len(exec_ids))
            cur.execute(
                f"DELETE FROM executions WHERE id IN ({ph2})", exec_ids,
            )

    conn.commit()

    if derived_ids:
        rebuild_trades(conn)
    rebuild_daily_summary(conn)
    return len(rows)


# --- Phase 7: journal entries -----------------------------------------------

def fetch_journal_entry(
    conn: sqlite3.Connection, trade_id: int,
) -> str:
    """Return the HTML notes for a trade. Empty string if no entry exists."""
    row = conn.execute(
        "SELECT notes FROM journal_entries WHERE trade_id = ?", (trade_id,),
    ).fetchone()
    return row["notes"] if row else ""


def save_journal_entry(
    conn: sqlite3.Connection, trade_id: int, notes: str,
) -> None:
    """Upsert journal text for a trade.

    Empty/whitespace-only notes delete the row instead of storing blank
    HTML — keeps `journal_entries` populated only when meaningful.
    """
    stripped = (notes or "").strip()
    cur = conn.cursor()
    if not stripped:
        cur.execute(
            "DELETE FROM journal_entries WHERE trade_id = ?", (trade_id,),
        )
        conn.commit()
        return
    # Try update first, fall back to insert.
    cur.execute(
        """
        UPDATE journal_entries
           SET notes = ?, updated_at = CURRENT_TIMESTAMP
         WHERE trade_id = ?
        """,
        (notes, trade_id),
    )
    if cur.rowcount == 0:
        cur.execute(
            """
            INSERT INTO journal_entries (trade_id, notes)
            VALUES (?, ?)
            """,
            (trade_id, notes),
        )
    conn.commit()


def has_journal_entry(conn: sqlite3.Connection, trade_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM journal_entries WHERE trade_id = ?", (trade_id,),
    ).fetchone()
    return row is not None


def trade_ids_with_journal(conn: sqlite3.Connection) -> set[int]:
    return {
        r["trade_id"] for r in conn.execute(
            "SELECT trade_id FROM journal_entries"
        ).fetchall()
    }


def search_journal_entries(
    conn: sqlite3.Connection, query: str,
) -> set[int]:
    """Return trade ids whose journal text contains `query` (case-insensitive)."""
    if not query:
        return set()
    pattern = f"%{query}%"
    return {
        r["trade_id"] for r in conn.execute(
            "SELECT trade_id FROM journal_entries WHERE notes LIKE ? COLLATE NOCASE",
            (pattern,),
        ).fetchall()
    }


# --- Phase 7: attachments (BLOB w/ sha256 dedup) ----------------------------

def insert_attachment(
    conn: sqlite3.Connection,
    mime: str,
    data: bytes,
    filename: Optional[str] = None,
) -> int:
    """Insert a file blob, deduping by SHA-256. Returns the attachment id."""
    sha = hashlib.sha256(data).hexdigest()
    row = conn.execute(
        "SELECT id FROM journal_attachments WHERE sha256 = ?", (sha,),
    ).fetchone()
    if row is not None:
        return int(row["id"])
    cur = conn.execute(
        """
        INSERT INTO journal_attachments (sha256, mime, filename, bytes)
        VALUES (?, ?, ?, ?)
        """,
        (sha, mime, filename, sqlite3.Binary(data)),
    )
    conn.commit()
    return int(cur.lastrowid)


def fetch_attachment(
    conn: sqlite3.Connection, attachment_id: int,
) -> Optional[tuple[str, bytes, Optional[str]]]:
    """Return (mime, bytes, filename) for an attachment, or None."""
    row = conn.execute(
        "SELECT mime, bytes, filename FROM journal_attachments WHERE id = ?",
        (attachment_id,),
    ).fetchone()
    if row is None:
        return None
    return (row["mime"], bytes(row["bytes"]), row["filename"])


# --- Phase 7: tags + trade_tags ---------------------------------------------

def fetch_all_tags(conn: sqlite3.Connection) -> list[dict]:
    """Return all tags ordered by name."""
    return [
        dict(r) for r in conn.execute(
            "SELECT id, name, color FROM tags ORDER BY name COLLATE NOCASE"
        ).fetchall()
    ]


def create_tag(
    conn: sqlite3.Connection, name: str, color: str = "#888888",
) -> int:
    """Insert a new tag. Raises sqlite3.IntegrityError on duplicate name."""
    cur = conn.execute(
        "INSERT INTO tags (name, color) VALUES (?, ?)",
        (name.strip(), color),
    )
    conn.commit()
    return int(cur.lastrowid)


def delete_tag(conn: sqlite3.Connection, tag_id: int) -> None:
    """Delete a tag globally. trade_tags rows cascade-drop."""
    conn.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
    conn.commit()


def fetch_trade_tag_ids(
    conn: sqlite3.Connection, trade_id: int,
) -> list[int]:
    return [
        r["tag_id"] for r in conn.execute(
            "SELECT tag_id FROM trade_tags WHERE trade_id = ? ORDER BY tag_id",
            (trade_id,),
        ).fetchall()
    ]


def set_trade_tags(
    conn: sqlite3.Connection, trade_id: int, tag_ids: list[int],
) -> None:
    """Replace the tag set for a trade with the given list."""
    cur = conn.cursor()
    cur.execute("DELETE FROM trade_tags WHERE trade_id = ?", (trade_id,))
    for tid in tag_ids:
        cur.execute(
            "INSERT OR IGNORE INTO trade_tags (trade_id, tag_id) VALUES (?, ?)",
            (trade_id, tid),
        )
    conn.commit()


def trade_ids_with_any_tag(
    conn: sqlite3.Connection, tag_ids: list[int],
) -> set[int]:
    """Return trade ids that have at least one of the given tags."""
    if not tag_ids:
        return set()
    placeholders = ",".join("?" * len(tag_ids))
    return {
        r["trade_id"] for r in conn.execute(
            f"SELECT DISTINCT trade_id FROM trade_tags WHERE tag_id IN ({placeholders})",
            tag_ids,
        ).fetchall()
    }


# --- Phase 9: briefs --------------------------------------------------------

def fetch_briefs(conn: sqlite3.Connection) -> list[dict]:
    """Return all briefs ordered by last-edited (updated_at DESC)."""
    return [
        dict(r) for r in conn.execute(
            """
            SELECT id, title, content_html, created_at, updated_at
            FROM briefs
            ORDER BY updated_at DESC, id DESC
            """
        ).fetchall()
    ]


def fetch_brief(conn: sqlite3.Connection, brief_id: int) -> Optional[dict]:
    row = conn.execute(
        """
        SELECT id, title, content_html, created_at, updated_at
        FROM briefs WHERE id = ?
        """,
        (brief_id,),
    ).fetchone()
    return dict(row) if row else None


def insert_brief(
    conn: sqlite3.Connection, title: str, content_html: str = "",
) -> int:
    """Create a new brief and return its id."""
    cur = conn.execute(
        """
        INSERT INTO briefs (title, content_html)
        VALUES (?, ?)
        """,
        (title, content_html),
    )
    conn.commit()
    return int(cur.lastrowid)


def update_brief(
    conn: sqlite3.Connection,
    brief_id: int,
    *,
    title: Optional[str] = None,
    content_html: Optional[str] = None,
) -> None:
    """Update title and/or content_html; bumps updated_at."""
    sets: list[str] = []
    params: list = []
    if title is not None:
        sets.append("title = ?")
        params.append(title)
    if content_html is not None:
        sets.append("content_html = ?")
        params.append(content_html)
    if not sets:
        return
    sets.append("updated_at = CURRENT_TIMESTAMP")
    params.append(brief_id)
    conn.execute(
        f"UPDATE briefs SET {', '.join(sets)} WHERE id = ?",
        params,
    )
    conn.commit()


def delete_brief(conn: sqlite3.Connection, brief_id: int) -> None:
    conn.execute("DELETE FROM briefs WHERE id = ?", (brief_id,))
    conn.commit()


def search_briefs(
    conn: sqlite3.Connection, query: str,
) -> set[int]:
    """Return brief ids whose title or content matches `query`
    (case-insensitive LIKE)."""
    if not query:
        return set()
    pattern = f"%{query}%"
    return {
        r["id"] for r in conn.execute(
            """
            SELECT id FROM briefs
            WHERE title LIKE ? COLLATE NOCASE
               OR content_html LIKE ? COLLATE NOCASE
            """,
            (pattern, pattern),
        ).fetchall()
    }


# --- Strategies CRUD -------------------------------------------------------
# Mirrors the briefs CRUD — schema is identical so the JournalEditor can
# render either one with no special-casing.


def fetch_strategies(conn: sqlite3.Connection) -> list[dict]:
    """Return all strategy pages ordered newest-edited first."""
    return [
        dict(r) for r in conn.execute(
            """
            SELECT id, title, content_html, created_at, updated_at
            FROM strategies
            ORDER BY updated_at DESC, id DESC
            """
        ).fetchall()
    ]


def fetch_strategy(
    conn: sqlite3.Connection, strategy_id: int,
) -> Optional[dict]:
    row = conn.execute(
        """
        SELECT id, title, content_html, created_at, updated_at
        FROM strategies WHERE id = ?
        """,
        (strategy_id,),
    ).fetchone()
    return dict(row) if row else None


def insert_strategy(
    conn: sqlite3.Connection, title: str, content_html: str = "",
) -> int:
    cur = conn.execute(
        "INSERT INTO strategies (title, content_html) VALUES (?, ?)",
        (title, content_html),
    )
    conn.commit()
    return int(cur.lastrowid)


def update_strategy(
    conn: sqlite3.Connection,
    strategy_id: int,
    *,
    title: Optional[str] = None,
    content_html: Optional[str] = None,
) -> None:
    sets: list[str] = []
    params: list = []
    if title is not None:
        sets.append("title = ?")
        params.append(title)
    if content_html is not None:
        sets.append("content_html = ?")
        params.append(content_html)
    if not sets:
        return
    sets.append("updated_at = CURRENT_TIMESTAMP")
    params.append(strategy_id)
    conn.execute(
        f"UPDATE strategies SET {', '.join(sets)} WHERE id = ?",
        params,
    )
    conn.commit()


def delete_strategy(conn: sqlite3.Connection, strategy_id: int) -> None:
    conn.execute("DELETE FROM strategies WHERE id = ?", (strategy_id,))
    conn.commit()


def search_strategies(
    conn: sqlite3.Connection, query: str,
) -> set[int]:
    """Return strategy ids whose title or content matches `query`
    (case-insensitive LIKE)."""
    if not query:
        return set()
    pattern = f"%{query}%"
    return {
        r["id"] for r in conn.execute(
            """
            SELECT id FROM strategies
            WHERE title LIKE ? COLLATE NOCASE
               OR content_html LIKE ? COLLATE NOCASE
            """,
            (pattern, pattern),
        ).fetchall()
    }


# --- Per-document thumbnail selection --------------------------------------
# Briefs and strategies both have a ``thumbnail_ids`` column that holds a
# JSON list of attachment ids opted in to the thumbnail strip. NULL or an
# unparseable payload is treated as "no thumbnails" — the new default
# after the opt-in switchover.


def _decode_thumbnail_ids(raw) -> set[int]:
    if raw is None:
        return set()
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return set()
    if not isinstance(parsed, list):
        return set()
    out: set[int] = set()
    for v in parsed:
        try:
            out.add(int(v))
        except (TypeError, ValueError):
            continue
    return out


def _encode_thumbnail_ids(ids: set[int]) -> Optional[str]:
    # Sort for deterministic on-disk diffs and stable test snapshots.
    cleaned = sorted({int(v) for v in ids})
    return json.dumps(cleaned) if cleaned else None


def fetch_brief_thumbnail_ids(
    conn: sqlite3.Connection, brief_id: int,
) -> set[int]:
    row = conn.execute(
        "SELECT thumbnail_ids FROM briefs WHERE id = ?", (brief_id,),
    ).fetchone()
    if row is None:
        return set()
    return _decode_thumbnail_ids(row["thumbnail_ids"])


def set_brief_thumbnail_ids(
    conn: sqlite3.Connection, brief_id: int, ids: set[int],
) -> None:
    """Persist the opt-in thumbnail set for a brief.

    Does NOT bump ``updated_at`` — marking a thumbnail is a curation
    action, not a content edit, and we don't want it to reorder the
    list or trigger autosave-style refreshes on other tabs.
    """
    conn.execute(
        "UPDATE briefs SET thumbnail_ids = ? WHERE id = ?",
        (_encode_thumbnail_ids(ids), brief_id),
    )
    conn.commit()


def fetch_strategy_thumbnail_ids(
    conn: sqlite3.Connection, strategy_id: int,
) -> set[int]:
    row = conn.execute(
        "SELECT thumbnail_ids FROM strategies WHERE id = ?",
        (strategy_id,),
    ).fetchone()
    if row is None:
        return set()
    return _decode_thumbnail_ids(row["thumbnail_ids"])


def set_strategy_thumbnail_ids(
    conn: sqlite3.Connection, strategy_id: int, ids: set[int],
) -> None:
    conn.execute(
        "UPDATE strategies SET thumbnail_ids = ? WHERE id = ?",
        (_encode_thumbnail_ids(ids), strategy_id),
    )
    conn.commit()


# --- Thumbnail tag presets + per-image assignments -------------------------
# Presets (``thumbnail_tags``) are a single global pool shared by Briefs and
# Strategies. Assignments (``thumbnail_tag_links``) are scoped to
# (doc_type, doc_id, attachment_id) so the same attachment opted into
# thumbnails on two different documents carries an independent tag set
# per document. ON DELETE CASCADE on tag_id keeps link rows from
# outliving the preset they reference.


_THUMB_DOC_TYPES = ("brief", "strategy")


def _check_doc_type(doc_type: str) -> str:
    if doc_type not in _THUMB_DOC_TYPES:
        raise ValueError(
            f"doc_type must be one of {_THUMB_DOC_TYPES}, got {doc_type!r}"
        )
    return doc_type


def seed_default_thumbnail_tags(conn: sqlite3.Connection) -> None:
    """Seed the four default thumbnail tag presets on first launch.

    Only fires when the table is empty so renames/deletes stick across
    restarts — same pattern as seed_default_strategies.
    """
    (existing,) = conn.execute(
        "SELECT COUNT(*) FROM thumbnail_tags"
    ).fetchone()
    if existing:
        return
    cur = conn.cursor()
    for idx, name in enumerate(DEFAULT_THUMBNAIL_TAGS):
        cur.execute(
            "INSERT INTO thumbnail_tags (name, sort_order) VALUES (?, ?)",
            (name, idx),
        )
    conn.commit()


def fetch_thumbnail_tags(conn: sqlite3.Connection) -> list[dict]:
    """Return every preset ordered by sort_order, then name."""
    rows = conn.execute(
        """
        SELECT id, name, sort_order
        FROM thumbnail_tags
        ORDER BY sort_order, name COLLATE NOCASE
        """
    ).fetchall()
    return [
        {"id": int(r["id"]),
         "name": str(r["name"]),
         "sort_order": int(r["sort_order"])}
        for r in rows
    ]


def add_thumbnail_tag(conn: sqlite3.Connection, name: str) -> int:
    """Insert a new preset, appended after existing ones. Returns its id.

    Raises ``ValueError`` for empty/whitespace names. A duplicate name
    (case-sensitive, matching the UNIQUE constraint) returns the
    existing row's id rather than raising so the caller can treat
    "Add new tag" idempotently.
    """
    cleaned = (name or "").strip()
    if not cleaned:
        raise ValueError("Thumbnail tag name cannot be empty")
    existing = conn.execute(
        "SELECT id FROM thumbnail_tags WHERE name = ?", (cleaned,),
    ).fetchone()
    if existing is not None:
        return int(existing["id"])
    (max_order,) = conn.execute(
        "SELECT COALESCE(MAX(sort_order), -1) FROM thumbnail_tags"
    ).fetchone()
    cur = conn.execute(
        "INSERT INTO thumbnail_tags (name, sort_order) VALUES (?, ?)",
        (cleaned, int(max_order) + 1),
    )
    conn.commit()
    return int(cur.lastrowid)


def rename_thumbnail_tag(
    conn: sqlite3.Connection, tag_id: int, new_name: str,
) -> bool:
    """Rename a preset in place. Returns False if the target name is
    already taken by another preset (UNIQUE collision) or if tag_id
    doesn't exist. Empty names raise ``ValueError``."""
    cleaned = (new_name or "").strip()
    if not cleaned:
        raise ValueError("Thumbnail tag name cannot be empty")
    clash = conn.execute(
        "SELECT id FROM thumbnail_tags WHERE name = ? AND id <> ?",
        (cleaned, int(tag_id)),
    ).fetchone()
    if clash is not None:
        return False
    cur = conn.execute(
        "UPDATE thumbnail_tags SET name = ? WHERE id = ?",
        (cleaned, int(tag_id)),
    )
    conn.commit()
    return cur.rowcount > 0


def delete_thumbnail_tag(conn: sqlite3.Connection, tag_id: int) -> bool:
    """Delete a preset. Linked assignments cascade away automatically."""
    cur = conn.execute(
        "DELETE FROM thumbnail_tags WHERE id = ?", (int(tag_id),),
    )
    conn.commit()
    return cur.rowcount > 0


def fetch_attachment_tag_ids(
    conn: sqlite3.Connection,
    doc_type: str,
    doc_id: int,
    attachment_id: int,
) -> set[int]:
    """Return the set of preset ids assigned to one image on one doc."""
    _check_doc_type(doc_type)
    rows = conn.execute(
        """
        SELECT tag_id FROM thumbnail_tag_links
        WHERE doc_type = ? AND doc_id = ? AND attachment_id = ?
        """,
        (doc_type, int(doc_id), int(attachment_id)),
    ).fetchall()
    return {int(r["tag_id"]) for r in rows}


def set_attachment_tags(
    conn: sqlite3.Connection,
    doc_type: str,
    doc_id: int,
    attachment_id: int,
    tag_ids: Iterable[int],
) -> None:
    """Replace the tag assignment for one image on one doc with ``tag_ids``.

    No-ops gracefully on invalid tag_ids — the foreign key on
    ``thumbnail_tag_links.tag_id`` enforces that only real preset ids
    land in the table (with PRAGMA foreign_keys = ON, set in connect()).
    """
    _check_doc_type(doc_type)
    new_ids = {int(v) for v in tag_ids}
    cur = conn.cursor()
    cur.execute(
        """
        DELETE FROM thumbnail_tag_links
        WHERE doc_type = ? AND doc_id = ? AND attachment_id = ?
        """,
        (doc_type, int(doc_id), int(attachment_id)),
    )
    for tid in new_ids:
        cur.execute(
            """
            INSERT OR IGNORE INTO thumbnail_tag_links
                (doc_type, doc_id, attachment_id, tag_id)
            VALUES (?, ?, ?, ?)
            """,
            (doc_type, int(doc_id), int(attachment_id), tid),
        )
    conn.commit()


def clear_attachment_tags(
    conn: sqlite3.Connection,
    doc_type: str,
    doc_id: int,
    attachment_id: int,
) -> None:
    """Wipe every tag assignment for one image on one doc.

    Called by the host tabs when an image is unpinned so stale
    assignments don't accumulate after the strip stops showing the
    image.
    """
    _check_doc_type(doc_type)
    conn.execute(
        """
        DELETE FROM thumbnail_tag_links
        WHERE doc_type = ? AND doc_id = ? AND attachment_id = ?
        """,
        (doc_type, int(doc_id), int(attachment_id)),
    )
    conn.commit()


def fetch_doc_thumb_tag_map(
    conn: sqlite3.Connection, doc_type: str, doc_id: int,
) -> dict[int, set[int]]:
    """Return ``{attachment_id: {tag_id, ...}}`` for one doc.

    Single query — the strip uses this to filter without paying per-image
    round-trip cost.
    """
    _check_doc_type(doc_type)
    rows = conn.execute(
        """
        SELECT attachment_id, tag_id FROM thumbnail_tag_links
        WHERE doc_type = ? AND doc_id = ?
        """,
        (doc_type, int(doc_id)),
    ).fetchall()
    out: dict[int, set[int]] = {}
    for r in rows:
        out.setdefault(int(r["attachment_id"]), set()).add(int(r["tag_id"]))
    return out


# --- Phase 11: manual trades ------------------------------------------------

def insert_manual_trade(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    direction: str,
    entry_time: _dt.datetime,
    exit_time: Optional[_dt.datetime],
    avg_entry_price: float,
    avg_exit_price: Optional[float],
    total_shares: int,
    total_commission: float = 0.0,
    stop_loss_price: Optional[float] = None,
) -> int:
    """Insert a hand-recorded trade and return its id.

    P&L and hold duration are computed here so the row is consistent
    with derived trades (which get them from the trade builder). An
    exit-less trade is treated as still-open.
    """
    is_open = exit_time is None or avg_exit_price is None
    gross = None
    net = None
    hold = None
    if not is_open:
        if direction == "Long":
            gross = (avg_exit_price - avg_entry_price) * total_shares
        else:
            gross = (avg_entry_price - avg_exit_price) * total_shares
        net = gross - total_commission
        hold = int((exit_time - entry_time).total_seconds())

    cur = conn.execute(
        """
        INSERT INTO trades (
            symbol, direction, entry_time, exit_time,
            avg_entry_price, avg_exit_price, total_shares,
            total_commission, gross_pnl, net_pnl,
            hold_duration_seconds, is_open, is_manual, stop_loss_price
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
        """,
        (
            symbol, direction, entry_time, exit_time,
            avg_entry_price, avg_exit_price, total_shares,
            total_commission, gross, net, hold,
            1 if is_open else 0, stop_loss_price,
        ),
    )
    conn.commit()
    rebuild_daily_summary(conn)
    return int(cur.lastrowid)


def update_manual_trade(
    conn: sqlite3.Connection,
    trade_id: int,
    *,
    symbol: str,
    direction: str,
    entry_time: _dt.datetime,
    exit_time: Optional[_dt.datetime],
    avg_entry_price: float,
    avg_exit_price: Optional[float],
    total_shares: int,
    total_commission: float = 0.0,
    stop_loss_price: Optional[float] = None,
) -> bool:
    """Update an existing manual trade. No-op (returns False) for
    derived trades — those are owned by the trade builder."""
    row = conn.execute(
        "SELECT is_manual FROM trades WHERE id = ?", (trade_id,),
    ).fetchone()
    if row is None or not row["is_manual"]:
        return False
    is_open = exit_time is None or avg_exit_price is None
    gross = net = hold = None
    if not is_open:
        if direction == "Long":
            gross = (avg_exit_price - avg_entry_price) * total_shares
        else:
            gross = (avg_entry_price - avg_exit_price) * total_shares
        net = gross - total_commission
        hold = int((exit_time - entry_time).total_seconds())

    conn.execute(
        """
        UPDATE trades
           SET symbol = ?, direction = ?, entry_time = ?, exit_time = ?,
               avg_entry_price = ?, avg_exit_price = ?, total_shares = ?,
               total_commission = ?, gross_pnl = ?, net_pnl = ?,
               hold_duration_seconds = ?, is_open = ?, stop_loss_price = ?,
               updated_at = CURRENT_TIMESTAMP
         WHERE id = ?
        """,
        (
            symbol, direction, entry_time, exit_time,
            avg_entry_price, avg_exit_price, total_shares,
            total_commission, gross, net, hold,
            1 if is_open else 0, stop_loss_price, trade_id,
        ),
    )
    conn.commit()
    rebuild_daily_summary(conn)
    return True


def fetch_trade(conn: sqlite3.Connection, trade_id: int) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM trades WHERE id = ?", (trade_id,),
    ).fetchone()
    return dict(row) if row else None


# --- Phase 11: stop-loss for R-multiple ------------------------------------

def set_stop_loss(
    conn: sqlite3.Connection,
    trade_id: int,
    stop_loss_price: Optional[float],
) -> None:
    """Set or clear the planned-stop price on any trade (manual or derived)."""
    conn.execute(
        "UPDATE trades SET stop_loss_price = ?, "
        "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (stop_loss_price, trade_id),
    )
    conn.commit()


def risk_to_stop_price(
    direction: str, avg_entry: float, total_shares: int, risk: float,
) -> Optional[float]:
    """Convert a dollar risk to a stop price using the trade's entry
    and share count. Returns None when the math isn't well-defined.

    Public so the Trades-tab bulk-risk UI can preview / apply stops
    using the same formula the import-time resolver uses.
    """
    if risk <= 0 or total_shares <= 0 or avg_entry <= 0:
        return None
    per_share = risk / total_shares
    if direction == "Short":
        return avg_entry + per_share
    return max(0.0, avg_entry - per_share)


# Backwards-compat alias for the one internal caller.
_risk_to_stop_price = risk_to_stop_price


def apply_import_time_risk(
    conn: sqlite3.Connection,
    hash_to_risk: dict[str, float],
    *,
    default_risk: Optional[float] = None,
) -> int:
    """Resolve each derived trade's planned risk into a stop-loss price.

    Precedence (only trades with NO existing stop are touched):

        1. Explicit risk from ``hash_to_risk`` — looked up via any
           execution's ``import_hash`` that belongs to the trade.
        2. Losing trade → ``risk = abs(net_pnl)`` (every loss becomes
           a clean -1R by construction).
        3. Winning trade + ``default_risk > 0`` → fall back to the
           global default.

    Trades that already have a stop (manually set, or preserved across
    ``rebuild_trades``) are left alone. Open trades (``net_pnl is
    None``) skip rules 2 and 3 but may still get rule 1.

    Returns the number of trades updated.
    """
    # For each derived trade with no stop, collect direction / entry /
    # shares / net_pnl and an "explicit risk" (if any hash in
    # hash_to_risk matches one of its executions).
    cur = conn.cursor()

    # Map import_hash → trade_id for hashes we have risk mappings for.
    hash_to_trade: dict[str, int] = {}
    if hash_to_risk:
        placeholders = ",".join("?" * len(hash_to_risk))
        for r in cur.execute(
            f"""
            SELECT e.import_hash, te.trade_id
            FROM executions e
            JOIN trade_executions te ON te.execution_id = e.id
            JOIN trades t ON t.id = te.trade_id
            WHERE e.import_hash IN ({placeholders})
              AND t.is_manual = 0
              AND t.stop_loss_price IS NULL
            """,
            list(hash_to_risk.keys()),
        ).fetchall():
            hash_to_trade[r["import_hash"]] = int(r["trade_id"])

    # Invert to trade → chosen risk (first-wins if multiple executions
    # for the same trade carry different annotations).
    trade_to_explicit_risk: dict[int, float] = {}
    for h, tid in hash_to_trade.items():
        risk = hash_to_risk.get(h)
        if risk is None or risk <= 0:
            continue
        trade_to_explicit_risk.setdefault(tid, float(risk))

    # Now iterate every derived trade without a stop and resolve.
    applied = 0
    for row in cur.execute(
        """
        SELECT id, direction, avg_entry_price, total_shares, net_pnl
        FROM trades
        WHERE is_manual = 0 AND stop_loss_price IS NULL
        """
    ).fetchall():
        tid = int(row["id"])
        resolved: Optional[float] = trade_to_explicit_risk.get(tid)

        if resolved is None:
            net = row["net_pnl"]
            if net is not None:
                if net < 0:
                    # Losing trade: pin risk to the realized loss so R = -1.
                    resolved = abs(float(net))
                elif default_risk is not None and default_risk > 0:
                    # Winner with no explicit risk — fall back to the
                    # user's saved default if one's set.
                    resolved = float(default_risk)

        if resolved is None or resolved <= 0:
            continue
        stop = _risk_to_stop_price(
            row["direction"],
            float(row["avg_entry_price"] or 0.0),
            int(row["total_shares"] or 0),
            resolved,
        )
        if stop is None:
            continue
        cur.execute(
            "UPDATE trades SET stop_loss_price = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (stop, tid),
        )
        applied += 1
    conn.commit()
    return applied


# --- Phase 11: recycle bin -------------------------------------------------

def _trade_to_snapshot_dict(row: dict) -> dict:
    """Convert a trade row into a JSON-serialisable dict."""
    out: dict = {}
    for k, v in row.items():
        if isinstance(v, _dt.datetime):
            out[k] = v.isoformat(sep=" ")
        elif isinstance(v, _dt.date):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


def _execution_to_snapshot_dict(row: dict) -> dict:
    return _trade_to_snapshot_dict(row)


def _build_trade_snapshot(
    conn: sqlite3.Connection, trade_id: int,
) -> Optional[dict]:
    """Capture everything needed to fully restore a trade later.

    Returns ``{"trade": {...}, "executions": [...], "journal": {...}|None,
    "tag_ids": [...]}`` or None if the trade doesn't exist.
    """
    trade_row = conn.execute(
        "SELECT * FROM trades WHERE id = ?", (trade_id,),
    ).fetchone()
    if trade_row is None:
        return None
    trade = _trade_to_snapshot_dict(dict(trade_row))

    # Linked executions (only meaningful for derived trades).
    exec_rows = conn.execute(
        """
        SELECT e.* FROM executions e
        JOIN trade_executions te ON te.execution_id = e.id
        WHERE te.trade_id = ?
        ORDER BY e.entered_at, e.id
        """,
        (trade_id,),
    ).fetchall()
    executions = [_execution_to_snapshot_dict(dict(r)) for r in exec_rows]

    # Journal entry (if any).
    j_row = conn.execute(
        "SELECT notes, created_at FROM journal_entries WHERE trade_id = ?",
        (trade_id,),
    ).fetchone()
    journal = (
        {"notes": j_row["notes"],
         "created_at": (
            j_row["created_at"].isoformat(sep=" ")
            if isinstance(j_row["created_at"], _dt.datetime)
            else j_row["created_at"]
         )}
        if j_row is not None else None
    )

    # Tag ids only — restoring uses ids; orphaned ids (tag deleted in
    # the meantime) are silently skipped on restore.
    tag_ids = [
        int(r["tag_id"]) for r in conn.execute(
            "SELECT tag_id FROM trade_tags WHERE trade_id = ?", (trade_id,),
        ).fetchall()
    ]

    return {
        "trade": trade,
        "executions": executions,
        "journal": journal,
        "tag_ids": tag_ids,
    }


def _build_trade_snapshots_bulk(
    conn: sqlite3.Connection, trade_ids: list[int],
) -> dict[int, dict]:
    """Batched analogue of ``_build_trade_snapshot`` for many ids at once.

    Issues a fixed four SELECTs (trades, executions+join, journals, tags)
    regardless of input length and joins them in memory, instead of the
    per-id ``_build_trade_snapshot`` loop which did 4×N queries. Used by
    ``soft_delete_trades`` so bulk-recycling stays responsive on
    selections of hundreds of rows.

    Returns ``{trade_id: snapshot}`` and silently omits ids whose trade
    row no longer exists (matches the per-id helper's None return).
    """
    if not trade_ids:
        return {}
    ids = [int(t) for t in trade_ids]
    placeholders = ",".join("?" * len(ids))

    trade_rows = conn.execute(
        f"SELECT * FROM trades WHERE id IN ({placeholders})", ids,
    ).fetchall()
    if not trade_rows:
        return {}

    exec_rows = conn.execute(
        f"""
        SELECT te.trade_id AS _tid, e.*
        FROM executions e
        JOIN trade_executions te ON te.execution_id = e.id
        WHERE te.trade_id IN ({placeholders})
        ORDER BY te.trade_id, e.entered_at, e.id
        """,
        ids,
    ).fetchall()
    execs_by_trade: dict[int, list[dict]] = {}
    for r in exec_rows:
        d = dict(r)
        tid = int(d.pop("_tid"))
        execs_by_trade.setdefault(tid, []).append(
            _execution_to_snapshot_dict(d)
        )

    j_rows = conn.execute(
        f"""
        SELECT trade_id, notes, created_at
        FROM journal_entries
        WHERE trade_id IN ({placeholders})
        """,
        ids,
    ).fetchall()
    journals: dict[int, dict] = {}
    for r in j_rows:
        created = r["created_at"]
        journals[int(r["trade_id"])] = {
            "notes": r["notes"],
            "created_at": (
                created.isoformat(sep=" ")
                if isinstance(created, _dt.datetime) else created
            ),
        }

    tag_rows = conn.execute(
        f"""
        SELECT trade_id, tag_id FROM trade_tags
        WHERE trade_id IN ({placeholders})
        """,
        ids,
    ).fetchall()
    tags_by_trade: dict[int, list[int]] = {}
    for r in tag_rows:
        tags_by_trade.setdefault(
            int(r["trade_id"]), [],
        ).append(int(r["tag_id"]))

    out: dict[int, dict] = {}
    for tr in trade_rows:
        tid = int(tr["id"])
        out[tid] = {
            "trade": _trade_to_snapshot_dict(dict(tr)),
            "executions": execs_by_trade.get(tid, []),
            "journal": journals.get(tid),
            "tag_ids": tags_by_trade.get(tid, []),
        }
    return out


def soft_delete_trade(
    conn: sqlite3.Connection, trade_id: int,
) -> bool:
    """Move a trade into the recycle bin and hard-delete it.

    The full snapshot (trade + executions + journal + tags) is preserved
    so ``restore_deleted_trade`` can fully reconstruct the row. Returns
    True if a trade was actually removed.
    """
    snapshot = _build_trade_snapshot(conn, trade_id)
    if snapshot is None:
        return False
    t = snapshot["trade"]
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO deleted_trades
            (symbol, direction, entry_time, net_pnl, is_manual, snapshot_json)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            t.get("symbol"), t.get("direction"), t.get("entry_time"),
            t.get("net_pnl"), 1 if t.get("is_manual") else 0,
            json.dumps(snapshot),
        ),
    )
    conn.commit()
    # Delegates to the existing hard-delete path so cascades and
    # rebuild_trades / rebuild_daily_summary fire correctly.
    return delete_trade(conn, trade_id)


def soft_delete_trades(
    conn: sqlite3.Connection, trade_ids: list[int],
) -> int:
    """Bulk-soft-delete; rebuilds run once at the end.

    Snapshots are gathered in a fixed four queries via
    ``_build_trade_snapshots_bulk`` instead of one per id — bulk-recycle
    of 500 rows used to fire 2000+ SELECTs and freeze the UI thread.
    """
    if not trade_ids:
        return 0
    snapshots = _build_trade_snapshots_bulk(conn, [int(t) for t in trade_ids])
    if not snapshots:
        return 0
    cur = conn.cursor()
    for tid in trade_ids:
        snap = snapshots.get(int(tid))
        if snap is None:
            continue
        t = snap["trade"]
        cur.execute(
            """
            INSERT INTO deleted_trades
                (symbol, direction, entry_time, net_pnl, is_manual,
                 snapshot_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                t.get("symbol"), t.get("direction"), t.get("entry_time"),
                t.get("net_pnl"), 1 if t.get("is_manual") else 0,
                json.dumps(snap),
            ),
        )
    conn.commit()
    # The existing bulk hard-delete handles cascade + a single rebuild.
    return delete_trades(conn, [int(t) for t in trade_ids])


def fetch_deleted_trades(conn: sqlite3.Connection) -> list[dict]:
    """List recycle-bin entries, newest deletion first."""
    return [
        dict(r) for r in conn.execute(
            """
            SELECT id, deleted_at, symbol, direction, entry_time,
                   net_pnl, is_manual
            FROM deleted_trades
            ORDER BY deleted_at DESC, id DESC
            """
        ).fetchall()
    ]


def _parse_iso_dt(s: Optional[str]) -> Optional[_dt.datetime]:
    if not s:
        return None
    return _dt.datetime.fromisoformat(s.replace(" ", "T", 1) if " " in s else s)


class CorruptSnapshotError(Exception):
    """Raised when a recycle-bin row's ``snapshot_json`` won't parse.

    Carries the offending ``deleted_id`` so the UI can offer to purge
    the row in place of restoring it. A corrupt snapshot is unrecoverable
    — the soft-delete write was likely interrupted (crash, full disk) —
    but we want a clean signal to the dialog instead of a raw
    ``json.JSONDecodeError`` propagating out and crashing the app.
    """
    def __init__(self, deleted_id: int, original: Exception):
        super().__init__(
            f"Snapshot for recycle-bin row {deleted_id} is corrupt: "
            f"{type(original).__name__}: {original}"
        )
        self.deleted_id = int(deleted_id)
        self.original = original


def restore_deleted_trade(
    conn: sqlite3.Connection, deleted_id: int,
) -> Optional[int]:
    """Re-create a previously soft-deleted trade from its snapshot.

    Returns the new trade id, or None if the recycle-bin row is gone.
    Tags that no longer exist are skipped silently. Raises
    ``CorruptSnapshotError`` if the stored JSON won't parse — callers
    should offer the user a purge option in that case.
    """
    row = conn.execute(
        "SELECT snapshot_json FROM deleted_trades WHERE id = ?",
        (deleted_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        snapshot = json.loads(row["snapshot_json"])
        if not isinstance(snapshot, dict):
            raise ValueError("snapshot root must be a JSON object")
    except (ValueError, TypeError) as e:
        raise CorruptSnapshotError(deleted_id, e) from e
    t = snapshot.get("trade") or {}
    executions = snapshot.get("executions") or []
    journal = snapshot.get("journal")
    tag_ids = snapshot.get("tag_ids") or []

    cur = conn.cursor()

    # 1) Re-create the trade row (fresh id; preserve everything else).
    cur.execute(
        """
        INSERT INTO trades (
            symbol, direction, entry_time, exit_time,
            avg_entry_price, avg_exit_price, total_shares,
            total_commission, gross_pnl, net_pnl,
            hold_duration_seconds, is_open, is_manual, stop_loss_price,
            created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        (
            t.get("symbol"), t.get("direction"),
            _parse_iso_dt(t.get("entry_time")),
            _parse_iso_dt(t.get("exit_time")),
            t.get("avg_entry_price"), t.get("avg_exit_price"),
            t.get("total_shares"), t.get("total_commission") or 0.0,
            t.get("gross_pnl"), t.get("net_pnl"),
            t.get("hold_duration_seconds"),
            1 if t.get("is_open") else 0,
            1 if t.get("is_manual") else 0,
            t.get("stop_loss_price"),
        ),
    )
    new_trade_id = int(cur.lastrowid)

    # 2) Re-create executions (derived trades only). import_hash unique
    #    constraint is fine — these were deleted, so there's no clash.
    for ex in executions:
        try:
            cur.execute(
                """
                INSERT INTO executions (
                    entered_at, type, symbol, raw_symbol, symbol_extension,
                    extension_type, order_status, stop_price, limit_price,
                    filled_price, quantity, filled_canceled_at, qty_filled,
                    qty_left, commission, source_file, import_hash
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _parse_iso_dt(ex.get("entered_at")),
                    ex.get("type"), ex.get("symbol"), ex.get("raw_symbol"),
                    ex.get("symbol_extension"), ex.get("extension_type"),
                    ex.get("order_status"), ex.get("stop_price"),
                    ex.get("limit_price"), ex.get("filled_price"),
                    ex.get("quantity"),
                    _parse_iso_dt(ex.get("filled_canceled_at")),
                    ex.get("qty_filled"), ex.get("qty_left"),
                    ex.get("commission") or 0.0, ex.get("source_file"),
                    ex.get("import_hash"),
                ),
            )
            new_exec_id = int(cur.lastrowid)
            cur.execute(
                "INSERT INTO trade_executions (trade_id, execution_id) "
                "VALUES (?, ?)",
                (new_trade_id, new_exec_id),
            )
        except sqlite3.IntegrityError:
            # Duplicate import_hash — execution still exists somewhere
            # else. Just relink it.
            existing = cur.execute(
                "SELECT id FROM executions WHERE import_hash = ?",
                (ex.get("import_hash"),),
            ).fetchone()
            if existing is not None:
                cur.execute(
                    "INSERT OR IGNORE INTO trade_executions "
                    "(trade_id, execution_id) VALUES (?, ?)",
                    (new_trade_id, int(existing["id"])),
                )

    # 3) Restore journal entry.
    if journal and journal.get("notes"):
        cur.execute(
            """
            INSERT INTO journal_entries (trade_id, notes, created_at)
            VALUES (?, ?, ?)
            """,
            (
                new_trade_id, journal["notes"],
                _parse_iso_dt(journal.get("created_at"))
                or _dt.datetime.now(),
            ),
        )

    # 4) Re-link tags (skip ones that no longer exist).
    for tag_id in tag_ids:
        cur.execute(
            "INSERT OR IGNORE INTO trade_tags (trade_id, tag_id) "
            "SELECT ?, id FROM tags WHERE id = ?",
            (new_trade_id, int(tag_id)),
        )

    cur.execute("DELETE FROM deleted_trades WHERE id = ?", (deleted_id,))
    conn.commit()

    # If the restored trade is derived AND any of its executions
    # already existed (re-imported between soft-delete and restore),
    # we now have a transient dual-ownership state — both the new
    # restored trade row and any existing rebuilt trade are linked to
    # the same executions. Run rebuild_trades to consolidate: it
    # drops every derived trade and re-builds one per logical group,
    # with the journal + tags + stop_loss snapshot path preserving
    # whatever we just attached. After the rebuild the user sees a
    # single canonical trade with the restored journal/tags.
    final_id = new_trade_id
    if not t.get("is_manual"):
        rebuild_trades(conn)
        # Look up the rebuilt trade — its (symbol, entry_time,
        # direction) identity matches what we just restored, so we
        # can hand back the new canonical id.
        row = conn.execute(
            """
            SELECT id FROM trades
            WHERE symbol = ? AND entry_time = ? AND direction = ?
              AND is_manual = 0
            LIMIT 1
            """,
            (
                t.get("symbol"),
                _parse_iso_dt(t.get("entry_time")),
                t.get("direction"),
            ),
        ).fetchone()
        if row is not None:
            final_id = int(row["id"])

    rebuild_daily_summary(conn)
    return final_id


def purge_deleted_trade(conn: sqlite3.Connection, deleted_id: int) -> None:
    conn.execute("DELETE FROM deleted_trades WHERE id = ?", (deleted_id,))
    conn.commit()


def purge_old_deleted_trades(
    conn: sqlite3.Connection, *, max_age_days: int = 30,
) -> int:
    """Delete recycle-bin entries older than ``max_age_days``.

    Called from app startup so the bin doesn't grow without bound.
    """
    cur = conn.execute(
        "DELETE FROM deleted_trades "
        "WHERE deleted_at < datetime('now', ?)",
        (f"-{int(max_age_days)} days",),
    )
    conn.commit()
    return cur.rowcount
