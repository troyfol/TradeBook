"""App-wide theme builder driven by the chart palette.

The static ``dark_theme.qss`` file is treated as a template — its
hardcoded color literals are rewritten on the fly from the user's
chart palette plus a set of derived gradations (lighter/darker
variants of the background, axis, and label colors). The result is
applied to ``QApplication`` once on startup and again whenever
``palette_hub().palette_changed`` fires.

Why derive instead of asking the user for ten colors: most app
surfaces (panels, hover states, borders) are perceptually variations
of the same neutral, so deriving them from one ``background`` keeps
the look coherent. The user picks intent (dark vs. light, warm vs.
cool); we fill in the exact tones.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QColor

from gui.widgets.chart_palette import ChartPalette, get_palette


# Hardcoded literals in the static QSS, mapped to the palette role
# they represent. Order matters: longer / more specific strings first
# so a substring like ``#1e1e1e`` doesn't get clobbered by a partial
# match against ``#1e1e1ee0`` (none in our QSS but defensive).
def _build_substitutions(pal: ChartPalette) -> dict[str, str]:
    bg = QColor(pal.background)
    label = QColor(pal.label)
    axis = QColor(pal.axis)

    # Non-chart surfaces sit one tone above the user's chart background
    # so charts pop off the page instead of blending into it. The chart
    # canvas itself is still painted with the raw ``pal.background``
    # via each widget's ``setBackground`` / ``fillRect`` call.
    app_bg = bg.lighter(140)
    bg_panel = app_bg.lighter(115).name()  # cards, inputs
    bg_alt = app_bg.lighter(135).name()    # secondary surface (tabs, status bar)
    bg_hover = app_bg.lighter(165).name()  # hover state
    bg_inset = app_bg.lighter(125).name()  # alternating rows
    bg_disabled = app_bg.lighter(110).name()

    border = axis.name()
    border_light = axis.lighter(140).name()

    # Body text stays neutral-light regardless of the user's "Labels
    # & ticks" palette choice — otherwise picking a blue label color
    # bleeds into every QTextEdit / QLabel / pasted-content body text
    # across the app. Axis ticks and hint labels still follow
    # ``label`` via the #b0b0b0 / #909090 substitutions below.
    text_primary = "#e0e0e0"
    text_secondary = label.name()                # subtitles, hints
    text_dim = label.darker(150).name()          # disabled, placeholder

    # Selection / accent — derived from positive so it stays in family
    # but kept on the cool/dim end so it doesn't overwhelm the data.
    pos = QColor(pal.positive)
    accent = pos.darker(140).name()
    accent_dim = pos.darker(260).name()

    neg = QColor(pal.negative).name()

    return {
        # backgrounds — main window steps up a tone from the chart bg
        # so chart cards pop instead of blending.
        "#1e1e1e": app_bg.name(),
        "#252525": bg_panel,
        "#2a2a2a": bg_inset,
        "#2d2d2d": bg_alt,
        "#383838": bg_hover,
        "#4a4a4a": bg_hover,
        # borders
        "#3c3c3c": border,
        "#555555": border_light,
        # text
        "#e0e0e0": text_primary,
        "#b0b0b0": text_secondary,
        "#909090": text_secondary,
        "#606060": text_dim,
        # accents (selection + focus ring)
        "#094771": accent_dim,
        "#00A3FF": accent,
        # negative-state error label
        "#FF1744": neg,
    }


def build_qss(static_qss: str, pal: ChartPalette) -> str:
    """Substitute palette-derived colors into the static QSS."""
    out = static_qss
    for old, new in _build_substitutions(pal).items():
        # Case-insensitive replace so #FF1744 / #ff1744 both match.
        out = _ireplace(out, old, new)
    return out


def _ireplace(haystack: str, needle: str, repl: str) -> str:
    lo = haystack.lower()
    n = needle.lower()
    out: list[str] = []
    i = 0
    while True:
        j = lo.find(n, i)
        if j < 0:
            out.append(haystack[i:])
            return "".join(out)
        out.append(haystack[i:j])
        out.append(repl)
        i = j + len(needle)


def load_static_qss(qss_path: Path) -> str:
    if qss_path.exists():
        return qss_path.read_text(encoding="utf-8")
    return ""


def apply_theme(app, qss_path: Path) -> None:
    """Apply palette-derived theme to the app and re-apply on changes."""
    base = load_static_qss(qss_path)

    def refresh() -> None:
        app.setStyleSheet(build_qss(base, get_palette()))

    refresh()
    from gui.widgets.chart_palette import palette_hub
    palette_hub().palette_changed.connect(refresh)
