"""Per-widget chart palette + change-notification hub.

Every dashboard chart resolves its colors through ``get_palette(self)``.
That walks up the widget tree looking for a ``ChartCard`` with a
per-card palette override; if none is found, falls back to the
app-wide default palette below. This means a right-click "Customize
chart colors…" on a single card changes only that card's colors —
siblings keep the shared default.

Five user-facing roles:
    * positive   — green for wins, profit, up-bars
    * negative   — red for losses, drawdown, down-bars
    * axis       — axis ticks + frame
    * label      — axis text + secondary labels
    * background — chart canvas behind the data

The *global* palette drives the app-wide QSS theme (panel/hover tones
derived from ``background``) and is the fallback every card starts
from. Per-card overrides are stored alongside the card's geometry in
the layout JSON blob.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from typing import Optional

from PySide6.QtCore import QObject, QSettings, Signal

from gui.settings_keys import DASHBOARD_CHART_PALETTE


@dataclass(frozen=True)
class ChartPalette:
    positive: str = "#00ff8a"
    negative: str = "#790eb8"
    axis: str = "#3c3c3c"
    label: str = "#04f5fe"
    background: str = "#0f0f0f"


# TradeBook's default theme — purple negatives, bright green positives,
# cyan labels, dark neutrals. Returned by ``get_palette()`` when no
# per-card override exists and no global customization has been saved.
DEFAULT_PALETTE = ChartPalette()


class _PaletteHub(QObject):
    palette_changed = Signal()


_hub: Optional[_PaletteHub] = None
_settings: Optional[QSettings] = None
_cache: Optional[ChartPalette] = None


def palette_hub() -> _PaletteHub:
    global _hub
    if _hub is None:
        _hub = _PaletteHub()
    return _hub


def init_palette(settings: QSettings) -> None:
    """Wire the palette to a QSettings store. Call once at startup."""
    global _settings, _cache
    _settings = settings
    _cache = _load_from_settings()


def _load_from_settings() -> ChartPalette:
    if _settings is None:
        return DEFAULT_PALETTE
    raw = _settings.value(DASHBOARD_CHART_PALETTE)
    if not raw:
        return DEFAULT_PALETTE
    try:
        data = json.loads(raw) if isinstance(raw, str) else {}
    except (json.JSONDecodeError, TypeError):
        return DEFAULT_PALETTE
    fields = {k: data[k] for k in DEFAULT_PALETTE.__dataclass_fields__ if k in data}
    return replace(DEFAULT_PALETTE, **fields)


def _walk_override(widget) -> Optional[ChartPalette]:
    """Walk up the parent chain looking for a ``ChartCard`` that has a
    per-card palette override set. Returns that palette, or ``None`` if
    no overriding ancestor exists.

    Duck-typed on the ``chart_palette_override()`` accessor so we don't
    have to import ``ChartCard`` here (would create a cycle).
    """
    w = widget
    while w is not None:
        getter = getattr(w, "chart_palette_override", None)
        if callable(getter):
            override = getter()
            if override is not None:
                return override
            # The first ChartCard we find is the "owning" card; if it
            # has no override, stop — ancestors aren't cards.
            return None
        w = w.parent() if hasattr(w, "parent") else None
    return None


def get_palette(widget=None) -> ChartPalette:
    """Resolve the effective palette for ``widget``.

    Resolution order:
        1. Per-card override set via ``set_chart_palette_override``
        2. ``DEFAULT_PALETTE`` (the TradeBook theme)

    A user's legacy global override (saved to QSettings before this
    rewrite) is intentionally NOT consulted — the default is now the
    TradeBook theme, always, unless a specific card has customized it.
    """
    if widget is not None:
        override = _walk_override(widget)
        if override is not None:
            return override
    return DEFAULT_PALETTE


def set_palette(new: ChartPalette) -> None:
    """Replace the global palette. Used by the app-wide theme flow;
    per-card dialogs should call ``ChartCard.set_chart_palette_override``
    instead."""
    global _cache
    _cache = new
    if _settings is not None:
        _settings.setValue(DASHBOARD_CHART_PALETTE, json.dumps(asdict(new)))
        _settings.sync()
    palette_hub().palette_changed.emit()


def reset_palette() -> None:
    """Revert the global palette to the TradeBook default."""
    set_palette(DEFAULT_PALETTE)


def palette_from_dict(data: dict) -> ChartPalette:
    """Build a ChartPalette from a JSON-serialisable dict, filling any
    missing fields from DEFAULT_PALETTE. Used when hydrating per-card
    overrides from the layout blob."""
    fields = {
        k: data[k]
        for k in DEFAULT_PALETTE.__dataclass_fields__
        if k in data and isinstance(data[k], str)
    }
    return replace(DEFAULT_PALETTE, **fields)


def palette_to_dict(palette: ChartPalette) -> dict:
    return asdict(palette)
