"""EXECUTOR: capped size per trade, capped number of open positions, one entry per coin,
never averages down. The desk can halt entries entirely (daily loss limit)."""
from __future__ import annotations

import math
import time

from .. import rules
from ..http import DataUnavailable
from ..models import Position, Score


class Executor:
    def __init__(self, broker, risk, state, logger):
        self.broker, self.risk, self.state, self.log = broker, risk, state, logger
        self.halted = False
        self.halt_reason = ""

    def size_for(self, balance: float) -> float:
        """Floor to whole cents: round() can round UP and breach the cap by half a cent."""
        return math.floor(balance * rules.EXECUTOR_MAX_POSITION_PCT) / 100.0

    def halt(self, reason: str) -> None:
        """Refuse every new entry until resume(). Open positions keep their stops."""
        self.halted, self.halt_reason = True, reason

    def resume(self) -> None:
        self.halted, self.halt_reason = False, ""

    def enter(self, s: Score) -> Position | None:
        p = s.pair
        if s.total < rules.ANALYST_MIN_SCORE:
            self.log.violation("executor_got_sub70", mint=p.mint, score=s.total)
            return None
        if p.mint in self.state.traded or p.mint in self.state.positions:
            self.log.log("skip", mint=p.mint, symbol=p.symbol, reason="one entry per coin — already traded")
            return None
        if self.halted:
            self.log.log("skip", mint=p.mint, symbol=p.symbol, reason=f"entries halted — {self.halt_reason}")
            return None
        if len(self.state.positions) >= rules.EXECUTOR_MAX_OPEN_POSITIONS:
            self.log.log("skip", mint=p.mint, symbol=p.symbol,
                         reason=f"max {rules.EXECUTOR_MAX_OPEN_POSITIONS} open positions")
            return None
        try:
            balance = self.broker.balance_usd()
            size = self.size_for(balance)
            if size < 1.0:
                self.log.log("skip", mint=p.mint, symbol=p.symbol, reason=f"size ${size} too small")
                return None
            assert size <= balance * rules.EXECUTOR_MAX_POSITION_PCT / 100 + 1e-9
            # RISK commits to a stop BEFORE the order goes in: no order ever goes in without one.
            expected = self.broker.expected_buy_price(p.mint, size, p.price_usd)
            committed = self.risk.stop_for(expected)
            self.log.log("stop_set", mint=p.mint, symbol=p.symbol, expected_entry=expected, stop=committed)
            # On disk BEFORE the order goes out. Between a confirmed buy and the position being
            # stored there is a window where a crash would leave tokens in the wallet that the
            # desk knows nothing about: no stop, and not in `traded`, so it could buy them again.
            self.state.begin_entry(mint=p.mint, symbol=p.symbol, pair_address=p.pair_address,
                                   score=s.total, usd=size, stop=committed, expected=expected)
            fill = self.broker.buy(p.mint, size, p.price_usd)
        except (DataUnavailable, KeyError, ValueError) as e:
            self.log.log("error", agent="executor", mint=p.mint, detail=f"buy failed: {e}")
            # "Not confirmed" is not "did not happen": the swap may still have landed.
            return self.reconcile_pending()
        # The stop is placed off the REAL fill, so it is exactly RISK_STOP_PCT from what we paid.
        # `committed` was quoted off the worst case allowed by slippage, so using it here would leave
        # live with a tighter stop than paper and would let DESK_LIVE_SLIPPAGE_BPS move a trading rule.
        # There is no position yet, so this is the stop being SET, not moved: nothing is being lowered.
        pos = Position(mint=p.mint, symbol=p.symbol, pair_address=p.pair_address, score=s.total,
                       entry_price=fill.price, qty=fill.qty, cost_usd=fill.usd,
                       stop_price=self.risk.stop_for(fill.price),
                       opened_at=time.time(), peak_price=fill.price, tx_entry=fill.tx)
        if abs(pos.stop_price - committed) > 1e-12:
            self.log.log("stop_rebased", mint=p.mint, symbol=p.symbol, committed=committed,
                         expected_entry=expected, actual_entry=fill.price, stop=pos.stop_price)
        self.state.open_position(pos)
        assert pos.stop_price >= pos.stop_floor() - 1e-12
        self.log.log("trade_open", mint=p.mint, symbol=p.symbol, score=s.total, entry_price=fill.price,
                     size_usd=fill.usd, qty=fill.qty, stop=pos.stop_price, balance_before=balance, tx=fill.tx)
        return pos

    def reconcile_pending(self) -> Position | None:
        """An order was placed and may not have been recorded. Ask the chain what we actually hold.

        Called at startup and whenever a buy raises. Never invents a position: if the holdings
        cannot be read, the pending record is kept so the next attempt can settle it.
        """
        pend = self.state.pending_entry
        if not pend:
            return None
        mint = pend["mint"]
        if mint in self.state.positions:          # already recorded; nothing to recover
            self.state.clear_entry()
            return self.state.positions[mint]
        try:
            qty = self.broker.held_qty(mint)
        except (DataUnavailable, KeyError, ValueError) as e:
            self.log.log("error", agent="executor", mint=mint,
                         detail=f"cannot reconcile pending entry, keeping the record: {e}")
            return None
        if qty <= 0:
            # The order never landed. Burn the coin anyway: a half-known entry is exactly where a
            # double buy comes from, and "one entry per coin, ever" is the stricter promise.
            self.state.traded.add(mint)
            self.state.clear_entry()
            self.log.log("recovered", mint=mint, symbol=pend.get("symbol"), held=0.0,
                         detail="order never filled; coin burned so it cannot be entered twice")
            return None
        entry = pend["usd"] / qty                 # exactly how a fill price is computed
        pos = Position(mint=mint, symbol=pend.get("symbol", "?"), pair_address=pend.get("pair_address", ""),
                       score=pend.get("score", 0.0), entry_price=entry, qty=qty, cost_usd=pend["usd"],
                       stop_price=self.risk.stop_for(entry), opened_at=pend.get("ts", time.time()),
                       peak_price=entry)
        self.state.open_position(pos)
        self.log.log("recovered", mint=mint, symbol=pos.symbol, held=qty, entry_price=entry,
                     stop=pos.stop_price, detail="tokens were held with no position recorded; "
                                                 "adopted and the stop is running again")
        self.log.log("trade_open", mint=mint, symbol=pos.symbol, score=pos.score, entry_price=entry,
                     size_usd=pend["usd"], qty=qty, stop=pos.stop_price, recovered=True, tx="")
        return pos

    def exit(self, pos: Position, ref_price: float, reason: str) -> bool:
        for _ in range(4):
            try:
                fill = self.broker.sell(pos.mint, pos.qty, ref_price, attempt=pos.sell_attempts)
                break
            except (DataUnavailable, KeyError, ValueError) as e:
                pos.sell_attempts += 1
                self.log.log("error", agent="executor", mint=pos.mint,
                             detail=f"sell attempt {pos.sell_attempts} failed: {e}")
                time.sleep(2)
        else:
            self.state.save()   # keep the escalation: Risk fires again next tick, wider
            return False        # still open
        pnl = fill.usd - pos.cost_usd
        self.state.close_position(pos.mint)
        self.log.log("trade_close", mint=pos.mint, symbol=pos.symbol, score=pos.score, entry_price=pos.entry_price,
                     exit_price=fill.price, size_usd=pos.cost_usd, proceeds_usd=fill.usd, pnl_usd=round(pnl, 4),
                     pnl_pct=round(pnl / pos.cost_usd * 100, 2), reason=reason,
                     held_min=round((time.time() - pos.opened_at) / 60, 1), peak=pos.peak_price, tx=fill.tx)
        return True
