"""Navigation aids for image-heavy rich-text editors.

Two widgets, both designed to live alongside a ``QTextEdit`` whose
document carries headings (H1/H2/H3 via QTextBlockFormat heading level)
and inline images (via the ``attachment:N`` URL convention used by
JournalEditor).

* ``OutlineWidget`` lists the document's headings as clickable rows.
  Click a heading → editor scrolls to that block. Stays cheap on big
  documents: walks the block tree once per ``refresh()`` and that's
  it.

* ``ImageThumbStrip`` renders a horizontal strip of explicitly-chosen
  image thumbnails. Click a thumbnail → editor scrolls to that image
  fragment; right-click → remove it from the strip. The set of
  attachment ids that should appear is supplied by the host tab via
  ``set_thumbnail_filter`` (per-doc opt-in). A size slider in the
  title row scales the entire row; size persistence lives on the host
  tab since the strip is shared between Briefs and Strategies.
"""
from __future__ import annotations

from typing import Callable, Optional, Sequence

from PySide6.QtCore import QSize, Qt, QUrl, Signal
from PySide6.QtGui import (
    QImage, QPixmap, QTextCursor, QTextDocument,
)
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMenu,
    QScrollArea, QSizePolicy, QSlider, QToolButton, QTextEdit,
    QVBoxLayout, QWidget,
)

from gui.widgets.journal_editor import parse_attachment_url
from gui.widgets.thumbnail_tag_menu import (
    ThumbTag, build_thumbnail_tag_submenu,
)


# ---- outline pane ---------------------------------------------------------


class OutlineWidget(QFrame):
    """Clickable list of headings extracted from a QTextDocument.

    Refresh is cheap (one block walk per call) so callers can re-render
    on every textChanged tick without worrying about cost. Use
    ``set_editor`` once at construction; subsequent ``refresh()`` calls
    re-read the document.
    """

    heading_clicked = Signal(int)  # block number

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)

        self._editor: Optional[QTextEdit] = None

        title = QLabel("Outline", self)
        title.setObjectName("sectionTitle")
        title.setStyleSheet(
            "color: #b0b0b0; font-size: 9pt; font-weight: bold;"
        )

        self.list = QListWidget(self)
        self.list.itemClicked.connect(self._on_clicked)
        self.list.setStyleSheet(
            "QListWidget { background: transparent; border: none; }"
            "QListWidget::item { padding: 2px 4px; }"
            "QListWidget::item:selected { "
            "  background: #094771; color: #e0e0e0;"
            "}"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)
        layout.addWidget(title)
        layout.addWidget(self.list, 1)

    def set_editor(self, editor: QTextEdit) -> None:
        self._editor = editor

    def refresh(self) -> None:
        """Re-read headings from the editor's document."""
        self.list.clear()
        if self._editor is None:
            return
        doc = self._editor.document()
        block = doc.firstBlock()
        while block.isValid():
            level = block.blockFormat().headingLevel()
            if level >= 1:
                text = block.text().strip() or "(empty heading)"
                item = QListWidgetItem(
                    f"{'  ' * (level - 1)}{text}"
                )
                item.setData(Qt.ItemDataRole.UserRole, block.blockNumber())
                if level == 1:
                    item.setForeground(Qt.GlobalColor.white)
                self.list.addItem(item)
            block = block.next()
        if self.list.count() == 0:
            placeholder = QListWidgetItem(
                "(no headings — use H1/H2/H3 in the toolbar)"
            )
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            placeholder.setForeground(Qt.GlobalColor.gray)
            self.list.addItem(placeholder)

    def _on_clicked(self, item: QListWidgetItem) -> None:
        block_no = item.data(Qt.ItemDataRole.UserRole)
        if block_no is None:
            return
        try:
            self.heading_clicked.emit(int(block_no))
        except (TypeError, ValueError):
            pass


# ---- image thumbnail strip -----------------------------------------------


# Discrete thumbnail sizes the slider snaps to. Heights are in screen
# pixels; widths follow a 4:3 ratio so the strip stays the same shape
# across sizes. Persistence is by index — adding new sizes at the end
# is safe; reordering would invalidate saved settings.
THUMB_SIZES: list[QSize] = [
    QSize(64, 48),
    QSize(96, 72),
    QSize(128, 96),
    QSize(160, 120),
    QSize(200, 150),
]
DEFAULT_THUMB_SIZE_INDEX = 1  # 96×72 — matches the pre-refactor look.


