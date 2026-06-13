"""Rich-text formatting toolbar for QTextEdit-backed widgets.

Shared by the Journal and Briefs tabs so the two feel like the same
editor. Wraps a target ``QTextEdit`` and mirrors its cursor / selection
state onto toggle buttons (bold is highlighted when the cursor sits in
bold text, etc.). All formatting ops respect the standard semantics:

    * If the editor has a selection, the format is applied to it.
    * Otherwise, the format is set on the current char format so the
      next typed characters pick it up.

Qt's built-in ``QTextEdit`` shortcuts (Ctrl+B / Ctrl+I / Ctrl+U) remain
active — this widget is purely an additional visual surface.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QSettings, Qt, Signal
from PySide6.QtGui import (
    QColor, QFont, QIcon, QKeySequence, QPixmap, QShortcut,
    QTextBlockFormat, QTextCharFormat, QTextCursor, QTextListFormat,
)
from PySide6.QtWidgets import (
    QColorDialog, QComboBox, QFrame, QHBoxLayout, QLabel, QMessageBox,
    QTextEdit, QToolButton, QWidget,
)

from gui.widgets import editor_defaults


FONT_SIZES = [8, 9, 10, 11, 12, 14, 16, 18, 20, 24, 28, 32, 36, 48]

# Heading-level font sizes (in pt). H1 is ~22pt, H2 ~18pt, H3 ~14pt.
# Body == level 0 strips the heading flag and resets to the editor's
# saved default size (so toggling H1 on then off doesn't leave the
# paragraph stuck at 22pt).
HEADING_SIZES: dict[int, float] = {0: 0.0, 1: 22.0, 2: 18.0, 3: 14.0}

# Default preset palette for the quick color picker. Click-and-hold
# opens the full QColorDialog; a single click applies the last-used
# color (initialized to the first entry).
TEXT_COLOR_PRESETS = [
    "#e0e0e0",  # default light (matches the editor foreground)
    "#FF1744",  # red — loss / warning
    "#00C853",  # green — win
    "#00A3FF",  # blue — neutral emphasis
    "#FFB300",  # amber — caution
    "#AB47BC",  # purple
]
HIGHLIGHT_COLOR_PRESETS = [
    "transparent",
    "#FFF59D",  # soft yellow
    "#C5E1A5",  # soft green
    "#FFAB91",  # soft red
    "#90CAF9",  # soft blue
    "#E1BEE7",  # soft purple
]


def _color_swatch(color: QColor, size: int = 14) -> QIcon:
    """Build a small filled-square icon for a color-picker button."""
    px = QPixmap(size, size)
    if color.alpha() == 0:
        px.fill(Qt.GlobalColor.transparent)
    else:
        px.fill(color)
    return QIcon(px)


class RichTextToolbar(QWidget):
    """Bold/italic/underline/strike + size + colors + lists for a QTextEdit."""

    def __init__(
        self,
        editor: QTextEdit,
        parent=None,
        *,
        settings: Optional[QSettings] = None,
    ):
        super().__init__(parent)
        self._editor = editor
        self._settings = settings
        self._syncing = False
        # Seed the text-color swatch from the saved default so the "A"
        # button reflects the current default color on first paint. A
        # user who set "18pt bold red" and relaunches should see a red
        # swatch, not the stock light-gray.
        self._text_color = QColor(TEXT_COLOR_PRESETS[0])
        if self._settings is not None:
            saved = editor_defaults.load(self._settings).color
            if saved:
                c = QColor(saved)
                if c.isValid():
                    self._text_color = c
        # Highlight starts at "transparent" so clicking the button the
        # first time clears any stray background rather than painting a
        # random color.
        self._highlight_color = QColor(0, 0, 0, 0)

        # ---- bold / italic / underline / strikethrough ---------------
        self.btn_bold = self._make_toggle("B", "Bold (Ctrl+B)", bold=True)
        self.btn_bold.toggled.connect(self._on_bold)

        self.btn_italic = self._make_toggle("I", "Italic (Ctrl+I)", italic=True)
        self.btn_italic.toggled.connect(self._on_italic)

        self.btn_underline = self._make_toggle(
            "U", "Underline (Ctrl+U)", underline=True,
        )
        self.btn_underline.toggled.connect(self._on_underline)

        self.btn_strike = self._make_toggle(
            "S", "Strikethrough (Ctrl+Shift+S)", strike=True,
        )
        self.btn_strike.toggled.connect(self._on_strike)

        # ---- heading buttons -----------------------------------------
        # Toggling a heading sets the QTextBlockFormat heading level
        # (so the outline pane on the Briefs / Strategies tabs can
        # extract it via document.findBlockByNumber +
        # blockFormat().headingLevel()) and applies a matching point
        # size + bold so the user sees the visual change immediately.
        # Click the active heading button again to revert to body text.
        # Single-digit labels because the parent toolbar's fixed-width
        # toggle buttons clip wider strings ("H1" was rendering blank
        # on the user's display).
        self.btn_h1 = self._make_heading_btn(1, "1", "Heading 1")
        self.btn_h2 = self._make_heading_btn(2, "2", "Heading 2")
        self.btn_h3 = self._make_heading_btn(3, "3", "Heading 3")

        # ---- font size combo -----------------------------------------
        self.combo_size = QComboBox(self)
        self.combo_size.setEditable(True)
        self.combo_size.setToolTip("Font size (pt)")
        self.combo_size.setFixedWidth(64)
        for s in FONT_SIZES:
            self.combo_size.addItem(str(s), s)
        self.combo_size.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.combo_size.activated.connect(self._on_size_activated)
        self.combo_size.lineEdit().returnPressed.connect(
            self._on_size_committed
        )

        # ---- text + highlight color pickers --------------------------
        self.btn_text_color = QToolButton(self)
        self.btn_text_color.setText("A")
        self.btn_text_color.setToolTip(
            "Text color — click to apply; right-click to choose"
        )
        self.btn_text_color.setPopupMode(
            QToolButton.ToolButtonPopupMode.DelayedPopup,
        )
        self.btn_text_color.clicked.connect(self._apply_text_color)
        self.btn_text_color.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu,
        )
        self.btn_text_color.customContextMenuRequested.connect(
            lambda _pt: self._pick_text_color()
        )

        self.btn_highlight = QToolButton(self)
        self.btn_highlight.setText("abc")
        self.btn_highlight.setToolTip(
            "Highlight color — click to apply; right-click to choose"
        )
        self.btn_highlight.clicked.connect(self._apply_highlight_color)
        self.btn_highlight.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu,
        )
        self.btn_highlight.customContextMenuRequested.connect(
            lambda _pt: self._pick_highlight_color()
        )
        self._refresh_color_swatches()

        # ---- lists ---------------------------------------------------
        self.btn_bullet = QToolButton(self)
        self.btn_bullet.setText("• List")
        self.btn_bullet.setCheckable(True)
        self.btn_bullet.setToolTip("Bulleted list")
        self.btn_bullet.clicked.connect(
            lambda _c=False: self._toggle_list(QTextListFormat.Style.ListDisc)
        )

        self.btn_numbered = QToolButton(self)
        self.btn_numbered.setText("1. List")
        self.btn_numbered.setCheckable(True)
        self.btn_numbered.setToolTip("Numbered list")
        self.btn_numbered.clicked.connect(
            lambda _c=False: self._toggle_list(
                QTextListFormat.Style.ListDecimal,
            )
        )

        # ---- horizontal separator ------------------------------------
        # Inserts a full-width horizontal rule spanning the editor — the
        # clean-line equivalent of typing out a row of tildes to divide
        # sections of a long brief / strategy page.
        self.btn_separator = QToolButton(self)
        self.btn_separator.setText("─ Line")
        self.btn_separator.setToolTip(
            "Insert a horizontal separator across the page"
        )
        self.btn_separator.clicked.connect(self._insert_separator)

        # ---- clear formatting ----------------------------------------
        self.btn_clear = QToolButton(self)
        self.btn_clear.setText("Clear")
        self.btn_clear.setToolTip(
            "Remove formatting from the current selection"
        )
        self.btn_clear.clicked.connect(self._clear_formatting)

        # ---- set / return-to default ---------------------------------
        # Both buttons are gated on having a QSettings store. "Set as
        # default" saves the current cursor format; "Return to default"
        # applies the saved default to the current selection (or the
        # cursor position for future typing).
        self.btn_set_default: Optional[QToolButton] = None
        self.btn_reset_to_default: Optional[QToolButton] = None
        if self._settings is not None:
            self.btn_set_default = QToolButton(self)
            self.btn_set_default.setText("Set as default")
            self.btn_set_default.setToolTip(
                "Save the current format (size / weight / color / …) "
                "as the default for new entries"
            )
            self.btn_set_default.clicked.connect(self._save_as_default)

            self.btn_reset_to_default = QToolButton(self)
            self.btn_reset_to_default.setText("Return to default")
            self.btn_reset_to_default.setToolTip(
                "Reset the selected text to the saved default format "
                "(or just the next typed character if nothing is selected)"
            )
            self.btn_reset_to_default.clicked.connect(self._return_to_default)

        # ---- layout --------------------------------------------------
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        for w in (self.btn_bold, self.btn_italic, self.btn_underline,
                  self.btn_strike):
            layout.addWidget(w)
        layout.addWidget(self._sep())
        for w in (self.btn_h1, self.btn_h2, self.btn_h3):
            layout.addWidget(w)
        layout.addWidget(self._sep())
        layout.addWidget(QLabel("Size:", self))
        layout.addWidget(self.combo_size)
        layout.addWidget(self._sep())
        layout.addWidget(self.btn_text_color)
        layout.addWidget(self.btn_highlight)
        layout.addWidget(self._sep())
        layout.addWidget(self.btn_bullet)
        layout.addWidget(self.btn_numbered)
        layout.addWidget(self._sep())
        layout.addWidget(self.btn_separator)
        layout.addWidget(self._sep())
        layout.addWidget(self.btn_clear)
        if self.btn_set_default is not None:
            layout.addWidget(self._sep())
            layout.addWidget(self.btn_set_default)
        if self.btn_reset_to_default is not None:
            layout.addWidget(self.btn_reset_to_default)
        layout.addStretch()

        # Shortcut — Ctrl+B/I/U are already handled by QTextEdit; add
        # Ctrl+Shift+S for strikethrough to match common conventions.
        self._sc_strike = QShortcut(
            QKeySequence("Ctrl+Shift+S"), self._editor,
        )
        self._sc_strike.activated.connect(
            lambda: self.btn_strike.toggle()
        )

        # Sync toolbar state whenever the cursor / format changes.
        self._editor.currentCharFormatChanged.connect(
            lambda _fmt: self._sync_from_editor()
        )
        self._editor.cursorPositionChanged.connect(self._sync_from_editor)
        self._sync_from_editor()

    # ---- helpers ----------------------------------------------------------

    def _sep(self) -> QFrame:
        line = QFrame(self)
        line.setFrameShape(QFrame.Shape.VLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        line.setFixedWidth(8)
        return line

    def _make_heading_btn(
        self, level: int, label: str, tip: str,
    ) -> QToolButton:
        btn = QToolButton(self)
        # Force text-only display so the QSS / Qt style heuristics can't
        # silently fall back to icon-only rendering (which was hiding
        # the heading-button labels entirely on the user's display).
        btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        btn.setText(label)
        btn.setCheckable(True)
        btn.setToolTip(tip)
        btn.setFixedWidth(28)
        font = QFont(btn.font())
        font.setBold(True)
        btn.setFont(font)
        btn.clicked.connect(
            lambda _checked=False, lvl=level: self._toggle_heading(lvl)
        )
        return btn

    def _toggle_heading(self, level: int) -> None:
        """Toggle the heading level on the cursor's current block.

        Clicking the active heading button reverts the block to body
        text. The block format heading level is what the Strategies
        tab's outline pane reads, so users only need to remember the
        toolbar buttons — no separate "make this a heading" command.
        """
        if self._syncing:
            return
        cursor = self._editor.textCursor()
        block_fmt = cursor.blockFormat()
        current_level = block_fmt.headingLevel()
        new_level = 0 if current_level == level else level
        cursor.beginEditBlock()
        try:
            block_fmt = QTextBlockFormat(block_fmt)
            block_fmt.setHeadingLevel(new_level)
            cursor.mergeBlockFormat(block_fmt)
            # Apply matching char format to the whole block so the
            # font size + weight reflect the heading level. We re-
            # select the block first so the format hits every char,
            # not just the cursor position.
            sel = QTextCursor(cursor)
            sel.movePosition(QTextCursor.MoveOperation.StartOfBlock)
            sel.movePosition(
                QTextCursor.MoveOperation.EndOfBlock,
                QTextCursor.MoveMode.KeepAnchor,
            )
            char_fmt = QTextCharFormat()
            if new_level == 0:
                # Body — strip heading-introduced size/weight by
                # re-applying the saved default char format.
                if self._settings is not None:
                    d = editor_defaults.load(self._settings)
                    char_fmt = editor_defaults.build_char_format(
                        d, editor=self._editor,
                    )
                else:
                    char_fmt.setFontPointSize(0.0)
                    char_fmt.setFontWeight(QFont.Weight.Normal.value)
            else:
                char_fmt.setFontPointSize(HEADING_SIZES[new_level])
                char_fmt.setFontWeight(QFont.Weight.Bold.value)
            if sel.hasSelection():
                sel.mergeCharFormat(char_fmt)
            self._editor.mergeCurrentCharFormat(char_fmt)
        finally:
            cursor.endEditBlock()
        self._sync_from_editor()

    def _make_toggle(
        self,
        label: str,
        tip: str,
        *,
        bold: bool = False,
        italic: bool = False,
        underline: bool = False,
        strike: bool = False,
    ) -> QToolButton:
        btn = QToolButton(self)
        btn.setText(label)
        btn.setCheckable(True)
        btn.setToolTip(tip)
        btn.setFixedWidth(28)
        font = QFont(btn.font())
        if bold:
            font.setBold(True)
        if italic:
            font.setItalic(True)
        if underline:
            font.setUnderline(True)
        if strike:
            font.setStrikeOut(True)
        btn.setFont(font)
        return btn

    def _merge_format(self, fmt: QTextCharFormat) -> None:
        """Apply a char format to the current selection, or seed it
        into the insertion point if there's no selection."""
        cursor = self._editor.textCursor()
        if cursor.hasSelection():
            cursor.mergeCharFormat(fmt)
        self._editor.mergeCurrentCharFormat(fmt)

    def _refresh_color_swatches(self) -> None:
        self.btn_text_color.setIcon(_color_swatch(self._text_color))
        self.btn_highlight.setIcon(_color_swatch(self._highlight_color))

    # ---- character-format handlers --------------------------------------

    def _on_bold(self, checked: bool) -> None:
        if self._syncing:
            return
        self._editor.setFontWeight(
            QFont.Weight.Bold if checked else QFont.Weight.Normal,
        )

    def _on_italic(self, checked: bool) -> None:
        if self._syncing:
            return
        self._editor.setFontItalic(checked)

    def _on_underline(self, checked: bool) -> None:
        if self._syncing:
            return
        self._editor.setFontUnderline(checked)

    def _on_strike(self, checked: bool) -> None:
        if self._syncing:
            return
        fmt = QTextCharFormat()
        fmt.setFontStrikeOut(checked)
        self._merge_format(fmt)

    def _on_size_activated(self, idx: int) -> None:
        if self._syncing:
            return
        try:
            size = float(self.combo_size.itemData(idx))
        except (TypeError, ValueError):
            return
        if size > 0:
            self._editor.setFontPointSize(size)

    def _on_size_committed(self) -> None:
        if self._syncing:
            return
        try:
            size = float(self.combo_size.currentText())
        except ValueError:
            return
        if 4 <= size <= 200:
            self._editor.setFontPointSize(size)

    # ---- color pickers ---------------------------------------------------

    def _apply_text_color(self) -> None:
        self._editor.setTextColor(self._text_color)

    def _pick_text_color(self) -> None:
        chosen = QColorDialog.getColor(
            self._text_color, self, "Choose text color",
        )
        if chosen.isValid():
            self._text_color = chosen
            self._refresh_color_swatches()
            self._apply_text_color()

    def _apply_highlight_color(self) -> None:
        fmt = QTextCharFormat()
        if self._highlight_color.alpha() == 0:
            # "transparent" clears the background.
            fmt.setBackground(Qt.GlobalColor.transparent)
        else:
            fmt.setBackground(self._highlight_color)
        self._merge_format(fmt)

    def _pick_highlight_color(self) -> None:
        chosen = QColorDialog.getColor(
            self._highlight_color
            if self._highlight_color.alpha() != 0
            else QColor("#FFF59D"),
            self,
            "Choose highlight color",
            QColorDialog.ColorDialogOption.ShowAlphaChannel,
        )
        if chosen.isValid():
            self._highlight_color = chosen
            self._refresh_color_swatches()
            self._apply_highlight_color()

    # ---- lists ----------------------------------------------------------

    def _toggle_list(self, style: QTextListFormat.Style) -> None:
        cursor = self._editor.textCursor()
        cursor.beginEditBlock()
        current = cursor.currentList()
        if current is not None and current.format().style() == style:
            # Unlink the block from its list by clearing the object idx.
            block_fmt = cursor.blockFormat()
            block_fmt.setObjectIndex(-1)
            cursor.setBlockFormat(block_fmt)
        else:
            list_fmt = QTextListFormat()
            list_fmt.setStyle(style)
            cursor.createList(list_fmt)
        cursor.endEditBlock()
        self._sync_from_editor()

    # ---- horizontal separator -------------------------------------------

    def _insert_separator(self) -> None:
        """Insert a full-width horizontal rule at the cursor.

        Qt renders an HTML ``<hr>`` as a block-spanning horizontal line,
        giving a clean section divider (the role a hand-typed row of
        tildes used to play) without the user typing any characters. The
        cursor is left on a fresh line below the rule, re-seeded with the
        editor's saved default size / color so typing simply continues in
        the user's chosen style.
        """
        cursor = self._editor.textCursor()
        cursor.beginEditBlock()
        try:
            cursor.insertHtml("<hr>")
        finally:
            cursor.endEditBlock()
        self._editor.setTextCursor(cursor)
        self._apply_default_cursor_format()

    def _apply_default_cursor_format(self) -> None:
        """Reset the cursor's char format to the saved editor defaults so
        the next typed character carries the user's size + color."""
        d = (
            editor_defaults.load(self._settings)
            if self._settings is not None
            else editor_defaults.EditorDefaults()
        )
        self._editor.setCurrentCharFormat(
            editor_defaults.build_char_format(d, editor=self._editor)
        )

    # ---- clear ----------------------------------------------------------

    def _clear_formatting(self) -> None:
        """Reset the selection (or future typing) to the user's saved
        defaults — NOT a truly-empty char format.

        A bare ``QTextCharFormat()`` would strip the foreground brush
        and font size from the affected text, after which the renderer
        falls back to the QPalette text color (white in the dark theme)
        and the QSS-derived 10 pt baseline. That looked like a "size 9
        white reset" to the user. Applying the saved defaults instead
        keeps Clear Formatting useful (drops bold/italic/strike/etc.)
        without dragging the text out of the user's chosen style.
        """
        cursor = self._editor.textCursor()
        d = (
            editor_defaults.load(self._settings)
            if self._settings is not None
            else editor_defaults.EditorDefaults()
        )
        fmt = editor_defaults.build_char_format(d, editor=self._editor)
        if cursor.hasSelection():
            cursor.setCharFormat(fmt)
        # Also seed the cursor format so the next typed character
        # inherits defaults (matters when the user clicks Clear with
        # no selection — the visible state shouldn't quietly differ).
        self._editor.setCurrentCharFormat(fmt)

    # ---- save-as-default ------------------------------------------------

    def _save_as_default(self) -> None:
        if self._settings is None:
            return
        d = editor_defaults.capture(self._editor)
        editor_defaults.save(self._settings, d)
        # Keep the color swatch in sync so the "A" button paints the
        # color the user just locked in as their default.
        if d.color:
            c = QColor(d.color)
            if c.isValid():
                self._text_color = c
                self._refresh_color_swatches()
        # Brief confirmation — keep it non-blocking so the user isn't
        # pestered for re-focus.
        QMessageBox.information(
            self,
            "Default format saved",
            f"New entries will use {d.size:g} pt"
            f"{' bold' if d.bold else ''}"
            f"{' italic' if d.italic else ''}"
            f"{' underlined' if d.underline else ''}"
            f"{f' in {d.color}' if d.color else ''}.",
        )

    def _return_to_default(self) -> None:
        """Reset the selection (or cursor format) to the saved default."""
        if self._settings is None:
            return
        d = editor_defaults.load(self._settings)
        cursor = self._editor.textCursor()
        if cursor.hasSelection():
            # Rewrite each char in the selection to the default format.
            cursor.setCharFormat(
                editor_defaults.build_char_format(d, editor=self._editor)
            )
        # Always re-seed widget font + current char format so the next
        # typed char inherits defaults regardless of whether a
        # selection existed.
        editor_defaults.apply_to_editor(self._editor, d)
        # Sync the color swatch in case the saved default's color has
        # drifted from whatever the toolbar was showing.
        if d.color:
            c = QColor(d.color)
            if c.isValid():
                self._text_color = c
                self._refresh_color_swatches()

    # ---- editor → toolbar sync ------------------------------------------

    def _sync_from_editor(self) -> None:
        """Reflect the current editor char-format onto the toolbar buttons."""
        self._syncing = True
        try:
            fmt = self._editor.currentCharFormat()
            self.btn_bold.setChecked(
                fmt.fontWeight() >= QFont.Weight.Bold.value
            )
            self.btn_italic.setChecked(fmt.fontItalic())
            self.btn_underline.setChecked(fmt.fontUnderline())
            self.btn_strike.setChecked(fmt.fontStrikeOut())

            # Font size — round to int for display if it looks like a
            # whole number; keep two decimals otherwise.
            size = fmt.fontPointSize() or self._editor.fontPointSize()
            if size <= 0:
                size = self._editor.font().pointSizeF()
            if size > 0:
                txt = (
                    str(int(size)) if abs(size - round(size)) < 1e-3
                    else f"{size:.1f}"
                )
                if self.combo_size.currentText() != txt:
                    idx = self.combo_size.findText(txt)
                    if idx >= 0:
                        self.combo_size.setCurrentIndex(idx)
                    else:
                        self.combo_size.setEditText(txt)

            cursor = self._editor.textCursor()
            current_list = cursor.currentList()
            style = (
                current_list.format().style() if current_list is not None
                else None
            )
            self.btn_bullet.setChecked(
                style == QTextListFormat.Style.ListDisc
            )
            self.btn_numbered.setChecked(
                style == QTextListFormat.Style.ListDecimal
            )

            # Heading buttons reflect the cursor's current block level.
            level = cursor.blockFormat().headingLevel()
            self.btn_h1.setChecked(level == 1)
            self.btn_h2.setChecked(level == 2)
            self.btn_h3.setChecked(level == 3)

            # Keep the text-color swatch in sync with whatever the
            # cursor is currently sitting in. Previously the swatch
            # stuck at the last user-clicked color and stopped
            # reflecting the format of the text under the cursor —
            # felt like the button was "hung". Only update when the
            # cursor's foreground actually resolves to a valid color
            # (some blocks have no explicit foreground).
            fg_brush = fmt.foreground()
            if fg_brush.style() != Qt.BrushStyle.NoBrush:
                fg = fg_brush.color()
                if (
                    fg.isValid()
                    and fg.alpha() != 0
                    and fg.name() != self._text_color.name()
                ):
                    self._text_color = fg
                    self._refresh_color_swatches()

            bg_brush = fmt.background()
            if bg_brush.style() != Qt.BrushStyle.NoBrush:
                bg = bg_brush.color()
                if (
                    bg.isValid()
                    and bg.alpha() != 0
                    and bg.name() != self._highlight_color.name()
                ):
                    self._highlight_color = bg
                    self._refresh_color_swatches()
        finally:
            self._syncing = False
