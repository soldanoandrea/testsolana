"""RISK: stop at −6% set before the order, never lowered; trailing stop after +12%; exits only on stop/trail."""
from __future__ import annotations

from .. import rules
from ..models import Position


def initial_stop(expected_entry_price: float) -> float:
    return expected_entry_price * (1 + rules.RISK_STOP_PCT / 100)


class Risk:
    def __init__(self, logger):
        self.log = logger

    def stop_for(self, expected_entry_price: float) -> float:
        return initial_stop(expected_entry_price)

    def update(self, pos: Position, price: float) -> str | None:
        """Feed a new price. Returns an exit reason, or None to keep holding."""
        if price <= 0:
            return None
        if price > pos.peak_price:
            pos.peak_price = price
        gain_pct = (pos.peak_price / pos.entry_price - 1) * 100
        if not pos.trailing and gain_pct >= rules.RISK_TRAIL_ACTIVATE_PCT:
            pos.trailing = True
            self.log.log("trail_on", mint=pos.mint, symbol=pos.symbol, peak=pos.peak_price, gain_pct=round(gain_pct, 2))
        if pos.trailing:
            trail = pos.peak_price * (1 - rules.RISK_TRAIL_DISTANCE_PCT / 100)
            old = pos.stop_price
            if pos.raise_stop(trail):
                self.log.log("stop_raised", mint=pos.mint, symbol=pos.symbol, old=old, new=trail)
        if price <= pos.stop_price:
            return "trailing_stop" if pos.trailing else "stop_loss"
        return None

    def guard_stop(self, pos: Position, proposed: float) -> None:
        """Any code path that wants to set a stop goes through here. Lowering is blocked and logged."""
        if proposed < pos.stop_price:
            self.log.violation("attempt_to_lower_stop", mint=pos.mint, current=pos.stop_price, proposed=proposed)
            return
        pos.raise_stop(proposed)
