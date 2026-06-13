"""Lightweight pen / eraser canvas used by both standalone-draw and
image-annotation flows.

Strokes are stored as a list (one entry per mouse-down → mouse-up gesture)
so that an undo step can pop the most recent stroke without re-rendering
the others. Eraser strokes are just regular strokes painted in the
background color (or as Qt CompositionMode_Clear when there's a background
image, so the user can rub through annotations).

The canvas can optionally take a background QImage; in that case the saved
output composites the strokes on top of the background. Without a
background, the canvas is a transparent layer over the widget's palette.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from PySide6.QtCore import QPoint, QPointF, QRect, QSize, Qt
from PySide6.QtGui import (
    QColor, QImage, QMouseEvent, QPainter, QPaintEvent, QPen,
)
from PySide6.QtWidgets import QSizePolicy, QWidget


@dataclass
class _Stroke:
    color: QColor
    width: int
    erase: bool
    points: list[QPointF] = field(default_factory=list)


class DrawCanvas(QWidget):
    """Pen/eraser surface with undo + optional background image."""

    def __init__(
        self,
        background: Optional[QImage] = None,
        size: QSize = QSize(800, 500),
        parent=None,
    ):
        super().__init__(parent)
        self._background: Optional[QImage] = background
        self._strokes: list[_Stroke] = []
        self._current: Optional[_Stroke] = None
        self._pen_color = QColor("#FF1744")
        self._pen_width = 3
        self._erasing = False

        if background is not None and not background.isNull():
            target_size = background.size()
        else:
            target_size = size
        self._canvas_size = QSize(target_size)
        self.setFixedSize(target_size)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.CrossCursor)

    # ---- public configuration ---------------------------------------------

    def set_pen_color(self, color: QColor) -> None:
        self._pen_color = QColor(color)

    def set_pen_width(self, width: int) -> None:
        self._pen_width = max(1, int(width))

    def set_eraser(self, erasing: bool) -> None:
        self._erasing = bool(erasing)
        self.setCursor(
            Qt.CursorShape.PointingHandCursor if erasing
            else Qt.CursorShape.CrossCursor
        )

    def is_erasing(self) -> bool:
        return self._erasing

    def can_undo(self) -> bool:
        return bool(self._strokes)

    def undo(self) -> None:
        if self._strokes:
            self._strokes.pop()
            self.update()

    def clear_strokes(self) -> None:
        if self._strokes or self._current:
            self._strokes = []
            self._current = None
            self.update()

    def has_strokes(self) -> bool:
        return bool(self._strokes)

    # ---- export -----------------------------------------------------------

    def to_qimage(self) -> QImage:
        """Render the current canvas (background + strokes) into a QImage."""
        if self._background is not None and not self._background.isNull():
            out = QImage(self._background)
        else:
            out = QImage(self.size(), QImage.Format.Format_ARGB32_Premultiplied)
            out.fill(Qt.GlobalColor.white)
        painter = QPainter(out)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._paint_strokes(painter, with_background=False)
        painter.end()
        return out

    # ---- mouse handling ---------------------------------------------------

    def mousePressEvent(self, e: QMouseEvent) -> None:  # type: ignore[override]
        if e.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(e)
            return
        self._current = _Stroke(
            color=QColor(self._pen_color),
            width=self._pen_width,
            erase=self._erasing,
            points=[QPointF(e.position())],
        )
        self.update()

    def mouseMoveEvent(self, e: QMouseEvent) -> None:  # type: ignore[override]
        if self._current is None:
            return
        self._current.points.append(QPointF(e.position()))
        self.update()

    def mouseReleaseEvent(self, e: QMouseEvent) -> None:  # type: ignore[override]
        if self._current is None or e.button() != Qt.MouseButton.LeftButton:
            return
        if len(self._current.points) >= 1:
            self._strokes.append(self._current)
        self._current = None
        self.update()

    # ---- painting ---------------------------------------------------------

    def paintEvent(self, _e: QPaintEvent) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._paint_strokes(painter, with_background=True)
        painter.end()

    def _paint_strokes(self, painter: QPainter, with_background: bool) -> None:
        if with_background:
            if self._background is not None and not self._background.isNull():
                painter.drawImage(QPoint(0, 0), self._background)
            else:
                painter.fillRect(self.rect(), Qt.GlobalColor.white)

        all_strokes = list(self._strokes)
        if self._current is not None:
            all_strokes.append(self._current)

        for stroke in all_strokes:
            if stroke.erase:
                painter.setCompositionMode(
                    QPainter.CompositionMode.CompositionMode_Clear
                    if (with_background and self._background is not None)
                    else QPainter.CompositionMode.CompositionMode_SourceOver
                )
                pen_color = QColor(255, 255, 255, 255)
            else:
                painter.setCompositionMode(
                    QPainter.CompositionMode.CompositionMode_SourceOver
                )
                pen_color = stroke.color
            pen = QPen(
                pen_color, stroke.width, Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin,
            )
            painter.setPen(pen)
            pts = stroke.points
            if len(pts) == 1:
                painter.drawPoint(pts[0])
            else:
                for i in range(1, len(pts)):
                    painter.drawLine(pts[i - 1], pts[i])

        # Restore default mode in case caller paints more.
        painter.setCompositionMode(
            QPainter.CompositionMode.CompositionMode_SourceOver
        )
