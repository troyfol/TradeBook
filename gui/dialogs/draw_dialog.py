"""Dialog wrapping `DrawCanvas` with a small toolbar.

Two construction modes:
    DrawDialog.blank(parent, size)
        — opens a blank white canvas at the given size; on accept,
          `result_image()` returns the user's drawing as a QImage.
    DrawDialog.annotate(parent, image)
        — opens with `image` as the background; on accept,
          `result_image()` returns the image with strokes composited.

Toolbar:
    [Color █] [Width ───●─] [Pen / Eraser] [Undo] [Clear]   [Cancel] [OK]
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtWidgets import (
    QColorDialog, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QSlider, QVBoxLayout, QWidget,
)

from gui.dialogs._geometry import DialogGeometryMixin
from gui.widgets.draw_canvas import DrawCanvas


class DrawDialog(DialogGeometryMixin, QDialog):
    GEOMETRY_KEY = "dialog/draw/geometry"
    DEFAULT_SIZE = QSize(900, 560)

    def __init__(
        self,
        background: Optional[QImage] = None,
        size: QSize = DEFAULT_SIZE,
        parent=None,
        title: str = "Draw",
    ):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)

        self.canvas = DrawCanvas(background=background, size=size, parent=self)

        # ---- toolbar ------------------------------------------------------
        self._color = QColor("#FF1744")
        self.btn_color = QPushButton(self)
        self.btn_color.setFixedSize(36, 24)
        self.btn_color.setToolTip("Pen color")
        self._refresh_color_swatch()
        self.btn_color.clicked.connect(self._on_pick_color)

        self.width_slider = QSlider(Qt.Orientation.Horizontal, self)
        self.width_slider.setRange(1, 24)
        self.width_slider.setValue(3)
        self.width_slider.setFixedWidth(120)
        self.width_slider.valueChanged.connect(self._on_width_changed)

        self.width_label = QLabel("3 px", self)
        self.width_label.setFixedWidth(36)

        self.btn_pen = QPushButton("Pen", self)
        self.btn_pen.setCheckable(True)
        self.btn_pen.setChecked(True)
        self.btn_pen.clicked.connect(lambda: self._set_mode(eraser=False))

        self.btn_eraser = QPushButton("Eraser", self)
        self.btn_eraser.setCheckable(True)
        self.btn_eraser.clicked.connect(lambda: self._set_mode(eraser=True))

        self.btn_undo = QPushButton("Undo", self)
        self.btn_undo.clicked.connect(self.canvas.undo)

        self.btn_clear = QPushButton("Clear", self)
        self.btn_clear.clicked.connect(self.canvas.clear_strokes)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(6)
        toolbar.addWidget(QLabel("Color:", self))
        toolbar.addWidget(self.btn_color)
        toolbar.addSpacing(8)
        toolbar.addWidget(QLabel("Width:", self))
        toolbar.addWidget(self.width_slider)
        toolbar.addWidget(self.width_label)
        toolbar.addSpacing(8)
        toolbar.addWidget(self.btn_pen)
        toolbar.addWidget(self.btn_eraser)
        toolbar.addSpacing(8)
        toolbar.addWidget(self.btn_undo)
        toolbar.addWidget(self.btn_clear)
        toolbar.addStretch()

        # ---- canvas in a scroll area (for big background images) ----------
        scroll = QScrollArea(self)
        scroll.setWidget(self.canvas)
        scroll.setWidgetResizable(False)
        scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # ---- buttons ------------------------------------------------------
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        layout.addLayout(toolbar)
        layout.addWidget(scroll, 1)
        layout.addWidget(buttons)

        # Initial canvas state
        self.canvas.set_pen_color(self._color)
        self.canvas.set_pen_width(3)

        # Restore prior geometry if any (after the layout is in place
        # so initial sizeHint isn't fighting the restore).
        self._restore_geometry()

    # ---- factories --------------------------------------------------------

    @classmethod
    def blank(cls, parent=None, size: QSize = DEFAULT_SIZE) -> "DrawDialog":
        return cls(background=None, size=size, parent=parent, title="New Drawing")

    @classmethod
    def annotate(cls, image: QImage, parent=None) -> "DrawDialog":
        return cls(
            background=image, size=image.size(), parent=parent,
            title="Annotate Image",
        )

    # ---- result -----------------------------------------------------------

    def result_image(self) -> QImage:
        return self.canvas.to_qimage()

    def has_strokes(self) -> bool:
        return self.canvas.has_strokes()

    # ---- toolbar slots ----------------------------------------------------

    def _refresh_color_swatch(self) -> None:
        pix = QPixmap(self.btn_color.size())
        pix.fill(self._color)
        self.btn_color.setIcon(pix)
        self.btn_color.setIconSize(self.btn_color.size())

    def _on_pick_color(self) -> None:
        c = QColorDialog.getColor(
            self._color, self, "Pen color",
            QColorDialog.ColorDialogOption.ShowAlphaChannel,
        )
        if c.isValid():
            self._color = c
            self._refresh_color_swatch()
            self.canvas.set_pen_color(c)
            # Picking a color also flips back to pen mode.
            self._set_mode(eraser=False)

    def _on_width_changed(self, v: int) -> None:
        self.canvas.set_pen_width(v)
        self.width_label.setText(f"{v} px")

    def _set_mode(self, eraser: bool) -> None:
        self.canvas.set_eraser(eraser)
        self.btn_pen.setChecked(not eraser)
        self.btn_eraser.setChecked(eraser)
