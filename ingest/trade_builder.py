"""Group filled executions into logical trades.

State machine per symbol (chronological order, Type column is authoritative):
    Buy            → open / add to Long
    Sell           → reduce / close Long
    Sell Short     → open / add to Short
    Buy to Cover   → reduce / close Short

An exit that closes more shares than the position holds is a *flip*. The
broker normally rejects these at order time, so in practice one means the
export is missing fills. The builder never guesses: it reports the flip
as a ``FlipInstance`` and the user resolves it once, per block of shares,
in the pre-import dialog. The chosen resolution is stored on the
execution and replayed on every later rebuild, so an answered flip stays
answered.

Resolutions (see ``RESOLUTION_*``):
    delete_extra  — drop the unmatched shares from the fill. The trade
                    closes at the position size it actually held, so P&L
                    is unchanged from the shares-held baseline.
    close_to_pnl  — book the unmatched shares into the trade at its own
                    average entry price, so the trade closes at its full
                    exit size. Use when the export is missing entry fills.
    leave_open    — close what was held, then open a real position in the
                    opposite direction for the remainder. Use when the
                    flip genuinely happened.

With no resolution recorded, the builder falls back to ``delete_extra``'s
shape (close what was held) but *also* emits the flip so the caller can
warn — that keeps a never-answered flip visible without losing the P&L
on the shares that did trade.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from config import (
    TYPE_BUY, TYPE_SELL, TYPE_SELL_SHORT, TYPE_BUY_TO_COVER,
    DIR_LONG, DIR_SHORT,
)


class TradeBuilderError(Exception):
    pass


# ---- flip resolutions ------------------------------------------------------

RESOLUTION_DELETE_EXTRA = "delete_extra"
RESOLUTION_CLOSE_TO_PNL = "close_to_pnl"
RESOLUTION_LEAVE_OPEN = "leave_open"

# "Stop the import and let me re-upload" is a caller-level choice, not a
# builder one — it aborts before anything is written, so it never reaches
# here. Listed for the UI's benefit.
RESOLUTION_STOP_IMPORT = "stop_import"

VALID_RESOLUTIONS = frozenset({
    RESOLUTION_DELETE_EXTRA,
    RESOLUTION_CLOSE_TO_PNL,
    RESOLUTION_LEAVE_OPEN,
})

RESOLUTION_LABELS = {
    RESOLUTION_DELETE_EXTRA: "Delete extra shares",
    RESOLUTION_CLOSE_TO_PNL: "Close to P&L",
    RESOLUTION_LEAVE_OPEN: "Leave as open (flipped)",
}


class FlipInstance(str):
    """One block of shares that couldn't be paired against a position.

    Subclasses ``str`` so it flows through the existing ``list[str]``
    error channel untouched — anything that just prints or greps the
    message keeps working — while carrying the structured fields the
    resolution UI needs. Deliberately per *fill*, not per share: the
    user answers "what happens to these 6 shares" once, not six times.
    """

    __slots__ = (
        "symbol", "import_hash", "entered_at", "type", "direction",
        "filled_price", "position_shares", "fill_shares", "excess_shares",
        "avg_entry_price",
    )

    def __new__(
        cls, *, symbol: str, import_hash: str, entered_at: datetime,
        type: str, direction: str, filled_price: float,
        position_shares: int, fill_shares: int, excess_shares: int,
        avg_entry_price: float,
    ) -> "FlipInstance":
        flip = "Long→Short" if direction == DIR_LONG else "Short→Long"
        self = super().__new__(cls, (
            f"{symbol}: {type} at {entered_at} would flip {flip} — "
            f"{fill_shares} share(s) against a {position_shares}-share "
            f"position, {excess_shares} unmatched"
        ))
        self.symbol = symbol
        self.import_hash = import_hash
        self.entered_at = entered_at
        self.type = type
        self.direction = direction
        self.filled_price = filled_price
        self.position_shares = position_shares
        self.fill_shares = fill_shares
        self.excess_shares = excess_shares
        self.avg_entry_price = avg_entry_price
        return self

    @property
    def message(self) -> str:
        return str(self)


def _fill_time(f: dict) -> datetime:
    """When a fill actually happened, not when its order was entered.

    TradeStation reports both, and for a resting order they can be days
    apart — a stop entered on the 6th that triggers on the 17th shows
    ``entered_at`` of the 6th. Exits are timestamped by fill so P&L
    lands on the day the position really closed and hold durations
    reflect the real time in the market. Entries keep using
    ``entered_at``: entry fills are effectively instant, and moving them
    would re-bucket the by-hour / by-weekday reports.

    Falls back to ``entered_at`` for rows with no fill timestamp.
    """
    return f.get("filled_canceled_at") or f["entered_at"]


@dataclass
class BuiltTrade:
    symbol: str
    direction: str
    entry_time: datetime
    exit_time: Optional[datetime]
    avg_entry_price: float
    avg_exit_price: Optional[float]
    total_shares: int
    total_commission: float
    gross_pnl: Optional[float]
    net_pnl: Optional[float]
    hold_duration_seconds: Optional[int]
    is_open: bool
    # Partial-close tracking for open positions that have been scaled
    # out of. ``closed_shares`` is how many of ``total_shares`` have
    # already been exited while the trade is still open; ``realized_*``
    # are the P&L on that closed slice. These stay informational — the
    # trade is NOT counted as closed (exit_time / net_pnl stay None) until
    # the whole position is flat, at which point it finalizes as one
    # closed trade over all shares. For a fully-closed trade
    # ``closed_shares == total_shares`` and the realized fields are None
    # (the realized P&L lives in gross_pnl / net_pnl instead).
    closed_shares: int = 0
    realized_pnl: Optional[float] = None
    realized_net_pnl: Optional[float] = None
    execution_hashes: list[str] = field(default_factory=list)


def _vwap(fills: list[dict]) -> tuple[float, int]:
    total_shares = sum(f["qty_filled"] for f in fills)
    if total_shares == 0:
        return 0.0, 0
    total_value = sum(f["qty_filled"] * f["filled_price"] for f in fills)
    return total_value / total_shares, total_shares


def _finalize(symbol: str, direction: str, entry_fills: list[dict],
              exit_fills: list[dict], is_open: bool) -> BuiltTrade:
    avg_entry, entry_shares = _vwap(entry_fills)
    entry_time = min(f["entered_at"] for f in entry_fills)
    entry_commission = sum(f["commission"] for f in entry_fills)
    exit_commission = sum(f["commission"] for f in exit_fills)
    total_commission = entry_commission + exit_commission
    # Synthetic fills (close_to_pnl's reconstructed entry shares) carry no
    # import_hash — they correspond to no execution row, so they must not
    # produce a trade_executions link.
    execution_hashes = [
        f["import_hash"] for f in entry_fills + exit_fills
        if f.get("import_hash")
    ]

    if is_open or not exit_fills:
        # Still-open trade. If it's been partially scaled out of, compute
        # the realized P&L on the closed slice so the process is visible,
        # but keep it uncounted as a closed trade (exit_time / gross / net
        # stay None) until the position is fully flat.
        closed_shares, realized_pnl, realized_net_pnl = _partial_realized(
            direction, avg_entry, entry_shares, entry_commission,
            exit_fills, exit_commission,
        )
        return BuiltTrade(
            symbol=symbol,
            direction=direction,
            entry_time=entry_time,
            exit_time=None,
            avg_entry_price=avg_entry,
            avg_exit_price=None,
            total_shares=entry_shares,
            total_commission=total_commission,
            gross_pnl=None,
            net_pnl=None,
            hold_duration_seconds=None,
            is_open=True,
            closed_shares=closed_shares,
            realized_pnl=realized_pnl,
            realized_net_pnl=realized_net_pnl,
            execution_hashes=execution_hashes,
        )

    avg_exit, _ = _vwap(exit_fills)
    exit_time = max(_fill_time(f) for f in exit_fills)
    if direction == DIR_LONG:
        gross = (avg_exit - avg_entry) * entry_shares
    else:
        gross = (avg_entry - avg_exit) * entry_shares
    net = gross - total_commission

    return BuiltTrade(
        symbol=symbol,
        direction=direction,
        entry_time=entry_time,
        exit_time=exit_time,
        avg_entry_price=avg_entry,
        avg_exit_price=avg_exit,
        total_shares=entry_shares,
        total_commission=total_commission,
        gross_pnl=gross,
        net_pnl=net,
        # Clamped at 0: TradeStation occasionally stamps a fill a second
        # *before* its own order's entered_at, which would otherwise
        # produce a negative hold on a fast scalp.
        hold_duration_seconds=max(
            0, int((exit_time - entry_time).total_seconds()),
        ),
        is_open=False,
        # A fully-closed trade has every share realized; the P&L already
        # lives in gross_pnl / net_pnl, so the realized_* slice fields stay
        # None to avoid double-reporting.
        closed_shares=entry_shares,
        execution_hashes=execution_hashes,
    )


def _partial_realized(
    direction: str, avg_entry: float, entry_shares: int,
    entry_commission: float, exit_fills: list[dict], exit_commission: float,
) -> tuple[int, Optional[float], Optional[float]]:
    """Realized P&L on the already-exited slice of a still-open trade.

    Returns ``(closed_shares, realized_gross, realized_net)``. When no
    exits have happened yet, returns ``(0, None, None)``. The net figure
    charges the full exit commission plus the share of entry commission
    proportional to the closed slice — the rest of the entry commission
    stays with the shares that are still open.
    """
    avg_exit, closed_shares = _vwap(exit_fills)
    if closed_shares <= 0:
        return 0, None, None
    if direction == DIR_LONG:
        realized_gross = (avg_exit - avg_entry) * closed_shares
    else:
        realized_gross = (avg_entry - avg_exit) * closed_shares
    prorated_entry_comm = (
        entry_commission * (closed_shares / entry_shares)
        if entry_shares else 0.0
    )
    realized_net = realized_gross - exit_commission - prorated_entry_comm
    return closed_shares, realized_gross, realized_net


def _split_fill(fill: dict, qty: int) -> dict:
    """A copy of ``fill`` covering only ``qty`` of its filled shares.

    Used when an exit fill closes more shares than the position holds.
    The **full** commission rides along with the closing slice rather
    than being prorated: the whole order's commission is real money
    paid, and the unmatched remainder never forms a trade to carry the
    rest, so prorating would quietly drop it from lifetime commission.
    ``import_hash`` is deliberately left alone so the execution still
    links to the trade in ``trade_executions``.
    """
    return {**fill, "qty_filled": qty}


def _synthetic_entry(fill: dict, qty: int, price: float) -> dict:
    """A phantom entry fill used by ``close_to_pnl``.

    The export is missing entry fills, so reconstruct them at the
    position's own average entry price — that keeps the VWAP unchanged
    while letting the trade close at its true exit size. It carries no
    commission (none was paid for shares the export never recorded) and
    no ``import_hash``, so it links to no execution row.
    """
    return {
        **fill,
        "qty_filled": qty,
        "filled_price": price,
        "commission": 0.0,
        "import_hash": None,
    }


def _build_for_symbol(
    symbol: str,
    fills: list[dict],
    resolutions: Optional[dict[str, str]] = None,
) -> tuple[list[BuiltTrade], list[str]]:
    """State-machine over one symbol's fills.

    Returns ``(trades, errors)``. ``errors`` holds plain strings for
    structural problems and :class:`FlipInstance` objects (which *are*
    strings) for unanswered flips, so callers can pick the flips back
    out with ``isinstance``.

    ``resolutions`` maps an execution's ``import_hash`` to one of the
    ``RESOLUTION_*`` values. A resolved flip is applied silently — it
    was answered once already and must not nag on every later rebuild.

    When a fill breaks state the builder records it and **resets** so
    subsequent fills can still form valid trades. Completed trades from
    before the bad fill are preserved — critical, because a single rogue
    execution must not wipe out every prior trade for the symbol (that
    cascade-drops journal entries + tags during ``rebuild_trades``).
    """
    resolutions = resolutions or {}
    fills = sorted(fills, key=lambda x: x["entered_at"])
    trades: list[BuiltTrade] = []
    errors: list[str] = []

    direction: Optional[str] = None
    entry_fills: list[dict] = []
    exit_fills: list[dict] = []
    position: int = 0

    def _reset() -> None:
        nonlocal direction, entry_fills, exit_fills, position
        direction = None
        entry_fills, exit_fills, position = [], [], 0

    def _close(is_open: bool = False) -> None:
        trades.append(
            _finalize(symbol, direction, entry_fills, exit_fills, is_open)
        )

    def _handle_overshoot(fill: dict, qty: int) -> None:
        """An exit fill closes more shares than the position holds.

        Applies the recorded resolution for this fill, or — when none
        exists — closes what was actually held and reports the flip so
        the caller can put it in front of the user.
        """
        nonlocal direction, entry_fills, exit_fills, position
        held = position
        avg_entry, _ = _vwap(entry_fills)
        flip = FlipInstance(
            symbol=symbol,
            import_hash=fill.get("import_hash") or "",
            entered_at=fill["entered_at"],
            type=fill["type"],
            direction=direction,
            filled_price=float(fill["filled_price"]),
            position_shares=held,
            fill_shares=qty,
            excess_shares=qty - held,
            avg_entry_price=avg_entry,
        )
        mode = resolutions.get(flip.import_hash)

        if mode == RESOLUTION_CLOSE_TO_PNL:
            # Reconstruct the missing entry shares at the position's own
            # average price so the trade closes at its full exit size.
            entry_fills.append(
                _synthetic_entry(fill, flip.excess_shares, avg_entry)
            )
            exit_fills.append(fill)
            _close()
            _reset()
            return

        if mode == RESOLUTION_LEAVE_OPEN:
            # A real flip: close what was held, then open a position the
            # other way for the remainder at this fill's price. The
            # commission was already charged to the closing leg.
            exit_fills.append(_split_fill(fill, held))
            _close()
            opening = {
                **fill, "qty_filled": flip.excess_shares, "commission": 0.0,
            }
            new_direction = DIR_SHORT if direction == DIR_LONG else DIR_LONG
            _reset()
            direction = new_direction
            entry_fills = [opening]
            exit_fills = []
            position = flip.excess_shares
            return

        # RESOLUTION_DELETE_EXTRA, or not yet answered: close the shares
        # actually held and drop the remainder. Only an *unanswered* flip
        # is reported — an answered one has already been dealt with.
        exit_fills.append(_split_fill(fill, held))
        _close()
        _reset()
        if mode != RESOLUTION_DELETE_EXTRA:
            errors.append(flip)

    for fill in fills:
        t = fill["type"]
        qty = fill["qty_filled"]
        try:
            if direction is None:
                if t == TYPE_BUY:
                    direction = DIR_LONG
                elif t == TYPE_SELL_SHORT:
                    direction = DIR_SHORT
                else:
                    raise TradeBuilderError(
                        f"{symbol}: '{t}' at {fill['entered_at']} "
                        f"has no open position"
                    )
                entry_fills = [fill]
                exit_fills = []
                position = qty
                continue

            # Long and Short are mirror images — pick the pair of types
            # that add to / reduce the open position and share one body.
            entry_type = (
                TYPE_BUY if direction == DIR_LONG else TYPE_SELL_SHORT
            )
            exit_type = (
                TYPE_SELL if direction == DIR_LONG else TYPE_BUY_TO_COVER
            )

            if t == entry_type:
                entry_fills.append(fill)
                position += qty
            elif t == exit_type:
                if qty > position:
                    _handle_overshoot(fill, qty)
                    continue
                exit_fills.append(fill)
                position -= qty
                if position == 0:
                    _close()
                    _reset()
            else:
                raise TradeBuilderError(
                    f"{symbol}: unexpected '{t}' on open {direction} "
                    f"at {fill['entered_at']}"
                )
        except TradeBuilderError as e:
            # Record the offending fill but DO NOT drop trades we've
            # already completed. Reset state so subsequent fills can
            # still form valid trades — a lone bad fill in the middle
            # of a symbol's history shouldn't nuke everything around
            # it. (Pre-fix behaviour wiped every prior trade for the
            # symbol, cascade-dropping their journal + tag data.)
            errors.append(str(e))
            _reset()

    # leftover open position → open trade
    if direction is not None and entry_fills:
        _close(is_open=True)

    return trades, errors


def build_trades(
    executions: list[dict],
    resolutions: Optional[dict[str, str]] = None,
) -> tuple[list[BuiltTrade], list[str]]:
    """Build trades from a list of filled execution dicts.

    Returns (trades, errors). Errors are per-fill strings; unanswered
    flips come back as :class:`FlipInstance` (a str subclass), so
    ``[e for e in errors if isinstance(e, FlipInstance)]`` recovers the
    structured ones. A single malformed fill no longer aborts the whole
    symbol's history — see ``_build_for_symbol``.

    ``resolutions`` maps ``import_hash`` → ``RESOLUTION_*``; a flip with
    a recorded answer is applied silently.
    """
    by_symbol: dict[str, list[dict]] = defaultdict(list)
    for ex in executions:
        by_symbol[ex["symbol"]].append(ex)

    all_trades: list[BuiltTrade] = []
    errors: list[str] = []
    for symbol, fills in by_symbol.items():
        sym_trades, sym_errors = _build_for_symbol(
            symbol, fills, resolutions,
        )
        all_trades.extend(sym_trades)
        errors.extend(sym_errors)

    all_trades.sort(key=lambda t: t.entry_time)
    return all_trades, errors
