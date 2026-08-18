"""Pre-import resolution for position flips.

A *flip* is an exit fill that closes more shares than the position holds.
The broker normally rejects these at order time, so in practice one means
the export is missing fills — and the app must not guess which way.

This dialog puts the decision in front of the user **before anything is
written**, once per block of shares rather than once per share. The answer
is stored on the execution, so every later rebuild replays it silently and
the warning never comes back — unless the trade is cleared and re-uploaded,
which deletes the fill along with its answer and deliberately re-asks.

Four outcomes:
    Stop import        — abort the whole import, nothing is written, so the
                         user can fix the export and paste again.
    Delete extra       — drop the unmatched shares. The trade closes at the
                         size actually held, so P&L is unchanged.
    Leave as open      — the flip really happened: close what was held and
                         open a position the other way for the remainder.
    Close to P&L       — the export is missing entry fills: book the
                         unmatched shares at the position's own average
                         entry so the trade closes at its full exit size.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
)

from ingest.trade_builder import (
    RESOLUTION_CLOSE_TO_PNL, RESOLUTION_DELETE_EXTRA, RESOLUTION_LEAVE_OPEN,
    RESOLUTION_STOP_IMPORT,
)


class FlipResolutionDialog(QDialog):
    """Ask how to handle one flip. ``choice`` holds the answer."""

    def __init__(self, flip, index: int = 1, total: int = 1, parent=None):
        super().__init__(parent)
        self.flip = flip
        self.choice: Optional[str] = None
        self.setWindowTitle("Unmatched shares")
        self.setModal(True)
        self.setMinimumWidth(560)

        counter = f"  ({index} of {total})" if total > 1 else ""
        heading = QLabel(
            f"<b>{flip.symbol} — {flip.excess_shares} unmatched "
            f"share(s){counter}</b>", self,
        )
        heading.setTextFormat(Qt.TextFormat.RichText)

        opposite = "Short" if flip.direction == "Long" else "Long"
        detail = QLabel(
            f"The {flip.type} on "
            f"{flip.entered_at:%Y-%m-%d %H:%M:%S} closed "
            f"<b>{flip.fill_shares:,}</b> share(s) against a "
            f"<b>{flip.position_shares:,}</b>-share {flip.direction} "
            f"position, leaving <b>{flip.excess_shares:,}</b> unmatched.<br><br>"
            "A broker normally rejects an order that flips a position, so "
            "this usually means the export is missing fills.",
            self,
        )
        detail.setTextFormat(Qt.TextFormat.RichText)
        detail.setWordWrap(True)

        held_pnl = self._pnl(flip.position_shares)
        full_pnl = self._pnl(flip.fill_shares)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.addWidget(heading)
        layout.addWidget(detail)

        layout.addWidget(self._option(
            "Delete extra shares",
            f"Drop the {flip.excess_shares:,} unmatched share(s). The trade "
            f"closes at {flip.position_shares:,} shares — gross "
            f"{held_pnl}. No effect on P&L.",
            RESOLUTION_DELETE_EXTRA,
        ))
        layout.addWidget(self._option(
            "Leave as open (flipped)",
            f"Treat the flip as real: close the {flip.position_shares:,} "
            f"shares held, then open a {flip.excess_shares:,}-share "
            f"{opposite} position at "
            f"${flip.filled_price:,.4f}.",
            RESOLUTION_LEAVE_OPEN,
        ))
        layout.addWidget(self._option(
            "Close to P&L",
            f"Assume the export dropped {flip.excess_shares:,} entry "
            f"fill(s). Book them at the position's average entry "
            f"(${flip.avg_entry_price:,.4f}) so the trade closes at "
            f"{flip.fill_shares:,} shares — gross {full_pnl}.",
            RESOLUTION_CLOSE_TO_PNL,
        ))
        layout.addWidget(self._option(
            "Stop import (re-upload)",
            "Cancel the whole import. Nothing is written, so you can fix "
            "the export and paste again.",
            RESOLUTION_STOP_IMPORT,
        ))

    # ---- helpers ----------------------------------------------------------

    def _pnl(self, shares: int) -> str:
        """Gross P&L on ``shares`` at this flip's entry / exit prices."""
        diff = self.flip.filled_price - self.flip.avg_entry_price
        if self.flip.direction != "Long":
            diff = -diff
        v = diff * shares
        sign = "-" if v < 0 else ""
        return f"{sign}${abs(v):,.2f}"

    def _option(self, title: str, body: str, value: str) -> QWidget:
        btn = QPushButton(title, self)
        btn.setMinimumWidth(190)
        btn.clicked.connect(lambda: self._pick(value))
        text = QLabel(body, self)
        text.setWordWrap(True)
        text.setStyleSheet("color: #a0a0a0;")

        wrap = QWidget(self)
        row = QHBoxLayout(wrap)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        row.addWidget(btn, 0, Qt.AlignmentFlag.AlignTop)
        row.addWidget(text, 1)
        return wrap

    def _pick(self, value: str) -> None:
        self.choice = value
        self.accept()

    # Closing via the window's X is the same as declining to decide, which
    # must not silently import a half-answered batch.
    def reject(self) -> None:  # noqa: D102
        self.choice = RESOLUTION_STOP_IMPORT
        super().reject()


def resolve_flips(flips: list, parent=None) -> Optional[dict[str, str]]:
    """Walk the user through every flip.

    Returns ``{import_hash: RESOLUTION_*}``, or ``None`` if the user chose
    to stop the import (or dismissed a dialog), in which case the caller
    must write nothing at all.
    """
    answers: dict[str, str] = {}
    total = len(flips)
    for i, flip in enumerate(flips, start=1):
        dlg = FlipResolutionDialog(flip, index=i, total=total, parent=parent)
        dlg.exec()
        if dlg.choice is None or dlg.choice == RESOLUTION_STOP_IMPORT:
            return None
        answers[flip.import_hash] = dlg.choice
    return answers
