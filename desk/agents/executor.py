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
            # RISK sets the stop BEFORE the order goes in
            expected = self.broker.expected_buy_price(p.mint, size, p.price_usd)
            stop = self.risk.stop_for(expected)
            self.log.log("stop_set", mint=p.mint, symbol=p.symbol, expected_entry=expected, stop=stop)
            fill = self.broker.buy(p.mint, size, p.price_usd)
        except (DataUnavailable, KeyError, ValueError) as e:
            self.log.log("error", agent="executor", mint=p.mint, detail=f"buy failed: {e}")
            return None
        pos = Position(mint=p.mint, symbol=p.symbol, pair_address=p.pair_address, score=s.total,
                       entry_price=fill.price, qty=fill.qty, cost_usd=fill.usd, stop_price=stop,
                       opened_at=time.time(), peak_price=fill.price, tx_entry=fill.tx)
        # if we filled better than expected, the stop may move UP to −6% of the real fill; never down
        self.risk.guard_stop(pos, max(stop, self.risk.stop_for(fill.price)))
        self.state.traded.add(p.mint)
        self.state.positions[p.mint] = pos
        self.state.save()
        self.log.log("trade_open", mint=p.mint, symbol=p.symbol, score=s.total, entry_price=fill.price,
                     size_usd=fill.usd, qty=fill.qty, stop=pos.stop_price, balance_before=balance, tx=fill.tx)
        return pos

    def exit(self, pos: Position, ref_price: float, reason: str) -> bool:
        for attempt in range(4):
            try:
                fill = self.broker.sell(pos.mint, pos.qty, ref_price, attempt=attempt)
                break
            except (DataUnavailable, KeyError, ValueError) as e:
                self.log.log("error", agent="executor", mint=pos.mint, detail=f"sell attempt {attempt+1} failed: {e}")
                time.sleep(2)
        else:
            return False   # still open; Risk will fire again next tick
        pnl = fill.usd - pos.cost_usd
        self.state.positions.pop(pos.mint, None)
        self.state.save()
        self.log.log("trade_close", mint=pos.mint, symbol=pos.symbol, score=pos.score, entry_price=pos.entry_price,
                     exit_price=fill.price, size_usd=pos.cost_usd, proceeds_usd=fill.usd, pnl_usd=round(pnl, 4),
                     pnl_pct=round(pnl / pos.cost_usd * 100, 2), reason=reason,
                     held_min=round((time.time() - pos.opened_at) / 60, 1), peak=pos.peak_price, tx=fill.tx)
        return True
