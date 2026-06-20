"""Rich-text journal editor backed by SQLite-stored attachments.

`JournalEditor` is a `QTextEdit` subclass that:
    * Stores pasted screenshots as `journal_attachments` BLOBs (PNG-encoded)
      and references them from the editor's HTML via `attachment:<id>` URLs.
    * Accepts dragged files: image MIMEs are inserted inline as images,
      anything else is appended as a clickable link in an "Attachments"
      section at the bottom of the entry.
    * Uses a custom `QTextDocument` subclass whose `loadResource` resolves
      `attachment:<id>` URLs by fetching the matching BLOB from the DB,
      so saved entries render their images when re-opened.
    * Lets the user Ctrl-click attachment links to open the referenced
      file in the system default viewer (the bytes are dumped to a temp
      file first since they live in SQLite).

The editor knows about persistence through a `(get_conn, get_trade_id)`
pair so it can call `db_manager.insert_attachment` and
`db_manager.fetch_attachment` without owning the connection itself.
"""
from __future__ import annotations

import mimetypes
import os
import re
import sqlite3
import tempfile
from html import escape as _html_escape
from typing import Callable, Optional

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPoint, QRectF, Qt, QUrl, QSettings
from PySide6.QtGui import (
    QColor, QContextMenuEvent, QDesktopServices, QImage, QKeyEvent, QMouseEvent,
    QPainter, QPaintEvent, QPen, QTextCharFormat, QTextCursor, QTextDocument,
    QTextFormat, QTextImageFormat, QWheelEvent,
)
from PySide6.QtWidgets import QMenu, QMessageBox, QTextEdit

from gui.widgets import editor_defaults
from ingest import db_manager

# Attachment size cap (50 MB). Prevents bloating the SQLite database with
# oversized files that would slow down backup/restore.
MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024

# QUrl scheme used to reference DB-stored attachments inside the document.
ATTACHMENT_SCHEME = "attachment"

# Default look of the overlay border the editor paints around images that
# are pinned as thumbnails (a pure UI affordance — the image bytes are
# untouched). Color is a hex string; width is in screen pixels. The host
# tab can override these per-tab via ``set_pinned_overlay_style``.
DEFAULT_PINNED_OVERLAY_COLOR = "#FFD700"  # gold — visible on dark + light
DEFAULT_PINNED_OVERLAY_WIDTH = 3
MAX_PINNED_OVERLAY_WIDTH = 8


def make_attachment_url(att_id: int) -> QUrl:
    return QUrl(f"{ATTACHMENT_SCHEME}:{int(att_id)}")


def parse_attachment_url(url: QUrl) -> Optional[int]:
    """Return the integer id from an attachment: URL, or None."""
    if url.scheme() != ATTACHMENT_SCHEME:
        return None
    raw = url.path() or url.toString().split(":", 1)[-1]
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


# ---- custom QTextDocument that resolves attachment: URLs -------------------


class _JournalDocument(QTextDocument):
    """QTextDocument that fetches `attachment:N` images from the DB on demand."""

    def __init__(
        self,
        get_conn: Callable[[], sqlite3.Connection],
        parent=None,
    ):
        super().__init__(parent)
        self._get_conn = get_conn

    def loadResource(self, type_: int, name: QUrl):
        if isinstance(name, QUrl) and name.scheme() == ATTACHMENT_SCHEME:
            att_id = parse_attachment_url(name)
            if att_id is not None:
                payload = db_manager.fetch_attachment(self._get_conn(), att_id)
                if payload is not None:
                    mime, data, _filename = payload
                    if mime.startswith("image/"):
                        img = QImage.fromData(QByteArray(data))
                        if not img.isNull():
                            # Cache so subsequent paints don't re-query DB.
                            self.addResource(
                                QTextDocument.ResourceType.ImageResource,
                                name,
                                img,
                            )
                            return img
                    # Non-image: return raw bytes (used by link rendering, etc.)
                    return QByteArray(data)
        return super().loadResource(type_, name)


# ---- editor ----------------------------------------------------------------


