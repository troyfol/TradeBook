"""Brief generation — aggregate journal entries across a filtered set
of trades into a single editable HTML document.

A *brief* is a user-curated document that consolidates trading notes for a
date range, tags, and/or tickers. The generator:

    1. Filters closed trades by (date range, tags, symbols, has-journal)
    2. Fetches each matching trade's journal HTML
    3. Strips images out (or keeps them, per ``include_images``)
    4. Emits a single HTML document with a per-trade section header
    5. Returns (title, html) — the title follows a filename-safe pattern
       derived from the filter spec

The HTML it produces uses the same ``attachment:N`` URL scheme as
``journal_entries``, so the Briefs tab can render and edit it with the
existing ``JournalEditor`` widget.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from html import escape as _html_escape
from html.parser import HTMLParser
from typing import Optional

from ingest import db_manager


# Inclusive date bounds used as "no filter set" sentinels (mirrors
# JournalTab's min/max dates).
_UNBOUNDED_FROM = date(1990, 1, 1)
_UNBOUNDED_TO = date(2100, 12, 31)


@dataclass
class BriefFilters:
    """Snapshot of the Journal tab's filter bar at brief-generation time."""
    date_from: Optional[date] = None
    date_to: Optional[date] = None
    symbols: list[str] = field(default_factory=list)  # uppercased
    tag_ids: list[int] = field(default_factory=list)
    tag_names: list[str] = field(default_factory=list)  # human-readable
    search_text: str = ""
    include_images: bool = False

    def has_date_range(self) -> bool:
        if self.date_from is None and self.date_to is None:
            return False
        lo = self.date_from or _UNBOUNDED_FROM
        hi = self.date_to or _UNBOUNDED_TO
        return lo > _UNBOUNDED_FROM or hi < _UNBOUNDED_TO

    def effective_from(self) -> Optional[date]:
        return (
            self.date_from
            if self.date_from and self.date_from > _UNBOUNDED_FROM
            else None
        )

    def effective_to(self) -> Optional[date]:
        return (
            self.date_to
            if self.date_to and self.date_to < _UNBOUNDED_TO
            else None
        )


# ---- title generation ------------------------------------------------------


_FILENAME_UNSAFE = re.compile(r"[<>:\"/\\|?*\x00-\x1f]")


def _sanitize_title_component(s: str) -> str:
    """Strip chars that would be illegal in a filename, collapse spaces."""
    s = _FILENAME_UNSAFE.sub("", s or "").strip()
    s = re.sub(r"\s+", "_", s)
    return s[:64]  # cap each component


def build_title(filters: BriefFilters) -> str:
    """Generate a filename-safe title from the filter spec.

    Format: ``{date_or_range}_{sorted_filter_tokens}``

    Single-day range: ``2026-04-11``
    Multi-day range:  ``2026-04-01_to_2026-04-11``
    Unbounded:        ``all_dates``

    Filter tokens (tags + tickers) are sorted alphabetically and joined
    by underscores. With no filters, only the date component appears.
    """
    lo = filters.effective_from()
    hi = filters.effective_to()

    if lo is None and hi is None:
        date_part = "all_dates"
    elif lo is not None and hi is not None:
        if lo == hi:
            date_part = lo.isoformat()
        else:
            date_part = f"{lo.isoformat()}_to_{hi.isoformat()}"
    elif lo is not None:
        date_part = f"from_{lo.isoformat()}"
    else:  # hi only
        date_part = f"through_{hi.isoformat()}"

    tokens = []
    for s in filters.symbols:
        tokens.append(_sanitize_title_component(s.upper()))
    for name in filters.tag_names:
        tokens.append(_sanitize_title_component(name))
    # Case-insensitive alphabetical sort so "MSFT" and "Mistake" order by
    # letter rather than by ASCII byte (uppercase vs lowercase).
    unique = sorted({t for t in tokens if t}, key=lambda s: s.lower())

    if unique:
        return f"{date_part}_{'_'.join(unique)}"
    return date_part


# ---- HTML post-processing --------------------------------------------------


