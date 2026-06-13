"""Central registry of QSettings keys used across the app.

Why this exists: keys were previously declared as module-local constants
in each tab and dialog, with no single place to audit them. Centralising
them here means:

    * One file to scan for namespace collisions ("am I about to reuse
      `dashboard/foo` for two different things?")
    * Renaming a key updates every reader at once
    * New developers can see the entire persisted-preference surface

Convention: namespace keys with `<area>/<setting>` so they group cleanly
under the QSettings INI file.

Dialog geometry keys live on the dialog classes themselves
(`GEOMETRY_KEY` class attribute) — see `gui/dialogs/_geometry.py`.
"""
from __future__ import annotations


# ---- Main window -----------------------------------------------------------

MAIN_WINDOW_GEOMETRY = "main_window/geometry"
MAIN_WINDOW_STATE = "main_window/state"

# Zoom levels for the two zoomable tables.
ZOOM_TRADES = "zoom/trades"
ZOOM_NEW_TRADE_PREVIEW = "zoom/new_trade_preview"


# ---- Dashboard -------------------------------------------------------------

DASHBOARD_CARD_ORDER = "dashboard/stat_card_order"
DASHBOARD_CHART_ORDER = "dashboard/chart_card_order"
# Phase 14: full layout state per chart card — JSON blob with order
# + per-card size class + per-card minimized flag. Supersedes
# DASHBOARD_CHART_ORDER (which is migrated forward on first load).
DASHBOARD_CHART_LAYOUT = "dashboard/chart_layout_v1"
# Phase 14b: per-user color palette applied to every dashboard chart.
# JSON blob with keys: positive, negative, axis, label, background.
DASHBOARD_CHART_PALETTE = "dashboard/chart_palette_v1"
# Phase 14c: named layout presets. JSON list of
#   [{"name": str, "items": [{"key", "x", "y", "w", "h", "minimized"}]}]
DASHBOARD_CHART_PRESETS = "dashboard/chart_presets_v1"
# Comma-separated set of chart-card keys the user has *seen* at least
# once. Used so that adding a new card to the registry surfaces it
# automatically on next launch — but only that one time. After the
# first load the key joins this set, so a subsequent removal via
# Configure Charts sticks across launches.
DASHBOARD_CHART_KEYS_SEEN = "dashboard/chart_keys_seen"

# DateRangeBar (lives on Dashboard but stored under dashboard/* by convention).
DASHBOARD_DATE_PRESET = "dashboard/date_preset"
DASHBOARD_DATE_FROM = "dashboard/date_from"
DASHBOARD_DATE_TO = "dashboard/date_to"


# ---- Calendar --------------------------------------------------------------

CALENDAR_YEAR = "calendar/year"
CALENDAR_MONTH = "calendar/month"
CALENDAR_CELL_MODE = "calendar/cell_mode"


# ---- Reports ---------------------------------------------------------------

REPORTS_ACTIVE_SUBTAB = "reports/active_subtab"
REPORTS_PRICE_EDGES = "reports/price_edges"
REPORTS_HOLD_EDGES_MINUTES = "reports/hold_edges_minutes"


# ---- Journal ---------------------------------------------------------------

JOURNAL_SPLITTER = "journal/splitter"
JOURNAL_LAST_TRADE_ID = "journal/last_trade_id"
JOURNAL_HAS_JOURNAL_ONLY = "journal/has_journal_only"


# ---- Briefs ----------------------------------------------------------------

BRIEFS_SPLITTER = "briefs/splitter"
BRIEFS_LAST_ID = "briefs/last_id"
BRIEFS_LAST_EXPORT_DIR = "briefs/last_export_dir"
# Outline pane + image thumbnail strip toggles. Mirror the equivalent
# Strategies-tab keys so the two surfaces feel identical.
BRIEFS_SHOW_OUTLINE = "briefs/show_outline"
BRIEFS_SHOW_THUMBS = "briefs/show_thumbs"
# Index into ``strategy_navigator.THUMB_SIZES`` for the currently
# selected thumbnail size. Persisted independently from the Strategies
# tab so the two surfaces can be tuned separately.
BRIEFS_THUMB_SIZE_INDEX = "briefs/thumb_size_index"


# ---- Strategies ------------------------------------------------------------

STRATEGIES_SPLITTER = "strategies/splitter"
STRATEGIES_LAST_ID = "strategies/last_id"
STRATEGIES_LAST_EXPORT_DIR = "strategies/last_export_dir"
# Outline / thumbnail-strip / collapsible-section visibility flags.
STRATEGIES_SHOW_OUTLINE = "strategies/show_outline"
STRATEGIES_SHOW_THUMBS = "strategies/show_thumbs"
STRATEGIES_FOLDED_BLOCKS = "strategies/folded_blocks"
# Index into ``strategy_navigator.THUMB_SIZES`` for the currently
# selected thumbnail size on the Strategies tab.
STRATEGIES_THUMB_SIZE_INDEX = "strategies/thumb_size_index"


# ---- Rich-text editor defaults --------------------------------------------
# Applied to any empty journal entry or brief so the user lands at their
# preferred size/weight/color/etc. when they start typing. Populated via
# the "Set as default" button on the formatting toolbar.

EDITOR_DEFAULT_SIZE = "editor/default_size"
EDITOR_DEFAULT_COLOR = "editor/default_color"
EDITOR_DEFAULT_BOLD = "editor/default_bold"
EDITOR_DEFAULT_ITALIC = "editor/default_italic"
EDITOR_DEFAULT_UNDERLINE = "editor/default_underline"


# ---- P&L goals -------------------------------------------------------------

GOAL_DAILY = "goals/daily_pnl"
GOAL_WEEKLY = "goals/weekly_pnl"
GOAL_MONTHLY = "goals/monthly_pnl"
GOAL_YEARLY = "goals/yearly_pnl"


# ---- R-multiple ------------------------------------------------------------
# Default dollar amount treated as 1R for trades that don't have a
# recorded stop-loss price. 0 = disabled (those trades are excluded
# from R reports). Used by analytics.r_multiple.trade_r.

RMULTIPLE_DEFAULT_RISK = "r_multiple/default_risk_dollars"
