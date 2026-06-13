"""Inline find / replace bar for any QTextEdit-derived widget.

Sits above the editor, hidden by default; Ctrl+F toggles it. Wraps
``QTextDocument.find`` for forward / backward search and supports
single-replace + replace-all. Designed to be dropped into the layout
of any tab that hosts a JournalEditor — the bar binds to a single
target editor.

Keyboard:
    * Ctrl+F     → show + focus
    * Enter      → find next
    * Shift+Ent  → find prev
    * Esc        → hide
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import (
    QKeySequence, QShortcut, QTextCursor, QTextDocument,
)
from PySide6.QtWidgets import (
    QCheckBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QTextEdit, QWidget,
)


class FindBar(QWidget):
    """Find + replace strip targeting a single QTextEdit."""

    def __init__(self, editor: QTextEdit, parent=None):
        super().__init__(parent)
        self._editor = editor

        self.find_edit = QLineEdit(self)
        self.find_edit.setPlaceholderText("Find")
        self.find_edit.returnPressed.connect(self._find_next)

        self.replace_edit = QLineEdit(self)
        self.replace_edit.setPlaceholderText("Replace with")

        self.chk_case = QCheckBox("Case", self)

        self.btn_prev = QPushButton("◀", self)
        self.btn_prev.setFixedWidth(28)
        self.btn_prev.setToolTip("Previous (Shift+Enter)")
        self.btn_prev.clicked.connect(self._find_prev)

        self.btn_next = QPushButton("▶", self)
        self.btn_next.setFixedWidth(28)
        self.btn_next.setToolTip("Next (Enter)")
        self.btn_next.clicked.connect(self._find_next)

        self.btn_replace = QPushButton("Replace", self)
        self.btn_replace.clicked.connect(self._replace_one)

        self.btn_replace_all = QPushButton("Replace all", self)
        self.btn_replace_all.clicked.connect(self._replace_all)

        self.status_label = QLabel("", self)
        self.status_label.setObjectName("hintLabel")
        self.status_label.setMinimumWidth(120)

        self.btn_close = QPushButton("✕", self)
        self.btn_close.setFixedWidth(28)
        self.btn_close.setToolTip("Close (Esc)")
        self.btn_close.clicked.connect(self.hide_bar)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(4)
        layout.addWidget(self.find_edit, 2)
        layout.addWidget(self.btn_prev)
        layout.addWidget(self.btn_next)
        layout.addWidget(self.replace_edit, 2)
        layout.addWidget(self.btn_replace)
        layout.addWidget(self.btn_replace_all)
        layout.addWidget(self.chk_case)
        layout.addWidget(self.status_label)
        layout.addWidget(self.btn_close)

        # Shortcuts — Ctrl+F (or Cmd+F on macOS) shows; Esc hides;
        # Shift+Enter is "find previous" while focus is on the find box.
        self._sc_show = QShortcut(QKeySequence.StandardKey.Find, editor)
        self._sc_show.activated.connect(self.show_bar)
        self._sc_show_self = QShortcut(QKeySequence.StandardKey.Find, self)
        self._sc_show_self.activated.connect(self.show_bar)
        self._sc_hide = QShortcut(QKeySequence("Esc"), self)
        self._sc_hide.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._sc_hide.activated.connect(self.hide_bar)
        self._sc_prev = QShortcut(QKeySequence("Shift+Return"), self.find_edit)
        self._sc_prev.activated.connect(self._find_prev)

        self.setVisible(False)

    # ---- public --------------------------------------------------------

    def show_bar(self) -> None:
        # Pre-fill the search field with the current selection if any.
        sel = self._editor.textCursor().selectedText()
        if sel and "\u2029" not in sel:  # exclude multi-paragraph selections
            self.find_edit.setText(sel)
        self.setVisible(True)
        self.find_edit.setFocus()
        self.find_edit.selectAll()

    def hide_bar(self) -> None:
        self.setVisible(False)
        self.status_label.setText("")
        self._editor.setFocus()

    # ---- internals -----------------------------------------------------

    def _flags(self, *, backward: bool = False) -> QTextDocument.FindFlag:
        flags = QTextDocument.FindFlag(0)
        if self.chk_case.isChecked():
            flags |= QTextDocument.FindFlag.FindCaseSensitively
        if backward:
            flags |= QTextDocument.FindFlag.FindBackward
        return flags

    def _find(self, *, backward: bool = False) -> bool:
        needle = self.find_edit.text()
        if not needle:
            return False
        ok = self._editor.find(needle, self._flags(backward=backward))
        if not ok:
            # Wrap around: jump to start (or end) and try once more.
            cur = self._editor.textCursor()
            cur.movePosition(
                QTextCursor.MoveOperation.End if backward
                else QTextCursor.MoveOperation.Start
            )
            self._editor.setTextCursor(cur)
            ok = self._editor.find(needle, self._flags(backward=backward))
            if ok:
                self.status_label.setText("(wrapped)")
            else:
                self.status_label.setText("Not found")
                return False
        else:
            self.status_label.setText("")
        return ok

    def _find_next(self) -> None:
        self._find(backward=False)

    def _find_prev(self) -> None:
        self._find(backward=True)

    def _replace_one(self) -> None:
        cur = self._editor.textCursor()
        needle = self.find_edit.text()
        if not needle:
            return
        # If the current selection matches the needle, replace it. Then
        # advance to the next occurrence either way.
        sel = cur.selectedText()
        equal = (
            sel == needle if self.chk_case.isChecked()
            else sel.lower() == needle.lower()
        )
        if cur.hasSelection() and equal:
            cur.insertText(self.replace_edit.text())
        self._find_next()

    def _replace_all(self) -> None:
        needle = self.find_edit.text()
        if not needle:
            return
        replacement = self.replace_edit.text()
        # Do the work inside one undo block so Ctrl+Z reverts the whole
        # operation, not each replacement.
        cur = self._editor.textCursor()
        cur.beginEditBlock()
        try:
            top = self._editor.textCursor()
            top.movePosition(QTextCursor.MoveOperation.Start)
            self._editor.setTextCursor(top)
            count = 0
            while self._editor.find(needle, self._flags()):
                self._editor.textCursor().insertText(replacement)
                count += 1
            self.status_label.setText(f"Replaced {count}")
        finally:
            cur.endEditBlock()