class ImageThumbStrip(QFrame):
    """Horizontal strip of thumbnails for opt-in document images.

    Click a thumbnail → editor scrolls to that image fragment.
    Right-click → "Remove from thumbnails" emits
    ``thumb_remove_requested(attachment_id)`` so the host tab can
    update its persisted thumbnail set.

    The strip only renders images whose attachment id is in the
    filter set supplied via ``set_thumbnail_filter``. Calling it with
    an empty set (the default) yields an empty strip with a
    discoverability hint.

    Thumbnails are cached per-attachment-id at the current pixel size;
    changing the size invalidates the cache.
    """

    image_clicked = Signal(int)  # block number containing the image
    thumb_remove_requested = Signal(int)  # attachment id
    size_changed = Signal(int)  # index into THUMB_SIZES
    # Emitted whenever the host tab should rebuild the strip — fires on
    # filter chip toggles. Hosts that need to re-query the DB on filter
    # change can listen; the strip already re-renders on its own.
    filter_changed = Signal()
    # Emitted when the user asks to add a new preset or open the manage
    # dialog from a thumbnail's right-click submenu. Hosts handle the
    # DB writes and broadcast cross-tab refresh.
    add_tag_requested = Signal(str, int)        # (name, attachment_id)
    manage_tags_requested = Signal()
    attachment_tags_changed = Signal(int, int, bool)
    # ^ (attachment_id, tag_id, included)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)

        self._editor: Optional[QTextEdit] = None
        self._cache: dict[int, QPixmap] = {}
        self._allowed_ids: set[int] = set()
        self._size_index: int = DEFAULT_THUMB_SIZE_INDEX
        # Guard against the slider's valueChanged firing while we're
        # programmatically syncing it from the host tab.
        self._suspend_size_signal = False

        # Tag state, refreshed by host on doc selection and on preset
        # mutation. The filter is transient (in-memory only) — switching
        # documents resets it via set_doc_tag_map.
        self._all_tags: list[ThumbTag] = []
        self._doc_tag_map: dict[int, set[int]] = {}
        self._filter_tag_ids: set[int] = set()
        self._chip_buttons: dict[int, QToolButton] = {}

        title = QLabel("Thumbnails", self)
        title.setObjectName("sectionTitle")
        title.setStyleSheet(
            "color: #b0b0b0; font-size: 9pt; font-weight: bold;"
        )

        # Size slider — snaps to discrete THUMB_SIZES entries.
        size_label = QLabel("Size:", self)
        size_label.setStyleSheet("color: #909090; font-size: 9pt;")

        self.size_slider = QSlider(Qt.Orientation.Horizontal, self)
        self.size_slider.setRange(0, len(THUMB_SIZES) - 1)
        self.size_slider.setSingleStep(1)
        self.size_slider.setPageStep(1)
        self.size_slider.setTickInterval(1)
        self.size_slider.setTickPosition(QSlider.TickPosition.NoTicks)
        self.size_slider.setFixedWidth(120)
        self.size_slider.setValue(self._size_index)
        self.size_slider.valueChanged.connect(self._on_slider_changed)

        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(6)
        title_row.addWidget(title)
        title_row.addStretch()
        title_row.addWidget(size_label)
        title_row.addWidget(self.size_slider)

        # Chip rail — one checkable QToolButton per preset, plus an
        # "All" pseudo-chip that clears the filter set. Wrapped in its
        # own horizontal scroll area so a long preset list doesn't push
        # the strip wider than the editor column.
        self._chip_host = QWidget(self)
        self._chip_layout = QHBoxLayout(self._chip_host)
        self._chip_layout.setContentsMargins(2, 0, 2, 0)
        self._chip_layout.setSpacing(4)
        self._chip_layout.addStretch()
        self._chip_scroll = QScrollArea(self)
        self._chip_scroll.setWidgetResizable(True)
        self._chip_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._chip_scroll.setWidget(self._chip_host)
        self._chip_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff,
        )
        self._chip_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded,
        )
        self._chip_scroll.setFixedHeight(28)

        self._row_host = QWidget(self)
        self._row_layout = QHBoxLayout(self._row_host)
        self._row_layout.setContentsMargins(2, 2, 2, 2)
        self._row_layout.setSpacing(4)
        self._row_layout.addStretch()

        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll.setWidget(self._row_host)
        self._scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff,
        )
        self._scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded,
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 4)
        layout.setSpacing(2)
        layout.addLayout(title_row)
        layout.addWidget(self._chip_scroll)
        layout.addWidget(self._scroll, 1)

        self._buttons: list[QToolButton] = []
        self._rebuild_chip_rail()
        self._apply_height_from_size()

    # ---- editor + filter wiring ------------------------------------------

    def set_editor(self, editor: QTextEdit) -> None:
        self._editor = editor

    def set_thumbnail_filter(self, allowed_ids: set[int]) -> None:
        """Replace the opt-in set and re-render. Pass an empty set to
        hide every thumbnail (the default for new / migrated docs)."""
        new = {int(v) for v in allowed_ids}
        if new == self._allowed_ids:
            return
        self._allowed_ids = new
        self.refresh()

    # ---- tag state -------------------------------------------------------

    def set_available_tags(self, tags: Sequence[ThumbTag]) -> None:
        """Replace the preset list, re-render the chip rail.

        Filter selection survives wherever possible — chips whose
        preset survived are kept active; chips whose preset was
        deleted are dropped from the filter set.
        """
        self._all_tags = list(tags)
        # Drop any active filter chips whose preset no longer exists.
        surviving = {t.id for t in self._all_tags}
        self._filter_tag_ids &= surviving
        self._rebuild_chip_rail()
        # Tag deletion may have changed which thumbnails pass the
        # filter — re-render the strip too.
        self.refresh()

    def set_doc_tag_map(
        self, mapping: dict[int, set[int]],
    ) -> None:
        """Replace the per-image tag map and reset the active filter.

        Called by the host tab when the user switches documents — a new
        doc starts with no filter applied so the user sees every
        thumbnail by default. Short-circuits when the incoming map is
        identical to the current one *and* no filter is active, so
        flipping back to the same brief doesn't re-walk the document.
        """
        normalized = {
            int(k): {int(v) for v in vs} for k, vs in mapping.items()
        }
        if normalized == self._doc_tag_map and not self._filter_tag_ids:
            return
        self._doc_tag_map = normalized
        if self._filter_tag_ids:
            self._filter_tag_ids = set()
            self._sync_chip_check_state()
        self.refresh()

    def update_attachment_tags(
        self, attachment_id: int, tag_ids: set[int],
    ) -> None:
        """Update the local tag map after the host writes a change.

        Mostly a convenience so the host doesn't have to re-fetch the
        whole map for one image.
        """
        att = int(attachment_id)
        new = {int(v) for v in tag_ids}
        if new:
            self._doc_tag_map[att] = new
        else:
            self._doc_tag_map.pop(att, None)
        # Only re-render if the filter is active — otherwise nothing
        # visible changed.
        if self._filter_tag_ids:
            self.refresh()

    def current_filter(self) -> set[int]:
        return set(self._filter_tag_ids)

    # ---- chip rail -------------------------------------------------------

    def _rebuild_chip_rail(self) -> None:
        """Re-render the chip rail from scratch — call after preset
        list changes."""
        # Tear down existing chips. Keep things simple — re-creating is
        # cheap and avoids partial-update bugs on rename.
        for btn in self._chip_buttons.values():
            btn.deleteLater()
        self._chip_buttons.clear()
        while self._chip_layout.count():
            item = self._chip_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        # "All" pseudo-chip — clears every filter when clicked.
        all_btn = QToolButton(self._chip_host)
        all_btn.setText("All")
        all_btn.setCheckable(True)
        all_btn.setChecked(not self._filter_tag_ids)
        all_btn.setStyleSheet(self._chip_style())
        all_btn.clicked.connect(self._on_all_chip_clicked)
        self._chip_layout.addWidget(all_btn)
        self._chip_buttons[0] = all_btn  # 0 reserved for the All chip

        if not self._all_tags:
            hint = QLabel(
                "(right-click a pinned thumbnail or image to add tags)",
                self._chip_host,
            )
            hint.setStyleSheet(
                "color: #707070; font-style: italic; font-size: 9pt;"
            )
            self._chip_layout.addWidget(hint)
        else:
            for tag in self._all_tags:
                btn = QToolButton(self._chip_host)
                btn.setText(tag.name)
                btn.setCheckable(True)
                btn.setChecked(tag.id in self._filter_tag_ids)
                btn.setStyleSheet(self._chip_style())
                btn.clicked.connect(
                    lambda checked, tid=int(tag.id):
                    self._on_filter_chip_clicked(tid, bool(checked))
                )
                self._chip_layout.addWidget(btn)
                self._chip_buttons[int(tag.id)] = btn

        self._chip_layout.addStretch()

    def _chip_style(self) -> str:
        return (
            "QToolButton {"
            " background: #2a2a2a; color: #d0d0d0;"
            " border: 1px solid #444; border-radius: 9px;"
            " padding: 2px 10px; font-size: 9pt;"
            "}"
            "QToolButton:checked {"
            " background: #094771; color: #ffffff;"
            " border: 1px solid #00A3FF;"
            "}"
            "QToolButton:hover { border: 1px solid #00A3FF; }"
        )

    def _sync_chip_check_state(self) -> None:
        """Re-mark every chip's checked state from the filter set
        without rebuilding."""
        all_btn = self._chip_buttons.get(0)
        if all_btn is not None:
            all_btn.setChecked(not self._filter_tag_ids)
        for tid, btn in self._chip_buttons.items():
            if tid == 0:
                continue
            btn.setChecked(tid in self._filter_tag_ids)

    def _on_all_chip_clicked(self, _checked: bool) -> None:
        # "All" always means "no filter". Clicking it when already
        # active is a no-op apart from re-checking the chip.
        if not self._filter_tag_ids:
            self._sync_chip_check_state()
            return
        self._filter_tag_ids.clear()
        self._sync_chip_check_state()
        self.filter_changed.emit()
        self.refresh()

    def _on_filter_chip_clicked(
        self, tag_id: int, checked: bool,
    ) -> None:
        if checked:
            self._filter_tag_ids.add(tag_id)
        else:
            self._filter_tag_ids.discard(tag_id)
        self._sync_chip_check_state()
        self.filter_changed.emit()
        self.refresh()

    # ---- size control ----------------------------------------------------

    def set_size_index(self, idx: int) -> None:
        """Programmatically set the slider position (host tab uses
        this during settings restore). Clamps to the valid range and
        suppresses the size_changed signal so restore doesn't echo
        back to the settings store."""
        idx = max(0, min(idx, len(THUMB_SIZES) - 1))
        if idx == self._size_index:
            return
        self._size_index = idx
        self._cache.clear()  # cached pixmaps are at the old size
        self._suspend_size_signal = True
        try:
            self.size_slider.setValue(idx)
        finally:
            self._suspend_size_signal = False
        self._apply_height_from_size()
        self.refresh()

    def _on_slider_changed(self, value: int) -> None:
        if self._suspend_size_signal:
            return
        value = max(0, min(int(value), len(THUMB_SIZES) - 1))
        if value == self._size_index:
            return
        self._size_index = value
        self._cache.clear()
        self._apply_height_from_size()
        self.refresh()
        self.size_changed.emit(value)

    def _current_size(self) -> QSize:
        return THUMB_SIZES[self._size_index]

    def _apply_height_from_size(self) -> None:
        """Reserve enough vertical space for the current thumb size,
        the title row, and the chip rail. Keeps the strip from
        clipping when the user slides to a larger preset."""
        cur = self._current_size()
        # title row (~22px) + chip rail (~28px) + padding + thumb +
        # scrollbar headroom.
        self.setFixedHeight(cur.height() + 72)

    # ---- rendering -------------------------------------------------------

    def refresh(self) -> None:
        # Tear down existing buttons / placeholder labels.
        for b in self._buttons:
            self._row_layout.removeWidget(b)
            b.deleteLater()
        self._buttons = []
        while self._row_layout.count():
            item = self._row_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        if self._editor is None:
            self._row_layout.addStretch()
            return
        doc = self._editor.document()

        if not self._allowed_ids:
            empty = QLabel(
                "(no thumbnails yet — right-click an image and choose "
                "'Add to thumbnails' to pin it here)",
                self._row_host,
            )
            empty.setStyleSheet("color: #707070; font-style: italic;")
            self._row_layout.addWidget(empty)
            self._row_layout.addStretch()
            return

        # Walk the doc once, recording the first block that holds each
        # opted-in attachment id so the thumbnail jumps there. Same
        # attachment in multiple blocks → first hit wins; that's fine
        # because the user can scroll forward from there.
        chosen: list[tuple[int, int]] = []  # (att_id, block_no)
        seen: set[int] = set()
        block = doc.firstBlock()
        while block.isValid():
            block_no = block.blockNumber()
            it = block.begin()
            while not it.atEnd():
                frag = it.fragment()
                fmt = frag.charFormat()
                if fmt.isImageFormat():
                    name = fmt.toImageFormat().name()
                    if name:
                        att_id = parse_attachment_url(QUrl(name))
                        if (
                            att_id is not None
                            and att_id in self._allowed_ids
                            and att_id not in seen
                        ):
                            seen.add(att_id)
                            chosen.append((att_id, block_no))
                it += 1
            block = block.next()

        if not chosen:
            empty = QLabel(
                "(pinned images aren't in this document — they may have "
                "been removed)",
                self._row_host,
            )
            empty.setStyleSheet("color: #707070; font-style: italic;")
            self._row_layout.addWidget(empty)
            self._row_layout.addStretch()
            return

        # Apply the active tag filter (OR-combine — a thumb passes if
        # any of its tags is in the filter set).
        if self._filter_tag_ids:
            visible = [
                (att_id, block_no) for att_id, block_no in chosen
                if self._doc_tag_map.get(att_id, set())
                & self._filter_tag_ids
            ]
            if not visible:
                empty = QLabel(
                    "(no thumbnails match the active tag filter — "
                    "click ‘All’ to clear)",
                    self._row_host,
                )
                empty.setStyleSheet(
                    "color: #707070; font-style: italic;"
                )
                self._row_layout.addWidget(empty)
                self._row_layout.addStretch()
                return
            chosen = visible

        for att_id, block_no in chosen:
            self._add_thumbnail(doc, att_id, block_no)
        self._row_layout.addStretch()

    def _add_thumbnail(
        self, doc: QTextDocument, att_id: int, block_no: int,
    ) -> None:
        pix = self._cache.get(att_id)
        if pix is None:
            url = QUrl(f"attachment:{att_id}")
            res = doc.resource(
                QTextDocument.ResourceType.ImageResource, url,
            )
            if isinstance(res, QImage) and not res.isNull():
                scaled = res.scaled(
                    self._current_size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                pix = QPixmap.fromImage(scaled)
                if len(self._cache) > 256:
                    self._cache.pop(next(iter(self._cache)))
                self._cache[att_id] = pix
        if pix is None:
            return
        size = self._current_size()
        btn = QToolButton(self._row_host)
        btn.setIcon(pix)
        btn.setIconSize(size)
        btn.setFixedSize(size.width() + 4, size.height() + 4)
        btn.setToolTip(
            f"Click: jump to image (paragraph {block_no + 1})\n"
            f"Right-click: remove from thumbnails"
        )
        btn.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed,
        )
        btn.setStyleSheet(
            "QToolButton { background: transparent; border: 1px solid #444; }"
            "QToolButton:hover { border: 1px solid #00A3FF; }"
        )
        btn.clicked.connect(
            lambda _checked=False, b=block_no: self.image_clicked.emit(b)
        )
        btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        btn.customContextMenuRequested.connect(
            lambda pos, b=btn, aid=att_id: self._on_thumb_context_menu(b, aid, pos)
        )
        self._row_layout.addWidget(btn)
        self._buttons.append(btn)

    def _on_thumb_context_menu(
        self, btn: QToolButton, att_id: int, pos,
    ) -> None:
        menu = QMenu(btn)
        remove_act = menu.addAction("Remove from thumbnails")
        # Tag submenu — always enabled on a thumbnail (a thumbnail is
        # by definition pinned, so the "pin first" gate doesn't apply).
        current = self._doc_tag_map.get(int(att_id), set())
        build_thumbnail_tag_submenu(
            menu,
            is_pinned=True,
            current_tag_ids=current,
            all_tags=self._all_tags,
            on_toggle=lambda tid, state, a=int(att_id):
                self.attachment_tags_changed.emit(a, tid, state),
            on_add_new=lambda name, a=int(att_id):
                self.add_tag_requested.emit(name, a),
            on_manage=lambda: self.manage_tags_requested.emit(),
        )
        chosen = menu.exec(btn.mapToGlobal(pos))
        if chosen is remove_act:
            self.thumb_remove_requested.emit(int(att_id))

    def clear_cache(self) -> None:
        """Drop all cached thumbnails — call when the editor swaps to
        a different document so stale pixmaps don't leak across."""
        self._cache.clear()


# ---- helper: scroll an editor to a given block ---------------------------


def scroll_editor_to_block(editor: QTextEdit, block_no: int) -> None:
    """Move the editor's cursor to the start of ``block_no`` and
    centre it in the viewport."""
    doc = editor.document()
    block = doc.findBlockByNumber(block_no)
    if not block.isValid():
        return
    cursor = QTextCursor(block)
    editor.setTextCursor(cursor)
    editor.ensureCursorVisible()
    # Give the user a visual cue by selecting the line.
    cursor.movePosition(
        QTextCursor.MoveOperation.EndOfBlock,
        QTextCursor.MoveMode.KeepAnchor,
    )
    editor.setTextCursor(cursor)
