"""Bin-edge editor dialog for the bucket-based reports.

Used by ReportsTab to override the default Entry Price and Hold Time
bucket layouts. The dialog is unit-aware: price edges are in dollars,
hold-time edges are in minutes.

The same dialog handles both report kinds via a `kind` constructor arg.
"""
from __future__ import annotations

import math
from typing import Optional

from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QVBoxLayout,
)

from gui.dialogs._geometry import DialogGeometryMixin


KIND_PRICE = "price"
KIND_HOLD_MINUTES = "hold_minutes"


def parse_edges(text: str) -> list[float]:
    """Parse a comma-separated edge list.

    Accepts integers, floats, and `inf` / `infinity` (case-insensitive)
    for the unbounded upper edge. Validates:
        - at least 2 edges
        - all non-negative
        - strictly increasing

    Raises ValueError on any failure.
    """
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if len(parts) < 2:
        raise ValueError("Need at least 2 edges (a low and a high).")
    edges: list[float] = []
    for p in parts:
        if p.lower() in ("inf", "infinity", "+inf"):
            edges.append(float("inf"))
            continue
        try:
            v = float(p)
        except ValueError:
            raise ValueError(f"Invalid number: {p!r}")
        if v < 0:
            raise ValueError(f"Edges must be ≥ 0 (got {p}).")
        edges.append(v)
    for i in range(len(edges) - 1):
        if not (edges[i] < edges[i + 1]):
            raise ValueError(
                f"Edges must be strictly increasing (got {edges[i]} ≥ {edges[i + 1]})."
            )
    if math.isinf(edges[0]):
        raise ValueError("First edge cannot be infinity.")
    return edges


def format_edges(edges: list[float]) -> str:
    """Render an edge list back to the canonical comma-separated string."""
    parts: list[str] = []
    for e in edges:
        if math.isinf(e):
            parts.append("inf")
        elif e == int(e):
            parts.append(str(int(e)))
        else:
            parts.append(f"{e:g}")
    return ", ".join(parts)


class BinConfigDialog(DialogGeometryMixin, QDialog):
    """Edit bin edges for a single binnable report."""

    GEOMETRY_KEY = "dialog/bin_config/geometry"

    def __init__(
        self,
        kind: str,
        current_edges: list[float],
        default_edges: list[float],
        parent=None,
    ):
        super().__init__(parent)
        self._kind = kind
        self._default_edges = list(default_edges)
        self._result_edges: Optional[list[float]] = None

        if kind == KIND_PRICE:
            self.setWindowTitle("Configure Price Bins")
            help_text = (
                "Enter bin edges in dollars, separated by commas.\n"
                "Use 'inf' for an unbounded upper edge.\n"
                "Example: 0, 1, 2, 5, 10, 20, 50, inf"
            )
        elif kind == KIND_HOLD_MINUTES:
            self.setWindowTitle("Configure Hold-Time Bins")
            help_text = (
                "Enter bin edges in minutes, separated by commas.\n"
                "Use 'inf' for an unbounded upper edge.\n"
                "Example: 0, 5, 30, 120, 480, inf"
            )
        else:
            raise ValueError(f"Unknown bin kind: {kind!r}")

        self.help_label = QLabel(help_text, self)
        self.help_label.setWordWrap(True)

        self.edit = QLineEdit(self)
        self.edit.setText(format_edges(current_edges))
        self.edit.setMinimumWidth(360)

        self.error_label = QLabel("", self)
        self.error_label.setObjectName("errorLabel")
        self.error_label.setWordWrap(True)

        self.btn_reset = QPushButton("Reset to defaults", self)
        self.btn_reset.clicked.connect(self._on_reset)

        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        self.button_box.accepted.connect(self._on_accept)
        self.button_box.rejected.connect(self.reject)

        bottom = QHBoxLayout()
        bottom.addWidget(self.btn_reset)
        bottom.addStretch()
        bottom.addWidget(self.button_box)

        layout = QVBoxLayout(self)
        layout.addWidget(self.help_label)
        layout.addWidget(self.edit)
        layout.addWidget(self.error_label)
        layout.addLayout(bottom)

        self._restore_geometry()

    # ---- public ------------------------------------------------------------

    def selected_edges(self) -> Optional[list[float]]:
        return self._result_edges

    # ---- internals ---------------------------------------------------------

    def _on_reset(self) -> None:
        self.edit.setText(format_edges(self._default_edges))
        self.error_label.setText("")

    def _on_accept(self) -> None:
        try:
            edges = parse_edges(self.edit.text())
        except ValueError as e:
            self.error_label.setText(str(e))
            return
        self._result_edges = edges
        self.accept()
