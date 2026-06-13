"""Persistent default char format for the rich-text editor.

The Journal + Briefs editors both apply these defaults when the editor
is empty (first launch, "new empty brief", an existing entry that was
cleared). Populated through the "Set as default" button on the rich-
text toolbar, so the user can lock in their preferred size/weight/color.

Storage is plain QSettings keys — no JSON payload to future-proof. The
baseline (``INITIAL_DEFAULT_SIZE = 13``) sits 3 pt above the 10 pt QSS
baseline, matching the user's stated preference for slightly larger
text on first launch.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import QSettings
from PySide6.QtGui import QColor, QFont, QTextCharFormat
from PySide6.QtWidgets import QTextEdit

from gui.settings_keys import (
    EDITOR_DEFAULT_BOLD,
    EDITOR_DEFAULT_COLOR,
    EDITOR_DEFAULT_ITALIC,
    EDITOR_DEFAULT_SIZE,
    EDITOR_DEFAULT_UNDERLINE,
)

# Baseline on first launch — 3 pt above the QSS 10 pt global baseline.
INITIAL_DEFAULT_SIZE = 13.0


@dataclass
class EditorDefaults:
    """User-chosen default char format for empty editors."""
    size: float = INITIAL_DEFAULT_SIZE
    color: str = ""       # "" → inherit from stylesheet / palette
    bold: bool = False
    italic: bool = False
    underline: bool = False


def _coerce_bool(v: object) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes", "on")
    if isinstance(v, (int, float)):
        return bool(v)
    return False


def _coerce_float(v: object, fallback: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return fallback


def load(settings: Optional[QSettings]) -> EditorDefaults:
    """Return the persisted defaults, or the baseline on first launch."""
    d = EditorDefaults()
    if settings is None:
        return d
    d.size = _coerce_float(
        settings.value(EDITOR_DEFAULT_SIZE, d.size), d.size,
    )
    color = settings.value(EDITOR_DEFAULT_COLOR, "")
    d.color = color if isinstance(color, str) else ""
    d.bold = _coerce_bool(settings.value(EDITOR_DEFAULT_BOLD, False))
    d.italic = _coerce_bool(settings.value(EDITOR_DEFAULT_ITALIC, False))
    d.underline = _coerce_bool(settings.value(EDITOR_DEFAULT_UNDERLINE, False))
    # Clamp to a sane range — a stored 0 or negative would silently
    # invisibilize all new text.
    if not (4.0 <= d.size <= 200.0):
        d.size = INITIAL_DEFAULT_SIZE
    return d


def save(settings: QSettings, d: EditorDefaults) -> None:
    settings.setValue(EDITOR_DEFAULT_SIZE, float(d.size))
    settings.setValue(EDITOR_DEFAULT_COLOR, d.color or "")
    settings.setValue(EDITOR_DEFAULT_BOLD, bool(d.bold))
    settings.setValue(EDITOR_DEFAULT_ITALIC, bool(d.italic))
    settings.setValue(EDITOR_DEFAULT_UNDERLINE, bool(d.underline))
    # Flush to disk so a crash between save and the next load can't
    # lose the user's stated preference.
    settings.sync()


def capture(editor: QTextEdit) -> EditorDefaults:
    """Read the editor's current insertion-point format into an
    ``EditorDefaults`` — used by the toolbar's "Set as default" action."""
    fmt = editor.currentCharFormat()
    size = fmt.fontPointSize()
    if size <= 0:
        size = editor.fontPointSize()
    if size <= 0:
        size = editor.font().pointSizeF() or INITIAL_DEFAULT_SIZE
    fg = fmt.foreground().color()
    # Qt returns a black default even when no explicit color was set;
    # fall back to "inherit" whenever the alpha is 0 (no brush).
    color_str = ""
    if fmt.foreground().style() != 0 and fg.isValid() and fg.alpha() != 0:
        color_str = fg.name()
    return EditorDefaults(
        size=float(size),
        color=color_str,
        bold=fmt.fontWeight() >= QFont.Weight.Bold.value,
        italic=fmt.fontItalic(),
        underline=fmt.fontUnderline(),
    )