class JournalEditor(QTextEdit):
    """Rich-text editor with DB-backed image + file attachment handling."""

    def __init__(
        self,
        get_conn: Callable[[], sqlite3.Connection],
        get_trade_id: Callable[[], Optional[int]],
        parent=None,
        *,
        settings: Optional[QSettings] = None,
    ):
        super().__init__(parent)
        self._get_conn = get_conn
        self._get_trade_id = get_trade_id
        self._settings = settings
        self._doc = _JournalDocument(get_conn, self)
        self.setDocument(self._doc)
        self.setAcceptRichText(True)
        self.setAcceptDrops(True)
        self.setPlaceholderText(
            "Notes for this trade. Paste screenshots or drag files in."
        )
        self.setMouseTracking(True)
        # Optional hook the host tab sets so it can inject custom
        # entries into the right-click menu for an image fragment.
        # Called as ``builder(menu, att_id)`` just before the
        # "Annotate image…" entry. Tabs that don't care leave it None.
        self.image_context_menu_builder: Optional[
            Callable[[QMenu, int], None]
        ] = None
        # Attachment ids the host tab has pinned to its thumbnail
        # strip. Drawn with a yellow border overlay so the user can
        # see at a glance which embedded images are also thumbnails.
        # Journal tab (no thumbnail strip) leaves this empty and pays
        # no paint cost.
        self._thumbnail_ids: set[int] = set()
        # Color + width of that pinned-image overlay border. Defaults to
        # gold/3px; host tabs override per-tab via set_pinned_overlay_style.
        self._pinned_overlay_color: str = DEFAULT_PINNED_OVERLAY_COLOR
        self._pinned_overlay_width: int = DEFAULT_PINNED_OVERLAY_WIDTH
        # Apply persistent defaults on first construction so an empty
        # editor starts at the user's preferred size / weight / color.
        editor_defaults.apply_to_editor(
            self, editor_defaults.load(self._settings),
        )
        # Qt preserves the cursor's char format across most edits, but
        # emptying the document (select-all → delete, or backspacing
        # down to zero chars) can drop per-char attributes — color in
        # particular — leaving the next keystroke at the widget's
        # palette color instead of the user's saved default. Catch
        # that by re-seeding whenever the doc goes empty.
        self.textChanged.connect(self._maybe_reseed_defaults)

    # ---- document lifecycle ------------------------------------------------

    def setHtml(self, html: str) -> None:  # type: ignore[override]
        """Replace the underlying document on every content switch.

        ``QTextDocument`` caches every decoded image resource forever via
        ``addResource`` — there's no public API to clear it. Swapping in a
        fresh document on each load is the cleanest way to bound the
        editor's memory; construction is cheap and the next repaint re-
        fetches only the images actually on screen.

        If the incoming content is visually empty (new entry, cleared
        brief, or a previously-saved entry that's just ``<html><body>
        <p/></body></html>`` boilerplate), the persistent editor
        defaults are re-applied so the next keystroke picks them up
        rather than whatever format Qt's HTML roundtripping left
        behind.
        """
        self._doc = _JournalDocument(self._get_conn, self)
        self.setDocument(self._doc)
        super().setHtml(html or "")
        # Check PLAIN TEXT emptiness, not the HTML string. Qt wraps even
        # blank editors in a full ``<html>…<body><p/></body></html>``
        # envelope, so a string-level emptiness check misses that case —
        # which meant defaults silently stopped applying across trade
        # switches for users with saved-then-cleared journal entries.
        if not self.toPlainText().strip():
            editor_defaults.apply_to_editor(
                self, editor_defaults.load(self._settings),
            )

    def _maybe_reseed_defaults(self) -> None:
        """Re-apply persistent defaults whenever the document empties.

        Guarded by ``_reseeding`` so the setCurrentCharFormat call
        inside ``apply_to_editor`` can't re-enter via textChanged.
        """
        if getattr(self, "_reseeding", False):
            return
        if self.toPlainText().strip():
            return
        self._reseeding = True
        try:
            editor_defaults.apply_to_editor(
                self, editor_defaults.load(self._settings),
            )
        finally:
            self._reseeding = False

    def keyPressEvent(self, event: QKeyEvent) -> None:  # type: ignore[override]
        """Belt-and-suspenders size + color persistence.

        Two distinct paths to defend against:

        1. Some Qt configurations (Windows native event loops, not
           the ``offscreen`` platform) drop the foreground brush from
           the cursor's current char format when the document
           transiently empties. The textChanged-driven reseed
           restores it, but on the very next printable keystroke Qt
           sometimes reads the format BEFORE our signal handler runs
           — so the first typed character ends up back at the
           palette color. Re-seed defaults right before the key
           dispatch fires whenever a printable key would land in an
           empty document.

        2. Pressing Enter (or Tab) makes Qt drop char-format
           properties from the new block — the foreground brush and
           explicit font size in particular, and *always* so when
           leaving a heading. The reseed in (1) doesn't trigger (the
           doc isn't empty), so after dispatch we patch any missing
           size / foreground back onto the cursor format via
           ``_heal_missing_cursor_format``.
        """
        text = event.text()
        if (
            text
            and text.isprintable()
            and self._settings is not None
            and not self.toPlainText().strip()
        ):
            editor_defaults.apply_to_editor(
                self, editor_defaults.load(self._settings),
            )
        super().keyPressEvent(event)
        # After a block-creating key (Enter, and sometimes Tab) Qt can
        # drop the cursor's explicit font size and/or foreground brush,
        # leaving the new line rendered at the QSS baseline — the
        # intermittent "size 9 white" reset. Patch back ONLY the
        # properties that actually went missing so an intentional
        # off-default run (e.g. 24 pt red) carries forward untouched.
        #
        # This replaces an earlier guard that (a) only fired when the
        # foreground had vanished *and* was present beforehand, so it
        # missed the common case where the cursor had no explicit
        # foreground going into the newline, and (b) never checked the
        # font size, so a size-only drop slipped through.
        if self._settings is not None and event.key() in (
            Qt.Key.Key_Return,
            Qt.Key.Key_Enter,
            Qt.Key.Key_Tab,
        ):
            self._heal_missing_cursor_format()

    def _heal_missing_cursor_format(self) -> None:
        """Fill a dropped size / foreground back onto the cursor format.

        Only the genuinely-absent properties are restored (from the saved
        editor defaults), and only when there is no active selection — so
        this can never reformat highlighted text; it just keeps the
        *next* typed character from falling back to the QSS
        "size 9 white" baseline after a newline.
        """
        if self.textCursor().hasSelection():
            return
        fmt = self.currentCharFormat()
        missing_fg = not fmt.hasProperty(
            QTextFormat.Property.ForegroundBrush
        )
        missing_size = (
            not fmt.hasProperty(QTextFormat.Property.FontPointSize)
            or fmt.fontPointSize() <= 0
        )
        if not (missing_fg or missing_size):
            return
        d = editor_defaults.load(self._settings)
        patch = QTextCharFormat()
        if missing_fg:
            color = d.color or editor_defaults.DEFAULT_FALLBACK_COLOR
            c = QColor(color)
            if c.isValid():
                patch.setForeground(c)
        if missing_size:
            size = (
                d.size if d.size > 0
                else editor_defaults.INITIAL_DEFAULT_SIZE
            )
            patch.setFontPointSize(size)
        self.mergeCurrentCharFormat(patch)

    # ---- mime / drop handling ---------------------------------------------

    def canInsertFromMimeData(self, source) -> bool:  # type: ignore[override]
        if source.hasImage() or source.hasUrls():
            return True
        return super().canInsertFromMimeData(source)

    def insertFromMimeData(self, source) -> None:  # type: ignore[override]
        # 1. Pasted screenshot (no file URL, just an image payload).
        if source.hasImage() and not source.hasUrls():
            qimg = source.imageData()
            if isinstance(qimg, QImage) and not qimg.isNull():
                self._insert_qimage(qimg)
                return

        # 2. Dragged or pasted files: split into images vs other.
        if source.hasUrls():
            for url in source.urls():
                if not url.isLocalFile():
                    continue
                path = url.toLocalFile()
                if not path or not os.path.isfile(path):
                    continue
                self._insert_file(path)
            return

        super().insertFromMimeData(source)

    # ---- file insertion helpers -------------------------------------------

    def _insert_qimage(self, qimg: QImage) -> None:
        """Persist a QImage as a PNG attachment and reference it in the doc."""
        if self._get_trade_id() is None:
            return
        # Snapshot the cursor's char format BEFORE inserting the image.
        # The default ``cursor.insertImage(url)`` writes an image
        # fragment whose char format has no foreground brush — so the
        # cursor inheriting from it (when the user clicks adjacent to
        # the image and starts a new line) would revert typing to the
        # QPalette default. Build the image fragment's format
        # explicitly with the user's current foreground + size so it
        # carries the surrounding text style.
        pre_fmt = self.currentCharFormat()
        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        qimg.save(buf, "PNG")
        data = bytes(buf.data())
        att_id = db_manager.insert_attachment(
            self._get_conn(), "image/png", data, filename=None,
        )
        url = make_attachment_url(att_id)
        # Pre-cache so the just-inserted image renders immediately without
        # needing a DB round-trip.
        self._doc.addResource(
            QTextDocument.ResourceType.ImageResource, url, qimg,
        )
        cursor = self.textCursor()
        img_fmt = self._build_image_format(url.toString(), pre_fmt)
        cursor.insertImage(img_fmt)
        cursor.insertBlock()
        self.setTextCursor(cursor)
        self._restore_post_image_format(pre_fmt)

    def _insert_file(self, path: str) -> None:
        """Insert a local file as either an inline image or a clickable link."""
        if self._get_trade_id() is None:
            return

        # Size guard — reject files that would bloat the database.
        try:
            file_size = os.path.getsize(path)
        except OSError:
            return
        if file_size > MAX_ATTACHMENT_BYTES:
            QMessageBox.warning(
                self, "File too large",
                f"Attachment exceeds the "
                f"{MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB size limit.",
            )
            return

        with open(path, "rb") as f:
            data = f.read()
        mime, _ = mimetypes.guess_type(path)
        mime = mime or "application/octet-stream"
        filename = _sanitize_filename(os.path.basename(path))

        att_id = db_manager.insert_attachment(
            self._get_conn(), mime, data, filename=filename,
        )
        url = make_attachment_url(att_id)

        if mime.startswith("image/"):
            qimg = QImage.fromData(QByteArray(data))
            if not qimg.isNull():
                self._doc.addResource(
                    QTextDocument.ResourceType.ImageResource, url, qimg,
                )
                pre_fmt = self.currentCharFormat()
                cursor = self.textCursor()
                img_fmt = self._build_image_format(url.toString(), pre_fmt)
                cursor.insertImage(img_fmt)
                cursor.insertBlock()
                self.setTextCursor(cursor)
                self._restore_post_image_format(pre_fmt)
                return

        # Fallback: append a clickable link (Ctrl-click to open).
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        safe_name = _html_escape(filename, quote=True)
        cursor.insertHtml(
            f'<p>📎 <a href="{_html_escape(url.toString(), quote=True)}">'
            f'{safe_name}</a></p>'
        )
        self.setTextCursor(cursor)

    def _build_image_format(
        self, url_str: str, pre_fmt: QTextCharFormat,
    ) -> QTextImageFormat:
        """Construct a ``QTextImageFormat`` that carries the user's
        current foreground brush and font size.

        ``QTextImageFormat`` is a subclass of ``QTextCharFormat``, so
        embedding the surrounding text's style on the image fragment
        means the cursor inherits a sensible foreground when it sits
        adjacent to the image — and any new block opened from that
        cursor (Enter/Tab) carries the same brush forward instead of
        snapping to the palette default. Width/height stay unset so
        Qt renders the image at its natural size by default; the
        Ctrl+wheel scaling path can grow it from there.
        """
        img_fmt = QTextImageFormat()
        img_fmt.setName(url_str)
        if pre_fmt.hasProperty(QTextFormat.Property.ForegroundBrush):
            img_fmt.setForeground(pre_fmt.foreground())
        else:
            d = editor_defaults.load(self._settings)
            color = d.color or editor_defaults.DEFAULT_FALLBACK_COLOR
            c = QColor(color)
            if c.isValid():
                img_fmt.setForeground(c)
        size = pre_fmt.fontPointSize()
        if size <= 0:
            d = editor_defaults.load(self._settings)
            size = d.size if d.size > 0 else editor_defaults.INITIAL_DEFAULT_SIZE
        if size > 0:
            img_fmt.setFontPointSize(size)
        return img_fmt

    def _restore_post_image_format(self, pre_fmt: QTextCharFormat) -> None:
        """Re-apply the cursor format that was active before an image
        insert so subsequent typing keeps the user's color and font.

        Falls back to the persisted editor defaults when ``pre_fmt`` had
        no explicit foreground brush — e.g. the user pasted as the very
        first action in a fresh editor, before any default-colored text
        was typed. We use ``hasProperty(ForegroundBrush)`` because
        ``foreground().style()`` can be Qt.NoBrush even on a format
        that genuinely carries a brush, and a default-constructed
        ``QTextCharFormat`` returns a valid-looking black brush from
        ``foreground().color()`` either way.
        """
        if pre_fmt.hasProperty(QTextFormat.Property.ForegroundBrush):
            self.setCurrentCharFormat(pre_fmt)
            return
        editor_defaults.apply_to_editor(
            self, editor_defaults.load(self._settings),
        )

    # ---- public: insert a freehand drawing ---------------------------------

    def insert_drawing(self, qimg: QImage) -> None:
        """Insert a user-drawn QImage as a new attachment at the cursor."""
        if qimg.isNull():
            return
        self._insert_qimage(qimg)

    # ---- thumbnail-pinned overlay -----------------------------------------

    def set_thumbnail_ids(self, ids) -> None:
        """Tell the editor which attachment ids are pinned thumbnails.

        Pinned images are painted with a yellow border overlay so the
        user can spot at a glance which embedded images also live in
        the thumbnail strip. Pure UI affordance — the underlying image
        bytes are untouched, so docx/html exports remain clean.
        """
        new_ids = {int(i) for i in (ids or set())}
        if new_ids == self._thumbnail_ids:
            return
        self._thumbnail_ids = new_ids
        self.viewport().update()

    def set_pinned_overlay_style(self, color_hex: str, width: int) -> None:
        """Set the color + thickness of the pinned-image overlay border.

        ``width`` 0 hides the overlay entirely. Validates the color and
        clamps the width, then repaints. The host tab calls this on
        settings restore and after the user edits the style.
        """
        color = QColor(color_hex)
        color_str = (
            color.name() if color.isValid() else self._pinned_overlay_color
        )
        try:
            w = int(width)
        except (TypeError, ValueError):
            w = self._pinned_overlay_width
        w = max(0, min(w, MAX_PINNED_OVERLAY_WIDTH))
        if (
            color_str == self._pinned_overlay_color
            and w == self._pinned_overlay_width
        ):
            return
        self._pinned_overlay_color = color_str
        self._pinned_overlay_width = w
        self.viewport().update()

    def pinned_overlay_style(self) -> tuple[str, int]:
        """Current (hex color, width) of the pinned-image overlay."""
        return self._pinned_overlay_color, self._pinned_overlay_width

    def paintEvent(self, event: QPaintEvent) -> None:  # type: ignore[override]
        super().paintEvent(event)
        if not self._thumbnail_ids or self._pinned_overlay_width <= 0:
            return
        painter = QPainter(self.viewport())
        try:
            pen = QPen(QColor(self._pinned_overlay_color))
            pen.setWidth(self._pinned_overlay_width)
            pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            for rect in self._iter_pinned_image_rects():
                painter.drawRect(rect)
        finally:
            painter.end()

    def _iter_pinned_image_rects(self):
        """Yield viewport-space rects for every image fragment whose
        attachment id is in ``self._thumbnail_ids``."""
        doc = self.document()
        viewport_rect = self.viewport().rect()
        block = doc.firstBlock()
        while block.isValid():
            it = block.begin()
            while not it.atEnd():
                frag = it.fragment()
                fmt = frag.charFormat()
                if fmt.isImageFormat():
                    att_id = parse_attachment_url(QUrl(fmt.toImageFormat().name()))
                    if att_id is not None and att_id in self._thumbnail_ids:
                        rect = self._image_fragment_rect(
                            frag.position(), fmt.toImageFormat(),
                        )
                        if rect is not None and rect.intersects(QRectF(viewport_rect)):
                            yield rect
                it += 1
            block = block.next()

    def _image_fragment_rect(
        self, start: int, img_fmt: QTextImageFormat,
    ) -> Optional[QRectF]:
        """Return the viewport-space rect of the image at ``start``.

        Uses two cursor rects (before and after the image character) to
        bracket the inline image's horizontal extent, then resolves the
        height from the format or the underlying image resource.
        """
        cursor = QTextCursor(self.document())
        cursor.setPosition(start)
        left_rect = self.cursorRect(cursor)
        cursor.setPosition(start + 1)
        right_rect = self.cursorRect(cursor)

        width = float(img_fmt.width())
        height = float(img_fmt.height())
        if width <= 0 or height <= 0:
            res = self._doc.resource(
                QTextDocument.ResourceType.ImageResource,
                QUrl(img_fmt.name()),
            )
            if isinstance(res, QImage) and not res.isNull():
                if width <= 0:
                    width = float(res.width())
                if height <= 0:
                    height = float(res.height())
        # Prefer the cursor-bracketed width when both cursor rects sit on
        # the same line — handles font-scaled images where the format
        # width may not match what Qt actually rendered.
        if (
            width <= 0
            and right_rect.top() == left_rect.top()
            and right_rect.left() > left_rect.left()
        ):
            width = float(right_rect.left() - left_rect.left())
        if width <= 0 or height <= 0:
            return None
        return QRectF(
            float(left_rect.left()),
            float(left_rect.top()),
            width,
            height,
        )

    # ---- image scaling (Ctrl + wheel) -------------------------------------

    def wheelEvent(self, e: QWheelEvent) -> None:  # type: ignore[override]
        if e.modifiers() & Qt.KeyboardModifier.ControlModifier:
            pos = e.position().toPoint()
            frag_info = self._image_fragment_at(pos)
            if frag_info is not None:
                step = 1.1 if e.angleDelta().y() > 0 else (1.0 / 1.1)
                self._scale_image_fragment(frag_info, step)
                e.accept()
                return
        super().wheelEvent(e)

    def _image_fragment_at(self, pos: QPoint):
        """Locate the QTextFragment containing the image under `pos`.

        Returns `(start_position, image_format)` or None. The fragment
        format may have a width / height of 0 (meaning "natural"), in which
        case the caller resolves the natural size from the document
        resource.
        """
        cursor = self.cursorForPosition(pos)
        block = cursor.block()
        if not block.isValid():
            return None
        target = cursor.position()
        it = block.begin()
        while not it.atEnd():
            frag = it.fragment()
            start = frag.position()
            end = start + frag.length()
            if start <= target <= end:
                fmt = frag.charFormat()
                if fmt.isImageFormat():
                    return start, fmt.toImageFormat()
            it += 1
        return None

    def _scale_image_fragment(self, frag_info, factor: float) -> None:
        start, img_fmt = frag_info
        url = QUrl(img_fmt.name())

        # Resolve current displayed size: prefer the explicit width set on
        # the format; fall back to the underlying image resource.
        cur_w = float(img_fmt.width()) if img_fmt.width() > 0 else 0.0
        cur_h = float(img_fmt.height()) if img_fmt.height() > 0 else 0.0
        if cur_w <= 0 or cur_h <= 0:
            res = self._doc.resource(
                QTextDocument.ResourceType.ImageResource, url,
            )
            if isinstance(res, QImage) and not res.isNull():
                if cur_w <= 0:
                    cur_w = float(res.width())
                if cur_h <= 0:
                    cur_h = float(res.height())
            else:
                return  # can't scale without a baseline

        new_w = max(16.0, cur_w * factor)
        # Cap at 4× the editor viewport width so a runaway zoom doesn't
        # create an unusable monster image.
        max_w = max(64.0, float(self.viewport().width()) * 4.0)
        new_w = min(new_w, max_w)
        # Preserve aspect ratio.
        ratio = new_w / cur_w if cur_w > 0 else 1.0
        new_h = cur_h * ratio

        new_fmt = QTextImageFormat(img_fmt)
        new_fmt.setWidth(new_w)
        new_fmt.setHeight(new_h)

        cursor = self.textCursor()
        cursor.beginEditBlock()
        cursor.setPosition(start)
        cursor.setPosition(start + 1, QTextCursor.MoveMode.KeepAnchor)
        cursor.setCharFormat(new_fmt)
        cursor.endEditBlock()

    # ---- right-click menu (Annotate image…) -------------------------------

    def contextMenuEvent(self, e: QContextMenuEvent) -> None:  # type: ignore[override]
        menu = self.createStandardContextMenu()
        frag_info = self._image_fragment_at(e.pos())
        if frag_info is not None:
            menu.addSeparator()
            # Let the host tab (Briefs / Strategies) inject thumbnail
            # toggle actions ABOVE the annotate entry so the most
            # frequent action sits closest to the cursor. The builder
            # is responsible for resolving the attachment id → current
            # thumbnail state and adding either "Add to thumbnails"
            # or "Remove from thumbnails".
            if self.image_context_menu_builder is not None:
                _start, img_fmt = frag_info
                att_id = parse_attachment_url(QUrl(img_fmt.name()))
                if att_id is not None:
                    try:
                        self.image_context_menu_builder(menu, att_id)
                    except Exception:
                        # A misbehaving builder must not block the
                        # rest of the context menu from showing.
                        pass
            act_annotate = menu.addAction("Annotate image…")
            act_annotate.triggered.connect(
                lambda: self._annotate_image_fragment(frag_info)
            )
        menu.exec(e.globalPos())

    def _annotate_image_fragment(self, frag_info) -> None:
        """Open the draw dialog with the clicked image as background and,
        on accept, replace the existing image fragment with the annotated
        version stored as a new attachment."""
        start, img_fmt = frag_info
        url = QUrl(img_fmt.name())
        bg = self._doc.resource(
            QTextDocument.ResourceType.ImageResource, url,
        )
        if not isinstance(bg, QImage) or bg.isNull():
            return
        # Local import to avoid an unconditional import cycle (the dialog
        # imports the canvas widget which in turn lives in gui/widgets).
        from gui.dialogs.draw_dialog import DrawDialog

        dlg = DrawDialog.annotate(bg, parent=self)
        if dlg.exec() != dlg.DialogCode.Accepted:
            return
        if not dlg.has_strokes():
            return  # nothing drawn → leave the original alone
        annotated = dlg.result_image()
        if annotated.isNull() or self._get_trade_id() is None:
            return

        # Persist the annotated image as a new attachment.
        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        annotated.save(buf, "PNG")
        data = bytes(buf.data())
        att_id = db_manager.insert_attachment(
            self._get_conn(), "image/png", data, filename=None,
        )
        new_url = make_attachment_url(att_id)
        self._doc.addResource(
            QTextDocument.ResourceType.ImageResource, new_url, annotated,
        )

        # Replace the existing image character with the new one, preserving
        # any explicit width/height the user had set on the original AND
        # carrying forward the foreground brush + size so the cursor
        # inherits a valid color when it lands adjacent to the image.
        new_fmt = QTextImageFormat()
        new_fmt.setName(new_url.toString())
        if img_fmt.width() > 0:
            new_fmt.setWidth(img_fmt.width())
        if img_fmt.height() > 0:
            new_fmt.setHeight(img_fmt.height())
        if img_fmt.hasProperty(QTextFormat.Property.ForegroundBrush):
            new_fmt.setForeground(img_fmt.foreground())
        if img_fmt.fontPointSize() > 0:
            new_fmt.setFontPointSize(img_fmt.fontPointSize())

        cursor = self.textCursor()
        cursor.beginEditBlock()
        cursor.setPosition(start)
        cursor.setPosition(start + 1, QTextCursor.MoveMode.KeepAnchor)
        cursor.removeSelectedText()
        cursor.insertImage(new_fmt)
        cursor.endEditBlock()

    # ---- link handling -----------------------------------------------------

    def mousePressEvent(self, e: QMouseEvent) -> None:  # type: ignore[override]
        if e.button() == Qt.MouseButton.LeftButton and (
            e.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            href = self.anchorAt(e.position().toPoint())
            if href:
                self._open_attachment_link(href)
                return
        super().mousePressEvent(e)

    def _open_attachment_link(self, href: str) -> None:
        url = QUrl(href)
        att_id = parse_attachment_url(url)
        if att_id is None:
            QDesktopServices.openUrl(url)
            return
        payload = db_manager.fetch_attachment(self._get_conn(), att_id)
        if payload is None:
            return
        mime, data, filename = payload
        suffix = os.path.splitext(filename or "")[1] or _guess_suffix(mime)

        # Write into the app-managed temp dir (purged on every launch)
        # so these files don't accumulate forever in the system tempdir.
        from config import TEMP_DIR
        try:
            TEMP_DIR.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(
                suffix=suffix, prefix="att_", dir=str(TEMP_DIR),
            )
        except OSError:
            # Fallback: system tempdir if the app dir is unavailable.
            fd, tmp_path = tempfile.mkstemp(
                suffix=suffix, prefix="tradebook_att_",
            )
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
        except OSError:
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(tmp_path))


def _guess_suffix(mime: str) -> str:
    ext = mimetypes.guess_extension(mime or "")
    return ext or ""


def _sanitize_filename(name: str) -> str:
    """Strip path separators, control characters, and cap length."""
    name = os.path.basename(name) if name else "attachment"
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    return name[:255] or "attachment"
