"""Per-tag P&L roll-up.

Joins closed trades to their assigned tags and sums net P&L per tag
name. A trade with multiple tags contributes its full P&L to *each*
of its tags — that's how Tradervue / similar tools compute the same
view; the goal is to answer "how do trades with this tag perform"
not "split P&L across tags".

Tags with zero matching trades are dropped from the result.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from typing import Iterable, Optional


def tag_breakdown_rows(
    conn: sqlite3.Connection,
    trades: Iterable[dict],
) -> list[tuple[str, float]]:
    """Return ``[(tag_name, total_net_pnl), ...]`` sorted by absolute
    P&L descending, then tag name ascending."""
    trade_ids = [int(t["id"]) for t in trades if t.get("id") is not None]
    if not trade_ids:
        return []

    placeholders = ",".join("?" * len(trade_ids))
    rows = conn.execute(
        f"""
        SELECT tg.name AS name, t.net_pnl AS net_pnl
        FROM trades t
        JOIN trade_tags tt ON tt.trade_id = t.id
        JOIN tags tg ON tg.id = tt.tag_id
        WHERE t.id IN ({placeholders})
          AND t.net_pnl IS NOT NULL
        """,
        trade_ids,
    ).fetchall()

    sums: dict[str, float] = defaultdict(float)
    for r in rows:
        try:
            sums[r["name"]] += float(r["net_pnl"])
        except (TypeError, ValueError):
            continue

    return sorted(
        sums.items(),
        key=lambda kv: (-abs(kv[1]), kv[0].lower()),
    )