# Fallback foreground when the user hasn't explicitly saved a default
# color. Matches the QSS body ``color: #e0e0e0`` so empty-default
# editors render the same way as before this fix — the difference is
# the cursor format now carries an *explicit* brush, surviving the
# delete-all → type sequence where Qt would otherwise drop the
# foreground entirely.
#
# Hardcoded because ``QPalette.text()`` is not modified by the
# stylesheet on Qt for Python — QSS only affects the *render path*,
# leaving the palette returning whatever the system theme picked
# (black on most desktops). Reading from the palette would bake black
# into the cursor format and make typed text invisible against the
# dark background.
DEFAULT_FALLBACK_COLOR = "#e0e0e0"


def _resolve_color(d: EditorDefaults, editor: Optional[QTextEdit]) -> str:
    """Return the hex color to bake into the cursor format.

    Empty ``d.color`` falls back to ``DEFAULT_FALLBACK_COLOR`` so the
    cursor format always carries an explicit ForegroundBrush. That
    matters because Qt drops the foreground brush from the cursor
    format whenever the document empties (select-all → delete) — once
    that happens, the next typed character inherits the QPalette
    default (black on most systems) regardless of what the
    ``QTextDocument.defaultFont`` carries. Forcing every reseed to
    embed a real color keeps post-deletion typing visually consistent.

    The ``editor`` argument is currently unused but kept on the API in
    case a future theme system needs to derive the fallback from the
    editor's palette dynamically.
    """
    if d.color:
        return d.color
    return DEFAULT_FALLBACK_COLOR


def build_char_format(
    d: EditorDefaults, *, editor: Optional[QTextEdit] = None,
) -> QTextCharFormat:
    """Materialize a ``QTextCharFormat`` matching the saved defaults.

    Shared between ``apply_to_editor`` (sets the cursor format so the
    next keystroke picks up defaults), the toolbar's "Return to
    default" action (applies defaults to an existing selection), and
    the toolbar's "Clear formatting" (resets a selection to defaults
    rather than wiping every property).

    Always sets size and foreground so a downstream
    ``setCharFormat`` / ``setCurrentCharFormat`` produces a cursor
    that survives delete-all-then-type without losing color or size.
    """
    fmt = QTextCharFormat()
    size = d.size if d.size > 0 else INITIAL_DEFAULT_SIZE
    fmt.setFontPointSize(size)
    fmt.setFontWeight(
        QFont.Weight.Bold.value if d.bold else QFont.Weight.Normal.value
    )
    fmt.setFontItalic(d.italic)
    fmt.setFontUnderline(d.underline)
    color = _resolve_color(d, editor)
    c = QColor(color)
    if c.isValid():
        fmt.setForeground(c)
    return fmt


def apply_to_editor(editor: QTextEdit, d: EditorDefaults) -> None:
    """Apply defaults to an empty editor so the next keystroke uses them.

    Sets both the widget-level font (so the empty-state cursor shows
    at the right size) and the current char format (so new text picks
    up weight/italic/underline/color). The cursor format always
    carries an explicit ForegroundBrush — see ``_resolve_color`` for
    why falling back to the QPalette text color matters.
    """
    # Widget-level font — affects the initial caret height in empty docs
    # and becomes the ``QTextDocument.defaultFont`` for un-styled runs.
    wfont = QFont(editor.font())
    if d.size > 0:
        wfont.setPointSizeF(d.size)
    wfont.setBold(d.bold)
    wfont.setItalic(d.italic)
    wfont.setUnderline(d.underline)
    editor.setFont(wfont)

    editor.setCurrentCharFormat(build_char_format(d, editor=editor))