class _ImageStripper(HTMLParser):
    """Drop all ``<img>`` tags while preserving everything else.

    Paragraphs that held only an image become empty and are trimmed out
    during the final compaction pass in ``_compact_html``.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._out: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "img":
            return
        attr_str = "".join(
            f' {name}="{_html_escape(val or "", quote=True)}"'
            for name, val in attrs
        )
        self._out.append(f"<{tag}{attr_str}>")

    def handle_startendtag(self, tag: str, attrs) -> None:
        if tag == "img":
            return
        attr_str = "".join(
            f' {name}="{_html_escape(val or "", quote=True)}"'
            for name, val in attrs
        )
        self._out.append(f"<{tag}{attr_str} />")

    def handle_endtag(self, tag: str) -> None:
        if tag == "img":
            return
        self._out.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        self._out.append(data)

    def handle_entityref(self, name: str) -> None:
        self._out.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self._out.append(f"&#{name};")

    def result(self) -> str:
        return "".join(self._out)


def strip_images_from_html(html: str) -> str:
    """Return ``html`` with every ``<img>`` element removed."""
    if not html or "<img" not in html.lower():
        return html or ""
    p = _ImageStripper()
    p.feed(html)
    p.close()
    return p.result()


def _extract_body_inner(html: str) -> str:
    """Return the inner HTML of ``<body>`` if present, else the input.

    Qt's ``QTextDocument.toHtml()`` wraps content in full
    ``<html><head>…</head><body>…</body></html>``. For concatenation we
    only want the body.
    """
    if not html:
        return ""
    m = re.search(
        r"<body[^>]*>(.*?)</body>",
        html,
        re.IGNORECASE | re.DOTALL,
    )
    if m:
        return m.group(1).strip()
    return html.strip()


# ---- main entry point ------------------------------------------------------


def _format_pnl(v: Optional[float]) -> str:
    if v is None:
        return "—"
    sign = "-" if v < 0 else "+"
    return f"{sign}${abs(v):,.2f}"


def _format_entry_time(t: dict) -> str:
    et = t.get("entry_time")
    if isinstance(et, datetime):
        return et.strftime("%Y-%m-%d %H:%M")
    return ""


def _build_trade_header(t: dict) -> str:
    """Build the per-trade section header — e.g.
    ``RAYA — Long — 2026-04-11 09:42 — Net P&L: +$14.14``."""
    parts = [
        _html_escape(t.get("symbol") or "?"),
        _html_escape(t.get("direction") or ""),
        _html_escape(_format_entry_time(t) or ""),
        f"Net P&amp;L: {_html_escape(_format_pnl(t.get('net_pnl')))}",
    ]
    header_text = " — ".join(p for p in parts if p)
    return f"<h3>{header_text}</h3>"


def _matches_filters(
    t: dict,
    filters: BriefFilters,
    journal_ids: set[int],
    matching_ids: Optional[set[int]],
) -> bool:
    if t["id"] not in journal_ids:
        return False
    if matching_ids is not None and t["id"] not in matching_ids:
        return False
    lo = filters.effective_from()
    hi = filters.effective_to()
    if lo is not None or hi is not None:
        et = t.get("exit_time") or t.get("entry_time")
        d = et.date() if isinstance(et, datetime) else et
        if d is None:
            return False
        if lo and d < lo:
            return False
        if hi and d > hi:
            return False
    if filters.symbols:
        sym_set = {s.upper() for s in filters.symbols}
        if (t.get("symbol") or "").upper() not in sym_set:
            return False
    return True


def generate_brief(
    conn: sqlite3.Connection,
    filters: BriefFilters,
) -> tuple[str, str, list[dict]]:
    """Build a brief document for the given filter snapshot.

    Returns ``(title, html, trades)`` — the derived title, the full brief
    HTML, and the list of trade dicts that were included (in chronological
    entry-time order). If no trades match, ``html`` is a friendly
    "no entries" notice so the brief is never empty.
    """
    journal_ids = db_manager.trade_ids_with_journal(conn)

    matching_ids: Optional[set[int]] = None

    def _intersect(s: set[int]) -> None:
        nonlocal matching_ids
        matching_ids = set(s) if matching_ids is None else (matching_ids & s)

    if filters.search_text.strip():
        _intersect(db_manager.search_journal_entries(
            conn, filters.search_text.strip(),
        ))
    if filters.tag_ids:
        _intersect(db_manager.trade_ids_with_any_tag(conn, filters.tag_ids))

    all_trades = db_manager.fetch_trades_for_display(conn)
    trades = [
        t for t in all_trades
        if _matches_filters(t, filters, journal_ids, matching_ids)
    ]
    # Chronological: oldest first so the brief reads top-to-bottom.
    trades.sort(key=lambda t: (t.get("entry_time") or datetime.min, t["id"]))

    title = build_title(filters)

    if not trades:
        html = (
            f"<h1>{_html_escape(title)}</h1>"
            "<p><em>No journal entries match the current filters.</em></p>"
        )
        return title, html, []

    sections: list[str] = [f"<h1>{_html_escape(title)}</h1>"]
    rendered: list[dict] = []
    for t in trades:
        entry_html = db_manager.fetch_journal_entry(conn, t["id"]) or ""
        body = _extract_body_inner(entry_html)
        if not filters.include_images:
            body = strip_images_from_html(body)
        if not body.strip():
            continue
        sections.append(_build_trade_header(t))
        sections.append(body)
        sections.append("<hr />")
        rendered.append(t)

    # When every matching trade stripped to empty (typical with
    # ``include_images=False`` on image-only entries), fall through to
    # the same notice we use for "no matches at all" rather than ship a
    # brief that is just a title. Keeps the returned ``trades`` list in
    # sync with what's actually in the HTML.
    if not rendered:
        html = (
            f"<h1>{_html_escape(title)}</h1>"
            "<p><em>No journal entries match the current filters.</em></p>"
        )
        return title, html, []

    # Remove the last <hr /> — it's a separator between sections, not a
    # terminator.
    if sections and sections[-1] == "<hr />":
        sections.pop()

    return title, "\n".join(sections), rendered
