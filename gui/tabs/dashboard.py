"""Dashboard tab — at-a-glance summary of trading performance.

Phase 13 rebuild adds:
    * Week strip — last N weekdays as a horizontal calendar
    * Collapsible sections (week strip + stat cards)
    * Configurable chart card grid (donut, gauges, dual bars, category
      bars, big-value card)
    * Scrollable layout so the page degrades gracefully as charts pile up

Layout from top to bottom:

    [date range]  [Goals]  [Configure cards]                ⇅
    ───── scroll area ─────────────────────────────────────
    [▼ Week strip]                                          (collapsible)
    [▼ Metrics]   ← the 9-card stat grid                    (collapsible)
    [Goal bars]   ← only visible when ≥1 goal is set
    [Chart cards grid]
    [Equity curve]
    [Daily P&L bars]
"""
from __future__ import annotations

import json
import math
from typing import Callable, Optional

from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QDialog, QGridLayout, QHBoxLayout, QInputDialog, QLabel, QMenu,
    QMessageBox, QPushButton, QScrollArea, QStackedWidget, QToolButton,
    QVBoxLayout, QWidget,
)

from analytics.metrics import (
    DEFAULT_METRIC_ORDER, METRIC_BY_KEY, METRIC_REGISTRY,
    TradeMetrics, compute_metrics, filter_by_exit_date,
)
from analytics import tag_breakdown as tag_mod
from analytics.reports import by_ticker_price
from analytics.time_analysis import (
    by_day_of_week, by_duration, by_hour_of_day,
)
from gui.dialogs.chart_card_config import ChartCardConfigDialog
from gui.dialogs.goals import GoalsDialog
from gui.dialogs.stat_card_config import StatCardConfigDialog
from gui.settings_keys import (
    DASHBOARD_CARD_ORDER, DASHBOARD_CHART_KEYS_SEEN, DASHBOARD_CHART_LAYOUT,
    DASHBOARD_CHART_ORDER, DASHBOARD_CHART_PRESETS,
)
from gui.widgets.charts import DailyPnLBarWidget, EquityCurveWidget
from gui.widgets.chart_registry import (
    CHART_BY_KEY, CHART_CARDS, DEFAULT_CHART_ORDER,
)
from gui.widgets.collapsible_section import CollapsibleSection
from gui.widgets.composed_charts import (
    BigValueCard, CategoryBars, ChartCanvas, ChartCard, DualHorizontalBar,
    SIZE_DEFAULTS_PX, SIZE_LARGE, SIZE_SMALL, SIZE_SPANS, SIZE_TALL, SIZE_WIDE,
)
from gui.widgets.date_range_bar import DateRangeBar
from gui.widgets.goal_bars import GoalBarsWidget
from gui.widgets.open_positions_card import OpenPositionsCard
from gui.widgets.painter_charts import DonutChart, GaugeChart
from gui.widgets.stat_card import StatCard
from gui.widgets.week_strip import WeekStripWidget
from ingest import db_manager


CARDS_PER_ROW = 4
CHART_CARDS_PER_ROW = 2

# Persistence keys for the collapsible sections.
_SETTINGS_WEEK_OPEN = "dashboard/week_strip_open"
_SETTINGS_METRICS_OPEN = "dashboard/metrics_open"


def _fmt_seconds(s: Optional[float]) -> str:
    """Human-readable duration (used by hold-time chart)."""
    if s is None:
        return "n/a"
    s = int(round(s))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        m, sec = divmod(s, 60)
        return f"{m}m {sec}s"
    if s < 86400:
        h, rem = divmod(s, 3600)
        m, _ = divmod(rem, 60)
        return f"{h}h {m}m"
    d, rem = divmod(s, 86400)
    h, _ = divmod(rem, 3600)
    return f"{d}d {h}h"


def _fmt_ratio_value(v: float) -> str:
    """Formatter for the risk-adjusted ratio BigValueCards. ``None`` is
    handled by BigValueCard itself (renders an em dash); this only sees
    real floats, so we just guard against the unbounded case."""
    if math.isinf(v):
        return "∞"
    return f"{v:.2f}"


def _report_rows_to_label_value(
    report,
    *,
    value_key: str = "net_pnl",
) -> list[tuple[str, float]]:
    """Convert a Report's row dicts to ``(label, value)`` tuples for
    the ``CategoryBars`` widget."""
    out: list[tuple[str, float]] = []
    for row in report.rows:
        try:
            v = float(row.get(value_key) or 0.0)
        except (TypeError, ValueError):
            continue
        out.append((str(row.get("label") or "?"), v))
    return out


