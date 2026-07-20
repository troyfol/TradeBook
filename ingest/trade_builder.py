"""Group filled executions into logical trades.

State machine per symbol (chronological order, Type column is authoritative):
    Buy            → open / add to Long
    Sell           → reduce / close Long
    Sell Short     → open / add to Short
    Buy to Cover   → reduce / close Short

Position-flip handling is intentionally not supported (the broker rejects
these at order time, per user workflow). Any exit exceeding the open
position raises TradeBuilderError.
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
    execution_hashes = [f["import_hash"] for f in entry_fills + exit_fills]

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
    exit_time = max(f["entered_at"] for f in exit_fills)
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
        hold_duration_seconds=int((exit_time - entry_time).total_seconds()),
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


def _build_for_symbol(
    symbol: str, fills: list[dict],
) -> tuple[list[BuiltTrade], list[str]]:
    """State-machine over one symbol's fills.

    Returns ``(trades, errors)``. When a fill breaks state (e.g. a
    Sell that would flip the position to negative, or an exit with no
    open position), the error is recorded and the builder **resets**
    so subsequent fills can still form valid trades. Completed trades
    from before the bad fill are preserved — this is critical: a
    single rogue execution must not wipe out every prior trade for
    the same symbol (that cascade-drops journal entries + tags during
    ``rebuild_trades``).
    """
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

    for fill in fills:
        t = fill["type"]
        qty = fill["qty_filled"]
        try:
            if direction is None:
                if t == TYPE_BUY:
                    direction = DIR_LONG
                    entry_fills = [fill]
                    exit_fills = []
                    position = qty
                elif t == TYPE_SELL_SHORT:
                    direction = DIR_SHORT
                    entry_fills = [fill]
                    exit_fills = []
                    position = qty
                else:
                    raise TradeBuilderError(
                        f"{symbol}: '{t}' at {fill['entered_at']} "
                        f"has no open position"
                    )
                continue

            if direction == DIR_LONG:
                if t == TYPE_BUY:
                    entry_fills.append(fill)
                    position += qty
                elif t == TYPE_SELL:
                    exit_fills.append(fill)
                    position -= qty
                    if position == 0:
                        trades.append(_finalize(
                            symbol, direction, entry_fills, exit_fills, False,
                        ))
                        _reset()
                    elif position < 0:
                        raise TradeBuilderError(
                            f"{symbol}: Sell at {fill['entered_at']} "
                            f"would flip Long→Short"
                        )
                else:
                    raise TradeBuilderError(
                        f"{symbol}: unexpected '{t}' on open Long "
                        f"at {fill['entered_at']}"
                    )
            else:  # DIR_SHORT
                if t == TYPE_SELL_SHORT:
                    entry_fills.append(fill)
                    position += qty
                elif t == TYPE_BUY_TO_COVER:
                    exit_fills.append(fill)
                    position -= qty
                    if position == 0:
                        trades.append(_finalize(
                            symbol, direction, entry_fills, exit_fills, False,
                        ))
                        _reset()
                    elif position < 0:
                        raise TradeBuilderError(
                            f"{symbol}: Buy to Cover at {fill['entered_at']} "
                            f"would flip Short→Long"
                        )
                else:
                    raise TradeBuilderError(
                        f"{symbol}: unexpected '{t}' on open Short "
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
        trades.append(
            _finalize(symbol, direction, entry_fills, exit_fills, True),
        )

    return trades, errors


def build_trades(executions: list[dict]) -> tuple[list[BuiltTrade], list[str]]:
    """Build trades from a list of filled execution dicts.

    Returns (trades, errors). Errors are per-fill strings — a single
    malformed fill no longer aborts the whole symbol's history (see
    ``_build_for_symbol`` for the recovery behaviour).
    """
    by_symbol: dict[str, list[dict]] = defaultdict(list)
    for ex in executions:
        by_symbol[ex["symbol"]].append(ex)

    all_trades: list[BuiltTrade] = []
    errors: list[str] = []
    for symbol, fills in by_symbol.items():
        sym_trades, sym_errors = _build_for_symbol(symbol, fills)
        all_trades.extend(sym_trades)
        errors.extend(sym_errors)

    all_trades.sort(key=lambda t: t.entry_time)
    return all_trades, errors
