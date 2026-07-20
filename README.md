# TradeBook

A local-first desktop trade journal and analytics platform for active traders. Built with PySide6 (Qt 6), SQLite, and pyqtgraph as a self-hosted replacement for [Tradervue](https://www.tradervue.com).

Paste your TradeStation order exports, and TradeBook automatically groups executions into logical trades, tracks performance metrics, and gives you a full journaling + strategy-playbook workflow — all offline, all on your machine.

![Python 3.13](https://img.shields.io/badge/python-3.13-blue)
![PySide6 6.11](https://img.shields.io/badge/PySide6-6.11-green)
![SQLite](https://img.shields.io/badge/database-SQLite-lightgrey)
![License](https://img.shields.io/badge/license-MIT-green)

---

## Features

### Data Import

- **Paste-to-import**: paste TradeStation order exports directly into the app (tab-delimited)
- **Smart dedup**: import-hash prevents duplicate executions on re-paste
- **Trade builder**: state machine groups fills into logical trades (Long/Short), computing VWAP entry/exit prices, gross/net P&L, and hold duration. Resilient — a single bad fill is reported as an error and the state machine resets, preserving completed trades that came before it
- **Partial closes (scale-outs)**: when you exit only part of a position (e.g. sell 600 of 1,140 shares), the trade stays a single **open** position — it isn't counted as a closed trade or added to any stat until the whole position is flat — but it now tracks the realized P&L on the closed slice so the process is visible. Open Positions shows the remaining shares + capital, and the Trades row shows the closed-slice realized P&L (marked provisional). When the remainder finally closes, it collapses into one closed trade over the full share count
- **Symbol parsing**: handles TradeStation extensions (`.A`, `.B`, `.D`, `.F`, `.P`, `.Q`, `.W`) and parenthetical tags (`(HB)`, `(SHO)`, etc.)
- **Pre-import backup**: automatic SQLite snapshot before every import so bad pastes can be rolled back
- **Inline preview editing**: parse a paste, fix typos directly on NEW rows in the preview table before importing — Delete key removes selected rows, right-click "Remove from preview" works on multi-selects
- **Bulk apply risk**: assign a planned risk-per-trade across imported fills in one step (drives R-multiple analytics later)

### Dashboard

A free-positioning canvas of draggable, resizable, minimizable chart cards. Drop them anywhere; positions persist as absolute pixels per layout preset.

- **14 chart cards** — equity curve, daily P&L bars, winning vs losing donut, hold-time W/L, avg W/L, largest gain/loss gauge, day-of-week, intraday-vs-multiday duration, price-bucket performance, hour-of-day, total fees, profit-factor gauge, tag breakdown, **open positions index** (count + capital deployed + per-symbol breakdown — independent of the date filter; for a partially-closed position it shows the **remaining** shares + capital and the realized P&L on the closed slice, not the full entry size)
- **12 configurable stat cards** — total trades, win rate, profit factor, expectancy, avg/max winner/loser, total P&L, avg/max/most-profitable hold time. Drag to reorder, check to show
- **Configurable per-card palette**: right-click a card → **Customize chart colors** to override positive / negative / axis / label / background. Per-card overrides persist across launches and sit on top of the global palette
- **Layout presets**: snapshot the canvas as a named preset, switch between presets in one click. Right-click a preset for rename / overwrite / delete
- **Date range filter**: All / YTD / Month / Week / Today presets plus custom date pickers
- **Goal tracking**: set daily / weekly / monthly / yearly P&L targets — progress bars on the dashboard show how today / this week / this month is tracking against the goal
- **Recent week strip**: collapsible row of the last 7 weekday cells with per-day P&L and trade count
- **Forward-migration**: when a new release adds a chart card it appears once on existing dashboards, then respects later removal — fresh cards never get silently buried, but explicit removals stick

### Calendar

- **Mon-Fri heatmap**: color-coded daily cells (green = profit, red = loss, intensity = magnitude, normalized per visible month)
- **3 display modes**: P&L only, P&L + trade count, P&L + win/loss split
- **Weekly totals**: aggregated in a 6th column to the right of each row
- **Month / year navigation**: arrow buttons + dropdowns; year combo auto-spans your trade history ± 5 years
- **Day drilldown**: double-click any cell to jump to the Trades tab filtered to that day; single-click populates a side panel with that day's trades

### Trades

- **Sortable table**: 11 columns (date, symbol, direction, entry/exit price, shares, gross/net P&L, commission, hold time, plus a stop-loss column). Open trades pin to the bottom regardless of sort order. A partially-closed open trade shows its remaining shares as `540 (of 1,140)` and displays the closed slice's realized P&L in the P&L cells, marked provisional (`~$2.85*`, muted tone, with a hover tooltip) until the position fully closes
- **Full filter bar**: date range presets, custom dates, multi-select symbol, direction, win/loss, hold-time bounds — defaults to showing all trades
- **Multi-select actions**: right-click to Hide, Delete (with confirmation), Edit (manual trades), Set / Clear stop loss; bulk "Delete all visible"
- **Soft delete + recycle bin**: deletes go to a recycle bin (`File → Recycle bin…`) for 30 days; restore brings the trade back with its journal entry, tags, and stop loss intact
- **Manual trade entry**: hand-record trades via `+ New manual trade` for fills that didn't come through TradeStation (broker outages, off-platform executions, demo entries)
- **Stop-loss + R-multiple**: attach a planned-stop price (or risk-dollar amount) per trade; the Reports tab's By R-Multiple sub-tab buckets outcomes by R
- **Zoom controls**: scale font and row height for readability — persists per table
- **Calendar drilldown**: date filter coexists with the filter bar (both AND together)
- **First-run welcome**: empty state with a "Go to New Trade" shortcut

### Journal

- **Rich text editor**: formatted notes per trade with inline images (paste or drag-drop)
- **Toolbar**: Bold / Italic / Underline / Strikethrough, **H1 / H2 / H3** heading toggles, font size, text + highlight color pickers, bullet + numbered lists, clear formatting, **Set as default** / **Return to default** for the editor's persistent char format
- **Image controls**: Ctrl+wheel scales the image under the cursor (capped at 4× viewport); right-click an image → **Annotate image…** opens a freehand draw dialog with pen/eraser/undo and saves the result as a new attachment
- **Sticky text formatting**: Qt drops the cursor's foreground brush + font size whenever the document empties or formatting is stripped — TradeBook bakes an explicit foreground (the user's saved default, or the dark-theme body tone if none is saved) into every reseed so typing after delete-all, Clear Formatting, image paste, or heading toggle keeps the user's chosen size + color instead of falling back to "size 9 white"
- **File attachments**: stored as BLOBs with SHA-256 dedup (50 MB size cap per file). Non-image files appear as Ctrl-clickable links that open in the OS default viewer
- **Drawing canvas**: standalone freehand annotation tool inserted as an attachment via the **Draw** button
- **Tagging system**: customizable colored tags (defaults: **Breakout, EP_Earnings, EP_Other, Parabolic Long, Parabolic Short**). Add new tags via the picker; deletes cascade across linked trades
- **Filters**: text search (LIKE on raw HTML), date range with preset buttons, has-journal-only toggle, tag multi-select, and ticker multi-select
- **Find / Replace**: Ctrl+F opens an inline find bar with case-sensitive, forward / backward, and replace-all (single undo block)
- **Autosave**: changes are flushed on selection change, tab switch, and window close — empty notes delete the entry rather than orphaning a row
- **Right-click Export**: export any journal entry as `.docx`, `.txt`, `.md`, or `.html` (rename inline before saving)
- **Generate Brief**: compile the filtered set of journal entries into a consolidated document (see Briefs below)

### Briefs

A workspace for consolidated trading documents — auto-generated digests or freeform writing. Built for documents large enough that scrolling alone won't cut it.

- **Generated briefs**: one-click compile all journal entries matching the Journal tab's current filters (date range, tags, tickers, text search) into a single chronological document with a per-trade header (`SYMBOL — Direction — date time — Net P&L`)
- **Empty briefs**: `+ New empty brief` button creates a blank titled document
- **Auto-titling**: generated titles follow `{date_or_range}_{tags_and_tickers_alpha}` — e.g. `2026-04-01_to_2026-04-11_Breakout_EP_Earnings_AAPL_MSFT` — and can be overridden at generation time
- **Full rich-text editor**: same JournalEditor as the Journal tab — image paste/drop, Ctrl+wheel scaling, right-click annotate, Draw button
- **Outline pane**: clickable list of every H1/H2/H3 heading on the right of the editor — click to scroll. Toggleable from the toolbar
- **Opt-in thumbnail strip**: a curated row of screenshots below the editor — *not* every embedded image automatically. Right-click any image in the editor → **Add to thumbnails** to pin it; right-click a thumbnail → **Remove from thumbnails** to drop it. Click a thumbnail to scroll to that image. Per-brief selection is stored in the DB; a size slider in the strip's title row scales every thumbnail together (5 presets from 64×48 up to 200×150), persisted per-tab. A **Border…** button next to the slider opens an editor with two sections, each a **color** + **thickness** control with a live preview: the **thumbnail tile** outline, and the **pinned-image highlight** — the border the editor paints around document images that are pinned as thumbnails (gold/3px by default; 0 px hides it). Both are persisted per-tab
- **Collapse all / Expand all**: fold every body block under each heading so you can see the document skeleton, then expand back when reading
- **Output options**: save to the Briefs tab, download as `.docx`, or both (via checkboxes in the Generate Brief dialog)
- **Include images**: optional toggle per brief — when on, images from source journal entries are preserved
- **List ordering**: briefs are sorted by last-edited (`updated_at DESC`) so your most recent work is always on top
- **Right-click Export**: same four-format export as the Journal tab, with optional rename
- **Search + date filter**: title + content LIKE search, plus a date range with preset buttons. Date filter is **timezone-aware** — briefs edited late-evening (UTC tomorrow) no longer disappear from the list until the next-day restart

### Strategies

A long-form playbook tab — separate database table from Briefs so per-setup playbooks don't co-mingle with daily digests.

- **Same navigator as Briefs**: outline pane (toggleable), opt-in thumbnail strip with right-click add/remove + a size slider + a **Border…** editor for both the thumbnail-tile outline and the pinned-image highlight (toggleable), collapse-all / expand-all heading sections — built for documents with dozens of screenshots where only a handful matter
- **Pre-seeded pages**: one empty page per default tag on first launch (Breakout, EP_Earnings, EP_Other, Parabolic Long, Parabolic Short). Add or delete pages freely; deletions stick across re-launches
- **Same editor, same toolbar**: H1/H2/H3 buttons, image paste/drop, Ctrl+wheel scaling, draw, find/replace, export to docx/txt/md/html — identical to Journal and Briefs
- **Search + date filter**: title + content LIKE search, with the same UTC-aware local-date conversion as Briefs

### Reports

Nine analytical sub-tabs, all sharing a common filter bar:

| Report | What it shows |
|--------|---------------|
| **By Symbol** | Per-ticker P&L, win rate, expectancy, profit factor |
| **By Direction** | Long vs Short breakdown |
| **By Day of Week** | Which weekdays are most profitable |
| **By Hour of Day** | Performance by market hour |
| **By Hold Time** | Short-term vs swing performance (configurable bins) |
| **By Entry Price** | Low-priced vs high-priced stock performance (configurable bins) |
| **By R-Multiple** | Outcome distribution in units of planned risk (uses stop-loss or default-risk fallback) |
| **Streaks** | Longest/current win and loss runs, average run length |
| **Drawdown** | Max drawdown ($, %), duration, peak and trough timestamps, longest underwater run |

Each report includes a sortable data table and a category bar chart with a Table / Chart view toggle. Copy-to-clipboard and Export-CSV per view.

### Data Safety

- **Backup folders split**: `backups/auto/` (capped at 5, pruned automatically) for startup + pre-import snapshots; `backups/manual/` (never pruned) for user-triggered snapshots via `Ctrl+B` or `File → Back up now`
- **Throttled startup backup**: skipped if a snapshot younger than 60 seconds already exists (avoids spam on rapid relaunches)
- **Restore from backup**: `File → Restore from backup…` browses both folders, lists snapshot age + trade count, takes a safety snapshot of the current DB before swapping
- **Atomic imports**: insert + rebuild runs in a single SQLite transaction; failures roll back cleanly
- **Recycle bin**: deleted trades sit in `deleted_trades` for 30 days with full JSON snapshots (executions + journal + tags) — restore brings them back as new rows; auto-purge runs on startup
- **Hidden trades**: soft-hide trades from view without deleting data — keyed by `(symbol, entry_time, direction)` so hide state survives `rebuild_trades` drops
- **Journal + tag preservation across rebuild**: re-importing fills no longer wipes notes — `rebuild_trades` snapshots journal entries, tag links, and stop-loss prices by `(symbol, entry_time, direction)` and re-links them after the rebuild

### Input Validation

- **Paste size cap**: 10 MB limit prevents the parser from consuming unbounded memory
- **Attachment size cap**: 50 MB per file prevents database bloat
- **Numeric guards**: parser rejects `NaN`, `Inf`, and other non-finite values in price/quantity fields
- **Filename sanitization**: attachment filenames are stripped of path separators, control characters, and capped at 255 chars
- **HTML escaping**: attachment link names are properly escaped to prevent injection via crafted filenames
- **Single-instance lock**: `QLockFile` on `data/tradebook.lock` prevents two copies racing to write the same DB; stale locks (left by crashes) are reclaimed automatically

---

## Screenshots

> *Coming soon — the app uses a custom dark theme with green/red P&L coloring throughout.*

---

## Installation

### From Source

```bash
# Clone or download the project
cd Trade_Tracker

# Create a virtual environment
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS / Linux

# Install dependencies
pip install PySide6 pyqtgraph numpy python-docx

# Run
python main.py
```

### Pre-built Executable (Windows)

1. Download `TradeBook.exe` from the `dist/` folder (or build it yourself — see below)
2. Place it wherever you like
3. Double-click to launch — `data/` and `backups/` folders are created automatically next to the exe on first run

No installation required. All data stays local.

---

## Building the Executable

```bash
# Install PyInstaller (if not already)
pip install pyinstaller

# Build from the spec file
cd Trade_Tracker
pyinstaller tradebook.spec --noconfirm
```

Output: `dist/TradeBook.exe` (~70 MB). The spec bundles:

- The QSS dark theme stylesheet
- Required DLLs for Conda-style Python installs (libffi, libssl, sqlite3, etc.)
- `tradebook.ico` as the application icon

The exe runs as a windowed GUI app (no console window).

> **Important**: never pass `--clean` and never delete `dist/` wholesale — `dist/data/`, `dist/backups/auto/`, `dist/backups/manual/`, and the Windows registry under `HKCU\Software\TradeBook\TradeBook` hold all your live data and formatting preferences. PyInstaller only overwrites `dist/TradeBook.exe`. Take a manual safety copy of `dist/data/tradebook.db` into `dist/backups/manual/` before any rebuild as a belt-and-suspenders precaution.

---

## Project Structure

```text
Trade_Tracker/
├── analytics/                     # Pure-data analysis (no Qt imports)
│   ├── metrics.py                 #   TradeMetrics + compute_metrics()
│   ├── calendar_data.py           #   Mon-Fri grid builder (DayCell, WeekRow)
│   ├── drawdown.py                #   Equity curve + max drawdown stats
│   ├── reports.py                 #   FilterSpec, apply_filters, by_symbol/direction/price
│   ├── streaks.py                 #   Win/loss streak analysis
│   ├── time_analysis.py           #   By day-of-week, hour, hold-time buckets
│   ├── briefs.py                  #   Brief generator (filter + title + HTML)
│   ├── r_multiple.py              #   R = pnl / risk distribution analytics
│   └── tag_breakdown.py           #   Per-tag P&L roll-up
│
├── export/                        # Document exporters (docx, txt, md, html)
│   └── exporters.py               #   HTML → block model → format writers
│
├── gui/
│   ├── main_window.py             # QTabWidget host (8 tabs), inter-tab signals,
│   │                              #   menu bar (Backup, Restore, Recycle bin)
│   ├── settings_keys.py           # Central registry for all QSettings keys
│   ├── styles/
│   │   ├── theme.py               #   Palette-derived QSS builder
│   │   └── dark_theme.qss         #   Static dark theme template
│   ├── tabs/
│   │   ├── dashboard.py           #   Free-canvas chart cards + stat cards + goal bars
│   │   ├── calendar_tab.py        #   Heatmap calendar with drilldown
│   │   ├── reports.py             #   9 report sub-tabs + bin configuration
│   │   ├── trades.py              #   Main trade table with filter bar
│   │   ├── journal.py             #   Rich text journal + tags + attachments
│   │   ├── briefs.py              #   Consolidated trading documents
│   │   ├── strategies.py          #   Long-form playbooks w/ outline + thumbs + collapse
│   │   └── new_trade.py           #   Paste preview + import flow
│   ├── dialogs/
│   │   ├── _geometry.py           #   DialogGeometryMixin (persist window position)
│   │   ├── bin_config.py          #   Price / hold-time bin edge editor
│   │   ├── chart_card_config.py   #   Drag-reorder + check chart cards
│   │   ├── chart_palette.py       #   Per-card color override picker
│   │   ├── date_jump.py           #   Calendar month picker
│   │   ├── draw_dialog.py         #   Freehand annotation canvas
│   │   ├── export_document.py     #   Rename + format picker for export
│   │   ├── generate_brief.py      #   Brief generation (output + images options)
│   │   ├── goals.py               #   Daily/weekly/monthly/yearly P&L target editor
│   │   ├── manual_trade.py        #   Hand-record + edit a trade
│   │   ├── new_tag.py             #   Tag creation (name + color)
│   │   ├── r_multiple_settings.py #   Default risk-dollar amount for R analytics
│   │   ├── recycle_bin.py         #   Soft-deleted trade browser + restore/purge
│   │   ├── restore_backup.py      #   Snapshot picker + safety-restore flow
│   │   ├── stat_card_config.py    #   Drag-reorder + check stat cards
│   │   ├── stop_loss.py           #   Set/clear stop on selected trades
│   │   └── tag_picker.py          #   Assign tags to a trade
│   └── widgets/
│       ├── calendar_grid.py       #   Custom-painted Mon-Fri heatmap
│       ├── chart_palette.py       #   ChartPalette + global hub + per-widget overrides
│       ├── chart_registry.py      #   ChartCardDef registry (key, label, default size)
│       ├── charts.py              #   pyqtgraph equity curve + daily P&L bars
│       ├── collapsible_section.py #   Click-header toggle host
│       ├── composed_charts.py     #   DualHorizontalBar / CategoryBars / BigValueCard /
│       │                          #     ChartCard + ChartCanvas free-positioning host
│       ├── date_range_bar.py      #   Date pickers + presets
│       ├── draw_canvas.py         #   QPixmap freehand drawing surface
│       ├── editor_defaults.py     #   Persistent default char format (size, color, …)
│       ├── find_bar.py            #   Inline find/replace strip for QTextEdit
│       ├── goal_bars.py           #   Stacked P&L progress bars vs targets
│       ├── journal_editor.py      #   Rich text + image paste/drop + attachments
│       ├── open_positions_card.py #   Live count + capital + per-symbol breakdown
│       ├── painter_charts.py      #   DonutChart / GaugeChart / ProfitFactorGauge
│       ├── report_filter_bar.py   #   Symbol/direction/W-L/hold-time filters
│       ├── report_view.py         #   Table + bar chart report renderer
│       ├── rich_text_toolbar.py   #   B/I/U/S, H1/H2/H3, size, colors, lists, defaults
│       ├── stat_card.py           #   Single metric display card
│       ├── stop_risk_inputs.py    #   Bidirectional stop-price ↔ risk-dollar inputs
│       ├── strategy_navigator.py  #   OutlineWidget + ImageThumbStrip
│       ├── tag_chip_strip.py      #   Horizontal colored tag chips
│       ├── week_strip.py          #   Last-N weekday cells with per-day P&L
│       └── zoom_controls.py       #   Font/row-height zoom buttons
│
├── ingest/                        # Data pipeline
│   ├── tradestation_parser.py     #   Parse TradeStation paste → execution dicts
│   ├── trade_builder.py           #   Group executions → logical trades
│   ├── db_manager.py              #   SQLite schema, CRUD, rebuild, recycle bin
│   └── backups.py                 #   Online backup + retention pruning
│
├── models/                        # Qt table models
│   ├── table_model.py             #   TradeTableModel (main Trades tab)
│   ├── journal_trade_model.py     #   JournalTradeModel (Journal tab sidebar)
│   └── preview_model.py           #   PreviewModel (New Trade import preview)
│
├── scripts/                       # User-run maintenance scripts
│   └── wipe_dist_briefs.py        #   Backup + clear all briefs from dist DB
│
├── config.py                      # Paths (frozen-aware), constants, order types
├── main.py                        # Application entry point
├── tradebook.spec                 # PyInstaller onefile build configuration
└── tradebook.ico                  # Application icon
```

---

## How It Works

### Import Flow

1. Copy your TradeStation order history (tab-delimited rows)
2. Paste into the **New Trade** tab
3. The parser extracts filled orders, flags duplicates and errors in a preview table
4. Optionally edit NEW rows inline (typo fixes, override prices) and trim unwanted rows with Delete
5. Click **Import** — a pre-import backup is taken, then:
   - Executions are inserted with `INSERT OR IGNORE` (dedup via import hash)
   - The trade builder groups executions into logical trades by symbol + chronological order
   - Import-time risk is applied (stop-loss derived from declared risk per trade)
   - The daily summary table is rebuilt
6. The app switches to the **Trades** tab showing your new trades

### Trade Builder Logic

The trade builder is a per-symbol state machine:

- **Buy** opens a Long position; **Sell Short** opens a Short position
- **Sell** closes a Long; **Buy to Cover** closes a Short
- Partial closes are supported (position scales down until flat)
- Entry/exit prices are volume-weighted averages (VWAP)
- Hold duration is measured from first entry fill to final exit fill
- A bad fill (e.g., a sell that would flip a long to short) is reported as an error and the state machine resets — completed trades that came earlier in that symbol's history are preserved, not wiped

### Data Storage

All data lives in a single SQLite database (`data/tradebook.db`):

| Table | Purpose |
|-------|---------|
| `executions` | Raw fills from TradeStation (deduped by `import_hash`) |
| `trades` | Logical trades derived from executions |
| `trade_executions` | Join table linking trades to their fills |
| `daily_summary` | Aggregated P&L by exit date |
| `hidden_trades` | Soft-hidden trade markers (keyed by symbol/entry_time/direction) |
| `journal_entries` | Rich text notes per trade |
| `journal_attachments` | Binary file storage (SHA-256 deduped) |
| `tags` | User-defined colored labels |
| `trade_tags` | Many-to-many trade-tag assignments |
| `briefs` | Generated + hand-written brief documents (incl. `thumbnail_ids` JSON of opted-in image attachments) |
| `strategies` | Long-form playbook documents — Strategies tab (incl. `thumbnail_ids` JSON of opted-in image attachments) |
| `deleted_trades` | Recycle bin — JSON-snapshot of soft-deleted trades |

Backups are stored under `backups/auto/` (auto-pruned, capped at 5) and `backups/manual/` (never pruned) as timestamped copies (e.g., `tradebook_20260411_142530.db`).

---

## Tech Stack

| Component | Technology |
|-----------|------------|
| **GUI Framework** | PySide6 6.11 (Qt 6) |
| **Charts** | pyqtgraph 0.14 |
| **Database** | SQLite 3 (via Python stdlib) |
| **Styling** | QSS dark theme (palette-derived, runtime-themed) |
| **Docx export** | python-docx 1.2 |
| **Packaging** | PyInstaller 6.19 (--onefile) |
| **Language** | Python 3.13 |

---

## Configuration & Persistence

Every persisted preference is registered in `gui/settings_keys.py` so there's a single grep target.

- **Window geometry + state**: saved/restored via `QSettings` (registry on Windows, INI on other platforms)
- **Zoom levels**: per-table font/row scaling persisted across sessions
- **Dashboard layout**: chart card position/size/minimized state per preset, plus per-card palette overrides
- **Dashboard chart presets**: named layout snapshots, applied via the toolbar preset bar
- **Dashboard "seen" set**: tracks which chart-card keys have been introduced so new releases auto-surface fresh cards once but respect later removals
- **Dashboard collapsible sections**: week strip + metrics open/closed state
- **Stat card order**: drag-drop arrangement persisted (forward-migrates new metrics)
- **Calendar display mode**: cell content mode (P&L / count / W-L) + last-viewed month/year persisted
- **Reports**: active sub-tab + custom price + hold-time bin edges
- **Journal + Briefs + Strategies**: splitter geometry, last-selected row, has-journal-only toggle, last export directory, search/filter state
- **Briefs + Strategies extras**: outline pane visibility, thumbnail strip visibility, thumbnail size index, thumbnail tile border color + width, pinned-image overlay border color + width (all persisted independently per tab); the *set* of pinned thumbnails lives on the brief/strategy DB row, not in QSettings
- **Editor defaults**: persistent default char format (size / color / bold / italic / underline) for new entries
- **Goals**: daily / weekly / monthly / yearly P&L targets
- **R-multiple**: default risk-dollar amount for trades without a recorded stop

All settings use the organization/app key `TradeBook/TradeBook`.

---

## Roadmap

- [ ] Multi-broker support (beyond TradeStation)
- [ ] CSV file import (in addition to paste)
- [ ] Trade replay / chart annotation overlay
- [ ] Export to Excel / PDF
- [ ] Options and futures trade support
- [ ] Per-strategy auto-tagging (apply a tag to every trade matching a strategy's filter)

---

## License

[MIT](LICENSE) © 2026 Troy Folmer
