"""Open-positions index for the Dashboard.

Lives inside a ChartCard like the other dashboard widgets — accepts a
list of open trade dicts via ``set_positions`` and renders a
two-stat header (count + capital deployed) plus a scrollable
symbol-by-symbol breakdown.

"Capital deployed" is computed as ``avg_entry_price * total_shares``
across every open position, regardless of direction. For shorts this
represents the notional value tied up as margin collateral, which is a
useful proxy for buying power consumed even if the brokerage's exact
formula differs.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QScrollArea, QVBoxLayout, QWidget,
)

from gui.widgets.chart_palette import get_palette, palette_hub


def _fmt_currency_compact(v: float) -> str:
    """Currency formatting that drops trailing cents past the thousands
    cut so the header value stays legible at small card widths."""
    sign = "-" if v < 0 else ""
    v = abs(v)
    if v >= 10_000:
        return f"{sign}${v:,.0f}"
    return f"{sign}${v:,.2f}"


def _fmt_shares(n: int) -> str:
    return f"{int(n):,} sh"


class OpenPositionsCard(QFrame):
    """Header + per-symbol breakdown of currently-open positions."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)

        # ---- header (count + deployed capital) ---------------------------
        self._count_value = QLabel("0", self)
        self._count_value.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._count_value.setStyleSheet(
            "font-size: 22pt; font-weight: bold; color: #e0e0e0;"
        )
        self._count_label = QLabel("open", self)
        self._count_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._count_label.setStyleSheet("color: #909090; font-size: 9pt;")

        self._cap_value = QLabel("$0", self)
        self._cap_value.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cap_value.setStyleSheet(
            "font-size: 22pt; font-weight: bold; color: #e0e0e0;"
        )
        self._cap_label = QLabel("deployed", self)
        self._cap_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cap_label.setStyleSheet("color: #909090; font-size: 9pt;")

        count_box = QVBoxLayout()
        count_box.setSpacing(0)
        count_box.addWidget(self._count_value)
        count_box.addWidget(self._count_label)

        cap_box = QVBoxLayout()
        cap_box.setSpacing(0)
        cap_box.addWidget(self._cap_value)
        cap_box.addWidget(self._cap_label)

        header = QHBoxLayout()
        header.setContentsMargins(4, 4, 4, 4)
        header.setSpacing(8)
        header.addStretch()
        header.addLayout(count_box)
        header.addStretch()
        header.addLayout(cap_box)
        header.addStretch()

        # ---- per-symbol list ---------------------------------------------
        self._list_host = QWidget(self)
        self._list_layout = QVBoxLayout(self._list_host)
        self._list_layout.setContentsMargins(6, 4, 6, 4)
        self._list_layout.setSpacing(2)
        self._list_layout.addStretch()

        scroll = QScrollArea(self)
        scroll.setWidget(self._list_host)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff,
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(2, 2, 2, 2)
        outer.setSpacing(2)
        outer.addLayout(header)
        outer.addWidget(scroll, 1)

        self._row_widgets: list[QWidget] = []
        self._last_positions: list[dict] = []

        # Re-render on palette changes so the long/short tint stays in
        # sync with the user's chosen positive/negative colors.
        palette_hub().palette_changed.connect(self._rerender_rows)

    # ---- public API -------------------------------------------------------

    def set_positions(self, trades: list[dict]) -> None:
        """Populate the card from a list of open trade dicts.

        Each dict must carry ``symbol``, ``direction``, ``total_shares``,
        and ``avg_entry_price``. Trades missing any of these are silently
        skipped — they're either malformed or represent something the
        card can't display sensibly.
        """
        rows: list[dict] = []
        total_capital = 0.0
        for t in trades:
            try:
                shares = int(t.get("total_shares") or 0)
                price = float(t.get("avg_entry_price") or 0.0)
            except (TypeError, ValueError):
                continue
            if shares <= 0 or price <= 0:
                continue
            value = shares * price
            rows.append({
                "symbol": str(t.get("symbol") or "?"),
                "direction": str(t.get("direction") or "Long"),
                "shares": shares,
                "price": price,
                "value": value,
            })
            total_capital += value
        # Largest position first — most operationally relevant.
        rows.sort(key=lambda r: r["value"], reverse=True)

        self._last_positions = rows
        self._count_value.setText(str(len(rows)))
        self._count_label.setText("open" if len(rows) == 1 else "open")
        self._cap_value.setText(_fmt_currency_compact(total_capital))
        self._rerender_rows()

    # ---- internal --------------------------------------------------------

    def _rerender_rows(self) -> None:
        # Tear down existing rows.
        for w in self._row_widgets:
            self._list_layout.removeWidget(w)
            w.deleteLater()
        self._row_widgets = []

        # Pop the trailing stretch; rebuild after rows.
        while self._list_layout.count():
            item = self._list_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        if not self._last_positions:
            empty = QLabel("No open positions.", self._list_host)
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setStyleSheet("color: #707070; font-style: italic;")
            self._list_layout.addWidget(empty)
            self._row_widgets.append(empty)
            self._list_layout.addStretch()
            return

        pal = get_palette(self)
        for row in self._last_positions:
            self._list_layout.addWidget(self._build_row_widget(row, pal))

        self._list_layout.addStretch()

    def _build_row_widget(self, row: dict, pal) -> QWidget:
        sym = QLabel(row["symbol"])
        sym.setStyleSheet("color: #e0e0e0; font-weight: bold; font-size: 10pt;")

        direction = row["direction"]
        is_long = direction.lower() == "long"
        dir_color = pal.positive if is_long else pal.negative
        dir_lbl = QLabel("Long" if is_long else "Short")
        dir_lbl.setStyleSheet(
            f"color: {dir_color}; font-size: 9pt; font-weight: bold;"
        )

        shares = QLabel(_fmt_shares(row["shares"]))
        shares.setStyleSheet("color: #b0b0b0; font-size: 9pt;")

        value = QLabel(_fmt_currency_compact(row["value"]))
        value.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
        )
        value.setStyleSheet("color: #e0e0e0; font-size: 10pt;")

        wrap = QWidget(self._list_host)
        layout = QHBoxLayout(wrap)
        layout.setContentsMargins(2, 1, 2, 1)
        layout.setSpacing(6)
        layout.addWidget(sym)
        layout.addWidget(dir_lbl)
        layout.addWidget(shares)
        layout.addStretch()
        layout.addWidget(value)
        wrap.setToolTip(
            f"{row['symbol']} · {direction} · "
            f"{int(row['shares']):,} shares @ ${row['price']:,.2f}"
        )
        self._row_widgets.append(wrap)
        return wrap