class DashboardTab(QWidget):
    def __init__(
        self,
        get_conn: Callable,
        settings: Optional[QSettings] = None,
        parent=None,
    ):
        super().__init__(parent)
        self._get_conn = get_conn
        self._settings = settings
        self._card_widgets: list[StatCard] = []
        # Make sure the chart palette is bound to the same store we'll
        # use for everything else — safe to call repeatedly.
        if settings is not None:
            from gui.widgets.chart_palette import init_palette
            init_palette(settings)

        # --- top bar: date range + buttons --------------------------------
        self.date_bar = DateRangeBar(settings, self)
        self.date_bar.range_changed.connect(lambda *_: self.refresh())

        self.btn_goals = QPushButton("🎯 Goals…", self)
        self.btn_goals.setToolTip(
            "Set daily / weekly / monthly / yearly P&L targets"
        )
        self.btn_goals.clicked.connect(self._on_edit_goals)

        self.btn_config = QPushButton("⚙ Stat Cards", self)
        self.btn_config.setToolTip(
            "Choose which stat cards appear in the Metrics row, "
            "and in what order"
        )
        self.btn_config.clicked.connect(self._on_configure)

        self.btn_charts = QPushButton("⚙ Charts", self)
        self.btn_charts.setToolTip(
            "Choose which chart cards appear on the Dashboard, "
            "and in what order"
        )
        self.btn_charts.clicked.connect(self._on_configure_charts)

        # Preset bar — sits where the old Fit-to-window button was,
        # extending leftward as more presets are saved. Each preset
        # button click applies that layout; right-click offers
        # rename / overwrite / delete. The trailing "+ Save" prompts
        # for a name and snapshots the current layout.
        self._presets_host = QWidget(self)
        self._presets_layout = QHBoxLayout(self._presets_host)
        self._presets_layout.setContentsMargins(0, 0, 0, 0)
        self._presets_layout.setSpacing(4)

        self.btn_save_preset = QPushButton("+ Save layout", self)
        self.btn_save_preset.setToolTip(
            "Save the current chart layout as a named preset"
        )
        self.btn_save_preset.clicked.connect(self._on_save_preset)

        top_bar = QHBoxLayout()
        top_bar.setSpacing(8)
        top_bar.addWidget(self.date_bar, 1)
        top_bar.addWidget(self._presets_host)
        top_bar.addWidget(self.btn_save_preset)
        top_bar.addWidget(self.btn_goals)
        top_bar.addWidget(self.btn_config)
        top_bar.addWidget(self.btn_charts)

        # Visible-feedback caption that updates on every refresh —
        # tells the user exactly what the cards below are summarising.
        # Without this, date-range changes that happen to produce
        # similar-looking numbers feel like nothing happened.
        self.range_caption = QLabel("", self)
        self.range_caption.setObjectName("hintLabel")
        self.range_caption.setAlignment(Qt.AlignmentFlag.AlignLeft)

        # --- week strip (collapsible) -------------------------------------
        self.week_strip = WeekStripWidget(self)
        self.section_week = CollapsibleSection(
            "Recent trading days", self,
            settings=settings, settings_key=_SETTINGS_WEEK_OPEN,
        )
        self.section_week.set_content(self.week_strip)

        # --- metrics grid (collapsible) -----------------------------------
        self.cards_container = QWidget(self)
        self.cards_grid = QGridLayout(self.cards_container)
        self.cards_grid.setSpacing(8)
        self.cards_grid.setContentsMargins(0, 0, 0, 0)
        self.section_metrics = CollapsibleSection(
            "Metrics", self,
            settings=settings, settings_key=_SETTINGS_METRICS_OPEN,
        )
        self.section_metrics.set_content(self.cards_container)

        # --- goal progress bars (hidden when no goals set) ----------------
        self.goal_bars = GoalBarsWidget(self._settings, self)

        # --- equity / daily bars (existing pyqtgraph charts) -------------
        # Constructed BEFORE _build_chart_cards because they're now
        # wrapped as ChartCards alongside the rest.
        self.equity_curve = EquityCurveWidget(self)
        self.equity_curve.setMinimumHeight(220)
        self.daily_bars = DailyPnLBarWidget(self)
        self.daily_bars.setMinimumHeight(180)

        # --- chart cards --------------------------------------------------
        # Hydrate persisted layout BEFORE building cards so the very
        # first paint reflects saved geometry / minimized states.
        self._load_chart_layout()
        self._build_chart_cards()

        # Free-positioning canvas for chart cards. Each card is moved
        # via its title-bar and resized via its bottom-right size grip;
        # positions/sizes persist as absolute pixels.
        self.charts_canvas = ChartCanvas(self)
        self._chart_save_timer = QTimer(self)
        self._chart_save_timer.setSingleShot(True)
        self._chart_save_timer.setInterval(250)
        self._chart_save_timer.timeout.connect(self._save_chart_layout)
        self._populate_canvas()

        # The chart canvas lives in its own scroll area so the user can
        # scroll past their layout's bounds without affecting the
        # surrounding page chrome (week strip, metrics, goal bars).
        self.charts_scroll = QScrollArea(self)
        self.charts_scroll.setWidget(self.charts_canvas)
        self.charts_scroll.setWidgetResizable(False)
        self.charts_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded,
        )
        self.charts_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.charts_scroll.setMinimumHeight(420)

        # --- assemble populated state ------------------------------------
        populated = QWidget(self)
        pop_layout = QVBoxLayout(populated)
        pop_layout.setContentsMargins(0, 0, 0, 0)
        pop_layout.setSpacing(10)
        pop_layout.addWidget(self.range_caption)
        pop_layout.addWidget(self.section_week)
        pop_layout.addWidget(self.section_metrics)
        pop_layout.addWidget(self.goal_bars)
        pop_layout.addWidget(self.charts_scroll, 1)

        # Wrap populated content in a scroll area so heights compose
        # gracefully on small screens.
        scroll = QScrollArea(self)
        scroll.setWidget(populated)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded,
        )
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        # The populated state is the only state — the prior "empty
        # placeholder" QStackedWidget was hiding the dashboard in
        # zero-trade ranges and masking date-change feedback. Cards
        # now show "$0 / 0 trades" themselves on empty ranges, which
        # is semantically correct and visually responsive.
        self.stack = QStackedWidget(self)
        self.stack.addWidget(scroll)            # index 0 (only)

        main = QVBoxLayout(self)
        main.setContentsMargins(10, 10, 10, 10)
        main.setSpacing(10)
        main.addLayout(top_bar)
        main.addWidget(self.stack, 1)

        self._rebuild_cards()
        self._rebuild_preset_bar()
        self.refresh()

    # ---- chart cards setup ------------------------------------------------

    def _build_chart_cards(self) -> None:
        """Construct each chart-card widget once. Held in
        ``self._chart_cards_by_key`` so the configurable grid can pick
        which subset to show (and in what order) without rebuilding
        the widgets each time the user opens the config dialog."""
        # The widgets we paint into are kept on `self.chart_*` so
        # refresh() can push fresh data in via the same attribute names
        # used pre-Phase 13.
        self.chart_winloss = DonutChart()
        self.chart_hold_wl = DualHorizontalBar(
            win_label="Winners hold",
            loss_label="Losers hold",
            formatter=_fmt_seconds,
        )
        self.chart_avg_wl = DualHorizontalBar(
            win_label="Avg winner",
            loss_label="Avg loser",
        )
        self.chart_largest = GaugeChart(mode=GaugeChart.MODE_GAIN_LOSS)
        self.chart_dow = CategoryBars(page_size=7, pct_basis="abs_total")
        self.chart_duration = CategoryBars(page_size=4, pct_basis="abs_total")
        self.chart_price = CategoryBars(page_size=7, pct_basis="abs_total")
        self.chart_hour = CategoryBars(page_size=7, pct_basis="abs_total")
        self.chart_fees = BigValueCard()
        self.chart_pf = GaugeChart(mode=GaugeChart.MODE_PROFIT_FACTOR)
        self.chart_tags = CategoryBars(page_size=7, pct_basis="win_loss_split")
        self.chart_open_positions = OpenPositionsCard()
        # Risk-adjusted ratio cards — single big number, sign-colored.
        self.chart_sortino = BigValueCard(
            formatter=_fmt_ratio_value, color_by_sign=True,
        )
        self.chart_gain_to_pain = BigValueCard(
            formatter=_fmt_ratio_value, color_by_sign=True,
        )

        # Map the registry keys to ChartCard widgets. Each card knows
        # its own key (used in drag-and-drop swap and persistence).
        inner_for: dict[str, QWidget] = {
            "winloss": self.chart_winloss,
            "hold_wl": self.chart_hold_wl,
            "avg_wl": self.chart_avg_wl,
            "largest": self.chart_largest,
            "dow": self.chart_dow,
            "duration": self.chart_duration,
            "price": self.chart_price,
            "hour": self.chart_hour,
            "fees": self.chart_fees,
            "pf": self.chart_pf,
            "tags": self.chart_tags,
            # Phase 14b: equity curve + daily P&L bars are now first-
            # class chart cards too — drag, resize, minimize, hide via
            # the same Configure Charts dialog as the rest.
            "equity_curve": self.equity_curve,
            "daily_pnl": self.daily_bars,
            "open_positions": self.chart_open_positions,
            "adjusted_sortino": self.chart_sortino,
            "gain_to_pain": self.chart_gain_to_pain,
        }
        self._chart_cards_by_key: dict[str, ChartCard] = {}
        for key, inner in inner_for.items():
            card = ChartCard(key, CHART_BY_KEY[key].label, inner)
            # Move / resize / minimize all funnel through the same
            # debounced save path.
            card.layout_changed.connect(self._on_card_layout_changed)
            card.geometry_changed.connect(self._on_card_layout_changed)
            self._chart_cards_by_key[key] = card

        # Reparent everything off the canvas initially. _populate_canvas
        # decides which become visible.
        for card in self._chart_cards_by_key.values():
            card.setParent(None)
            card.hide()

    def _populate_canvas(self) -> None:
        """Re-parent every selected chart card onto the free canvas
        with its persisted absolute (x, y, w, h) and minimize state.

        Cards with saved geometry land at their saved spot. Cards
        without saved geometry (newly added via Configure Charts, or
        on first launch) get placed by ``_find_empty_spot`` so they
        never spawn on top of an existing card.

        Called at construction, after the Configure Charts dialog,
        and after applying a preset.
        """
        for card in self._chart_cards_by_key.values():
            card.setParent(None)
            card.hide()

        order = self._load_chart_order()
        states = self._chart_layout_state

        # Two-pass placement so saved cards claim their spots first;
        # then unsaved cards search for the next empty slot around them.
        unseeded: list[tuple[str, int, int, bool]] = []  # (key, w, h, minimized)
        for key in order:
            card = self._chart_cards_by_key.get(key)
            if card is None:
                continue
            st = states.get(key) or {}
            x = st.get("x")
            y = st.get("y")
            w = st.get("w") or card.DEFAULT_W
            h = st.get("h") or card.DEFAULT_H
            minimized = bool(st.get("minimized", False))

            card.blockSignals(True)
            try:
                card.setParent(self.charts_canvas)
                card.set_minimized(minimized, emit=False)
                # Hydrate per-card palette override (if any).
                pal_dict = st.get("palette")
                if isinstance(pal_dict, dict):
                    from gui.widgets.chart_palette import palette_from_dict
                    card._palette_override = palette_from_dict(pal_dict)
                else:
                    card._palette_override = None
                if isinstance(x, int) and isinstance(y, int):
                    card.setGeometry(int(x), int(y), int(w), int(h))
                    card.show()
                    self.charts_canvas.ensure_room_for(card)
                else:
                    unseeded.append((key, int(w), int(h), minimized))
            finally:
                card.blockSignals(False)

        # Now place each unseeded card in the first empty spot.
        for key, w, h, minimized in unseeded:
            card = self._chart_cards_by_key[key]
            spot_x, spot_y = self._find_empty_spot(card, w, h)
            card.blockSignals(True)
            try:
                card.setGeometry(spot_x, spot_y, w, h)
                card.show()
            finally:
                card.blockSignals(False)
            self.charts_canvas.ensure_room_for(card)
            self._chart_layout_state[key] = {
                "x": card.x(), "y": card.y(),
                "w": card.width(), "h": card.height(),
                "minimized": minimized,
            }

        # Charts inside each card called `get_palette(self)` during
        # construction (before hydration assigned `card._palette_override`),
        # so they cached DEFAULT_PALETTE. Fire the global palette signal
        # once now so every chart re-resolves through `_walk_override`
        # and picks up its card's restored colors.
        from gui.widgets.chart_palette import palette_hub
        palette_hub().palette_changed.emit()

    def _find_empty_spot(
        self, card: ChartCard, w: int, h: int,
    ) -> tuple[int, int]:
        """Scan the canvas in row-major order at a coarse grid step
        and return the top-left of the first w×h rect that doesn't
        overlap any other visible card.

        Falls back to (margin, max_y) — a fresh row below everything —
        if nothing fits in the current canvas width."""
        from PySide6.QtCore import QRect

        canvas = self.charts_canvas
        margin = canvas.EDGE_MARGIN
        step = 20
        canvas_w = max(canvas.width(), canvas.DEFAULT_W)
        canvas_h = max(canvas.height(), canvas.DEFAULT_H)

        # Try existing canvas area first.
        y = margin
        while y + h + margin <= canvas_h:
            x = margin
            while x + w + margin <= canvas_w:
                if not canvas.would_collide(card, QRect(x, y, w, h)):
                    return (x, y)
                x += step
            y += step

        # Nothing fits — drop the card on a fresh row below the
        # current bottom edge. Compute that bottom from the cards
        # actually parented to the canvas (excluding ``card`` itself).
        max_bottom = margin
        for c in canvas.cards():
            if c is card or c.isHidden():
                continue
            max_bottom = max(max_bottom, c.y() + c.height() + margin)
        return (margin, max_bottom)

    # Convenience for refresh() and tests — returns the visible cards
    # in display order (top-left → bottom-right).
    @property
    def _chart_cards(self) -> list[ChartCard]:
        return [
            self._chart_cards_by_key[k]
            for k in self._load_chart_order()
            if k in self._chart_cards_by_key
        ]

    # ---- settings helpers --------------------------------------------------

    def _load_card_order(self) -> list[str]:
        if self._settings is None:
            return list(DEFAULT_METRIC_ORDER)
        raw = self._settings.value(DASHBOARD_CARD_ORDER)
        if raw is None:
            return list(DEFAULT_METRIC_ORDER)
        if isinstance(raw, str):
            keys = [k.strip() for k in raw.split(",") if k.strip()]
        elif isinstance(raw, (list, tuple)):
            keys = [str(k) for k in raw if k]
        else:
            return list(DEFAULT_METRIC_ORDER)
        # Drop any unknown keys.
        keys = [k for k in keys if k in METRIC_BY_KEY]
        if not keys:
            return list(DEFAULT_METRIC_ORDER)
        # Forward-migration: surface metrics that were added to the registry
        # after the user last saved their order.
        existing = set(keys)
        for k in DEFAULT_METRIC_ORDER:
            if k not in existing:
                keys.append(k)
        return keys

    def _save_card_order(self, order: list[str]) -> None:
        if self._settings is None:
            return
        self._settings.setValue(DASHBOARD_CARD_ORDER, ",".join(order))

    # --- chart-layout persistence ---------------------------------------
    #
    # Storage layout (one JSON blob per dashboard, under
    # ``DASHBOARD_CHART_LAYOUT``):
    #
    #   {"version": 1, "items": [
    #       {"key": "winloss", "size": "small", "minimized": false},
    #       {"key": "pf",      "size": "wide",  "minimized": true},
    #       ...
    #   ]}
    #
    # Order = list order. Per-card state lives in `self._chart_layout_state`
    # for fast lookup during _populate_chart_grid.

    @staticmethod
    def _default_state_for(key: str) -> dict:
        """Return the initial state for a chart key — pulls
        ``default_size`` from the registry to choose a starting tile
        size. Position fields are left absent; ``_populate_canvas``
        flow-packs them on first paint."""
        cdef = CHART_BY_KEY.get(key)
        size = cdef.default_size if cdef is not None else SIZE_SMALL
        w, h = SIZE_DEFAULTS_PX.get(size, SIZE_DEFAULTS_PX[SIZE_SMALL])
        return {"w": w, "h": h, "minimized": False}

    def _load_chart_layout(self) -> None:
        """Hydrate ``_chart_layout_order`` + ``_chart_layout_state`` from
        QSettings.

        Persistence schema:
            v2 (current) — {"version": 2, "items": [
                {"key", "x", "y", "w", "h", "minimized"}, ...
            ]}
            v1 (legacy)  — {"version": 1, "items": [
                {"key", "size", "minimized"}, ...
            ]}  → migrated to v2 by mapping size class → pixel preset
            DASHBOARD_CHART_ORDER (legacy comma list) → flow-packed
            none → all charts visible, flow-packed
        """
        self._chart_layout_order: list[str] = []
        self._chart_layout_state: dict[str, dict] = {}

        if self._settings is None:
            self._chart_layout_order = list(DEFAULT_CHART_ORDER)
            for key in self._chart_layout_order:
                self._chart_layout_state[key] = self._default_state_for(key)
            return

        raw = self._settings.value(DASHBOARD_CHART_LAYOUT)
        if isinstance(raw, str) and raw.strip():
            try:
                blob = json.loads(raw)
            except (ValueError, TypeError):
                blob = None
            if isinstance(blob, dict):
                version = blob.get("version", 1)
                items = blob.get("items") or []
                if version == 2:
                    # A v2 save is authoritative — even if empty (the
                    # user deselected every chart), don't fall through
                    # to defaults.
                    self._load_v2(items)
                    self._append_new_chart_keys()
                    return
                else:
                    self._load_v1(items)
                    if self._chart_layout_order:
                        self._append_new_chart_keys()
                        return

        # Migration: pick up the old order key if it exists.
        legacy = self._settings.value(DASHBOARD_CHART_ORDER)
        if isinstance(legacy, str) and legacy.strip():
            keys = [k.strip() for k in legacy.split(",") if k.strip()]
            keys = [k for k in keys if k in CHART_BY_KEY]
            if keys:
                self._chart_layout_order = keys
                for key in keys:
                    self._chart_layout_state[key] = self._default_state_for(key)
                self._append_new_chart_keys()
                return

        # First launch — show every chart, flow-packed at registry-
        # default tile sizes. Seed the seen-set so future "new card"
        # detection works correctly.
        self._chart_layout_order = list(DEFAULT_CHART_ORDER)
        for key in self._chart_layout_order:
            self._chart_layout_state[key] = self._default_state_for(key)
        self._save_chart_keys_seen(set(DEFAULT_CHART_ORDER))

    def _append_new_chart_keys(self) -> None:
        """Forward-migration: append any chart keys added to the
        registry since the user last opened the dashboard.

        Each key is "introduced" exactly once — its presence is
        remembered in ``DASHBOARD_CHART_KEYS_SEEN``. After that the
        user's layout is authoritative: removing a card via Configure
        Charts sticks across launches because the key already counts
        as seen. Only newly-registered keys (which aren't in the seen
        set) get auto-added.

        New cards land without saved x/y so ``_populate_canvas``
        flow-packs them into the next free spot.
        """
        seen = self._load_chart_keys_seen()
        # Keys already in the saved layout count as seen too — covers
        # the legacy-blob migration path where the seen set is empty
        # but the user clearly already has those cards. Without this,
        # those keys would be re-appended (duplicates).
        seen.update(self._chart_layout_order)
        new_keys = [k for k in DEFAULT_CHART_ORDER if k not in seen]
        if not new_keys:
            self._save_chart_keys_seen(seen)
            return
        seen.update(new_keys)
        # Don't override an explicit empty layout — that's the user
        # deliberately deselecting every card.
        if self._chart_layout_order:
            for key in new_keys:
                self._chart_layout_order.append(key)
                self._chart_layout_state[key] = self._default_state_for(key)
        self._save_chart_keys_seen(seen)

    def _load_chart_keys_seen(self) -> set[str]:
        if self._settings is None:
            return set()
        raw = self._settings.value(DASHBOARD_CHART_KEYS_SEEN)
        if isinstance(raw, str) and raw.strip():
            return {k.strip() for k in raw.split(",") if k.strip()}
        if isinstance(raw, (list, tuple)):
            return {str(k) for k in raw if k}
        return set()

    def _save_chart_keys_seen(self, seen: set[str]) -> None:
        if self._settings is None:
            return
        self._settings.setValue(
            DASHBOARD_CHART_KEYS_SEEN, ",".join(sorted(seen)),
        )

    def _load_v1(self, items: list) -> None:
        """v1 → v2 migration: map each {key, size} to a preset pixel
        tile size. Positions are left absent so the populator flow-
        packs the cards on first paint."""
        for item in items:
            if not isinstance(item, dict):
                continue
            key = item.get("key")
            if not isinstance(key, str) or key not in CHART_BY_KEY:
                continue
            raw_size = item.get("size", SIZE_SMALL)
            if not isinstance(raw_size, str) or raw_size not in SIZE_DEFAULTS_PX:
                raw_size = SIZE_SMALL
            w, h = SIZE_DEFAULTS_PX[raw_size]
            self._chart_layout_order.append(key)
            self._chart_layout_state[key] = {
                "w": w, "h": h,
                "minimized": bool(item.get("minimized", False)),
            }

    def _load_v2(self, items: list) -> None:
        for item in items:
            if not isinstance(item, dict):
                continue
            key = item.get("key")
            if not isinstance(key, str) or key not in CHART_BY_KEY:
                continue
            st: dict = {"minimized": bool(item.get("minimized", False))}
            for field in ("x", "y", "w", "h"):
                v = item.get(field)
                if isinstance(v, (int, float)):
                    st[field] = int(v)
            # Width / height fall back to registry default if missing
            # or pathologically small (e.g. corrupted JSON edit).
            if st.get("w", 0) < ChartCard.MIN_W:
                st["w"] = ChartCard.DEFAULT_W
            if st.get("h", 0) < ChartCard.MIN_H_MINIMIZED:
                st["h"] = ChartCard.DEFAULT_H
            # Per-card palette override (if present) — kept as a dict
            # here; _populate_canvas builds the ChartPalette and
            # applies it to the card.
            pal = item.get("palette")
            if isinstance(pal, dict):
                st["palette"] = pal
            self._chart_layout_order.append(key)
            self._chart_layout_state[key] = st

    def _save_chart_layout(self) -> None:
        if self._settings is None:
            return
        from gui.widgets.chart_palette import palette_to_dict
        items = []
        for key in self._chart_layout_order:
            card = self._chart_cards_by_key.get(key)
            if card is None:
                continue
            item = {
                "key": key,
                "x": card.x(),
                "y": card.y(),
                "w": card.width(),
                "h": card.height(),
                "minimized": card.is_minimized(),
            }
            override = card.chart_palette_override()
            if override is not None:
                item["palette"] = palette_to_dict(override)
            items.append(item)
        blob = {"version": 2, "items": items}
        self._settings.setValue(DASHBOARD_CHART_LAYOUT, json.dumps(blob))

    def _load_chart_order(self) -> list[str]:
        """Return current order — used by ``_populate_chart_grid``."""
        return list(self._chart_layout_order)

    def _save_chart_order(self, order: list[str]) -> None:
        """Replace the current order (e.g. after Configure Charts).
        Per-card state is preserved for keys that are still selected;
        keys newly added via the dialog get their registry default."""
        new_states: dict[str, dict] = {}
        for k in order:
            if k not in CHART_BY_KEY:
                continue
            existing = self._chart_layout_state.get(k)
            new_states[k] = existing if existing else self._default_state_for(k)
        self._chart_layout_state = new_states
        self._chart_layout_order = [k for k in order if k in CHART_BY_KEY]
        self._save_chart_layout()

    # ---- card grid management ---------------------------------------------

    def _rebuild_cards(self) -> None:
        for card in self._card_widgets:
            self.cards_grid.removeWidget(card)
            card.deleteLater()
        self._card_widgets = []

        order = self._load_card_order()
        for i, key in enumerate(order):
            row, col = divmod(i, CARDS_PER_ROW)
            card = StatCard(self.cards_container)
            card.setProperty("metric_key", key)
            self.cards_grid.addWidget(card, row, col)
            self._card_widgets.append(card)

        for c in range(CARDS_PER_ROW):
            self.cards_grid.setColumnStretch(c, 1)

    def _on_configure(self) -> None:
        dlg = StatCardConfigDialog(
            self._load_card_order(), METRIC_REGISTRY, self,
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            new_order = dlg.selected_order()
            self._save_card_order(new_order)
            self._rebuild_cards()
            self.refresh()

    def _on_edit_goals(self) -> None:
        if self._settings is None:
            return
        dlg = GoalsDialog(self._settings, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.refresh()

    def _on_configure_charts(self) -> None:
        dlg = ChartCardConfigDialog(
            self._load_chart_order(), CHART_CARDS, self,
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._save_chart_order(dlg.selected_order())
            self._populate_canvas()
            self.refresh()

    # ---- chart card user actions (drag / resize / minimize) -------------

    def _on_card_layout_changed(self) -> None:
        """A card moved, resized, or toggled minimize. Capture the new
        per-card state and schedule a debounced save (drags fire many
        signals per second)."""
        for key, card in self._chart_cards_by_key.items():
            if card.parent() is not self.charts_canvas:
                continue
            self._chart_layout_state[key] = {
                "x": card.x(), "y": card.y(),
                "w": card.width(), "h": card.height(),
                "minimized": card.is_minimized(),
            }
        self._chart_save_timer.start()

    # ---- preset bar ------------------------------------------------------

    def _load_presets(self) -> list[dict]:
        if self._settings is None:
            return []
        raw = self._settings.value(DASHBOARD_CHART_PRESETS)
        if not isinstance(raw, str) or not raw.strip():
            return []
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return []
        if not isinstance(data, list):
            return []
        out: list[dict] = []
        for entry in data:
            if (
                isinstance(entry, dict)
                and isinstance(entry.get("name"), str)
                and isinstance(entry.get("items"), list)
            ):
                out.append({
                    "name": entry["name"],
                    "items": entry["items"],
                })
        return out

    def _save_presets(self, presets: list[dict]) -> None:
        if self._settings is None:
            return
        self._settings.setValue(
            DASHBOARD_CHART_PRESETS, json.dumps(presets),
        )
        self._settings.sync()

    def _current_preset_items(self) -> list[dict]:
        """Snapshot the current canvas layout as a JSON-serialisable
        list — same shape as the ``items`` field in the v2 layout
        blob so it can be restored verbatim. Per-card palette
        overrides ride along with the geometry."""
        from gui.widgets.chart_palette import palette_to_dict
        items: list[dict] = []
        for key in self._chart_layout_order:
            card = self._chart_cards_by_key.get(key)
            if card is None:
                continue
            item = {
                "key": key,
                "x": card.x(), "y": card.y(),
                "w": card.width(), "h": card.height(),
                "minimized": card.is_minimized(),
            }
            override = card.chart_palette_override()
            if override is not None:
                item["palette"] = palette_to_dict(override)
            items.append(item)
        return items

    def _rebuild_preset_bar(self) -> None:
        # Clear existing buttons.
        while self._presets_layout.count():
            item = self._presets_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        for preset in self._load_presets():
            btn = QPushButton(preset["name"], self)
            btn.setToolTip(
                f"Apply layout: {preset['name']}\n"
                "Right-click for rename / overwrite / delete"
            )
            btn.clicked.connect(
                lambda _checked=False, name=preset["name"]:
                    self._on_apply_preset(name)
            )
            btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            btn.customContextMenuRequested.connect(
                lambda _pos, name=preset["name"], b=btn:
                    self._on_preset_context_menu(b, name)
            )
            self._presets_layout.addWidget(btn)

    def _on_save_preset(self) -> None:
        if self._settings is None:
            return
        existing = {p["name"] for p in self._load_presets()}
        # Pre-fill with a sensible suggestion.
        suggestion = "My layout"
        n = 1
        while suggestion in existing:
            n += 1
            suggestion = f"My layout {n}"
        name, ok = QInputDialog.getText(
            self, "Save layout", "Preset name:", text=suggestion,
        )
        if not ok:
            return
        name = name.strip()
        if not name:
            return
        presets = self._load_presets()
        if name in {p["name"] for p in presets}:
            confirm = QMessageBox.question(
                self, "Overwrite preset?",
                f"A preset named '{name}' already exists. "
                "Overwrite it with the current layout?",
            )
            if confirm != QMessageBox.StandardButton.Yes:
                return
            presets = [p for p in presets if p["name"] != name]
        presets.append({"name": name, "items": self._current_preset_items()})
        self._save_presets(presets)
        self._rebuild_preset_bar()

    def _on_apply_preset(self, name: str) -> None:
        for preset in self._load_presets():
            if preset["name"] != name:
                continue
            # Hydrate the in-memory layout state from the preset's
            # items (same shape as a v2 blob's items) and re-populate
            # the canvas. This also persists as the new "current"
            # layout via _on_card_layout_changed at the end.
            self._chart_layout_order = []
            self._chart_layout_state = {}
            self._load_v2(preset["items"])
            self._populate_canvas()
            # Push fresh data into newly-shown cards.
            self.refresh()
            self._on_card_layout_changed()
            return

    def _on_preset_context_menu(self, btn: QPushButton, name: str) -> None:
        menu = QMenu(self)
        act_apply = QAction("Apply", menu)
        act_apply.triggered.connect(lambda: self._on_apply_preset(name))
        menu.addAction(act_apply)
        act_overwrite = QAction("Overwrite with current layout", menu)
        act_overwrite.triggered.connect(
            lambda: self._on_overwrite_preset(name)
        )
        menu.addAction(act_overwrite)
        act_rename = QAction("Rename…", menu)
        act_rename.triggered.connect(lambda: self._on_rename_preset(name))
        menu.addAction(act_rename)
        menu.addSeparator()
        act_delete = QAction("Delete", menu)
        act_delete.triggered.connect(lambda: self._on_delete_preset(name))
        menu.addAction(act_delete)
        menu.exec(btn.mapToGlobal(btn.rect().bottomLeft()))

    def _on_overwrite_preset(self, name: str) -> None:
        presets = self._load_presets()
        for p in presets:
            if p["name"] == name:
                p["items"] = self._current_preset_items()
                break
        self._save_presets(presets)

    def _on_rename_preset(self, name: str) -> None:
        new_name, ok = QInputDialog.getText(
            self, "Rename preset", "New name:", text=name,
        )
        if not ok:
            return
        new_name = new_name.strip()
        if not new_name or new_name == name:
            return
        presets = self._load_presets()
        if any(p["name"] == new_name for p in presets):
            QMessageBox.warning(
                self, "Duplicate name",
                f"A preset named '{new_name}' already exists.",
            )
            return
        for p in presets:
            if p["name"] == name:
                p["name"] = new_name
                break
        self._save_presets(presets)
        self._rebuild_preset_bar()

    def _on_delete_preset(self, name: str) -> None:
        confirm = QMessageBox.question(
            self, "Delete preset?",
            f"Delete the layout preset '{name}'?",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        presets = [p for p in self._load_presets() if p["name"] != name]
        self._save_presets(presets)
        self._rebuild_preset_bar()

    # ---- data / rendering --------------------------------------------------

    def refresh(
        self, preloaded_closed: Optional[list[dict]] = None,
    ) -> None:
        if preloaded_closed is not None:
            all_closed = preloaded_closed
        else:
            all_closed = db_manager.fetch_closed_trades(self._get_conn())
        # Goals + week strip are always evaluated against today's
        # calendar windows, not the user's selected range.
        self.goal_bars.refresh(all_closed)
        self.week_strip.refresh(all_closed)

        start, end = self.date_bar.current_range()
        filtered = filter_by_exit_date(all_closed, start, end)

        # Open positions are a "right now" view — independent of the
        # date filter. Pulled fresh on every refresh so the card stays
        # in sync with imports.
        try:
            open_trades = db_manager.fetch_open_trades(self._get_conn())
        except Exception:
            open_trades = []
        self.chart_open_positions.set_positions(open_trades)

        # Always render the populated state — even on a zero-trade
        # range. Cards / charts then explicitly show "0 trades / $0"
        # which is the right semantic, and the user gets immediate
        # visual feedback that the date change was applied.
        self.stack.setCurrentIndex(0)
        metrics = compute_metrics(filtered)
        self._update_cards(metrics)
        self._update_chart_cards(filtered, metrics)

        # Caption that confirms the dashboard reflects the current
        # date selection.
        self.range_caption.setText(
            f"Showing <b>{len(filtered)}</b> closed trade"
            f"{'' if len(filtered) == 1 else 's'} from "
            f"<b>{start.isoformat()}</b> to <b>{end.isoformat()}</b>"
        )

        # Equity / daily-bar charts are now first-class ChartCards
        # inside the configurable grid — keep them populated even on
        # zero-trade ranges (pyqtgraph just renders an empty plot
        # rather than a broken widget). The user can minimize or
        # deselect either card via Configure Charts.
        self.equity_curve.set_data(filtered)
        self.daily_bars.set_data(filtered)

    def _update_cards(self, metrics: TradeMetrics) -> None:
        for card in self._card_widgets:
            key = card.property("metric_key")
            mdef = METRIC_BY_KEY.get(key)
            if mdef is None:
                card.set_data("?", "—")
                continue
            value = mdef.fmt(metrics)
            color = mdef.color(metrics) if mdef.color else None
            card.set_data(mdef.label, value, color)

    def _update_chart_cards(
        self, trades: list[dict], metrics: TradeMetrics,
    ) -> None:
        # Winning vs losing donut.
        self.chart_winloss.set_data(metrics.win_count, metrics.loss_count)

        # Hold time W/L.
        self.chart_hold_wl.set_data(
            metrics.avg_hold_seconds_winners,
            metrics.avg_hold_seconds_losers,
        )

        # Avg W/L $.
        self.chart_avg_wl.set_data(
            metrics.avg_winner if metrics.win_count else None,
            metrics.avg_loser if metrics.loss_count else None,
        )

        # Largest gain / loss half-circle.
        self.chart_largest.set_gain_loss(
            metrics.largest_winner,
            abs(metrics.largest_loser),
        )

        # Day-of-week bars (Mon–Fri ordered).
        self.chart_dow.set_rows(
            _report_rows_to_label_value(by_day_of_week(trades))
        )

        # Duration bars (Intraday / Multiday).
        self.chart_duration.set_rows(
            _report_rows_to_label_value(by_duration(trades))
        )

        # Price bucket bars.
        self.chart_price.set_rows(
            _report_rows_to_label_value(by_ticker_price(trades))
        )

        # Hour-of-day bars.
        self.chart_hour.set_rows(
            _report_rows_to_label_value(by_hour_of_day(trades))
        )

        # Total fees.
        self.chart_fees.set_value(metrics.total_commission)

        # Profit factor gauge — Inf clamps to cap.
        pf = metrics.profit_factor
        if math.isinf(pf):
            pf_for_gauge = float("inf")
        else:
            pf_for_gauge = pf
        self.chart_pf.set_profit_factor(
            None if pf_for_gauge == 0 else pf_for_gauge,
            cap=3.0,
        )

        # Tag breakdown — uses DB join.
        try:
            tag_rows = tag_mod.tag_breakdown_rows(self._get_conn(), trades)
        except Exception:
            tag_rows = []
        self.chart_tags.set_rows(tag_rows)

        # Risk-adjusted ratio cards. None (no closed trades) → em dash;
        # math.inf (upside, zero downside) → ∞ via the formatter.
        self.chart_sortino.set_value(metrics.adjusted_sortino)
        self.chart_gain_to_pain.set_value(metrics.gain_to_pain)
