"""ANALYST: score each Filter survivor 0–100. 70+ goes to Executor, under 70 dies.

Each component maps a real on-chain/market number onto 0..1, multiplied by its weight
from rules.ANALYST_WEIGHTS. A component with no data scores 0 — we never guess upward.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from .. import rules
from ..http import DataUnavailable
from ..models import Pair, Score, Verdict


def lin(x: float, lo: float, hi: float) -> float:
    """0 at <=lo, 1 at >=hi, linear between."""
    if x is None:
        return 0.0
    if hi == lo:
        return 1.0 if x >= hi else 0.0
    return max(0.0, min(1.0, (x - lo) / (hi - lo)))


def score_components(inp: dict) -> dict[str, float]:
    """Pure function: inputs -> 0..1 per component. Tested in tests/test_analyst.py."""
    c = {}
    # Momentum: rising but not parabolic (80%+ already killed). Best band +15..+50% in 1h, m5 not dumping.
    h1, m5 = inp.get("chg_h1"), inp.get("chg_m5")
    mom = lin(h1, 0, 15) if h1 is not None else 0.0
    if h1 is not None and h1 > 50:
        mom *= 1 - lin(h1, 50, 80) * 0.5          # fade toward the 80% kill line
    if m5 is not None and m5 < 0:
        mom *= 1 - lin(-m5, 0, 8)                  # current 5m dump erases momentum
    c["momentum"] = mom
    # Unique buyers in the last hour (GeckoTerminal distinct wallets)
    c["unique_buyers"] = lin(inp.get("buyers_h1"), 50, 400)
    # Buy/sell ratio: distinct buyers / distinct sellers when known, else txn counts
    c["buy_sell_ratio"] = lin(inp.get("buy_sell_ratio"), 1.0, 2.0)
    # Holder growth, % per hour over the observation window
    c["holder_growth"] = lin(inp.get("holder_growth_pct_h"), 0, 20)
    # Liquidity / market cap
    c["liquidity_to_mcap"] = lin(inp.get("liq_to_mcap"), 0.05, 0.20)
    # Smart wallets holding the token
    sw = inp.get("smart_wallets")
    c["smart_wallets"] = 0.0 if not sw else (0.6 if sw == 1 else 1.0)
    return c


def total(components: dict[str, float]) -> float:
    return round(sum(components[k] * w for k, w in rules.ANALYST_WEIGHTS.items()), 1)


@dataclass
class Watch:
    pair: Pair
    holders0: int
    t0: float


class Analyst:
    def __init__(self, sources, logger, smart_wallets: list[str], window_s: int):
        self.src = sources
        self.log = logger
        self.smart_wallets = smart_wallets
        self.window_s = window_s
        self.watching: dict[str, Watch] = {}
        self.scored: set[str] = set()

    def observe(self, v: Verdict) -> None:
        """Start the holder-growth clock for a Filter survivor."""
        if v.pair.mint in self.scored or v.pair.mint in self.watching:
            return
        self.watching[v.pair.mint] = Watch(v.pair, int(v.safety.holders or 0), time.time())
        self.log.log("observing", mint=v.pair.mint, symbol=v.pair.symbol, holders=v.safety.holders,
                     window_s=self.window_s)

    def due(self) -> list[Watch]:
        now = time.time()
        return [w for w in self.watching.values() if now - w.t0 >= self.window_s]

    def score(self, w: Watch, fresh: Pair, fresh_verdict: Verdict) -> Score:
        """Score a coin once its window is up. `fresh_verdict` is Filter's re-check on fresh data."""
        self.watching.pop(w.pair.mint, None)
        self.scored.add(w.pair.mint)
        inp: dict = {"chg_h1": fresh.price_change_h1, "chg_m5": fresh.price_change_m5,
                     "liq_to_mcap": fresh.liquidity_usd / fresh.market_cap if fresh.market_cap else None}
        try:
            act = self.src.gt.pool_h1_activity(fresh.pair_address)
            inp["buyers_h1"] = act["buyers"]
            inp["buy_sell_ratio"] = act["buyers"] / max(act["sellers"], 1)
        except (DataUnavailable, KeyError, TypeError):
            inp["buyers_h1"] = None
            inp["buy_sell_ratio"] = fresh.buys_h1 / max(fresh.sells_h1, 1) if fresh.buys_h1 else None
        h1 = fresh_verdict.safety.holders if fresh_verdict.safety else None
        dt = time.time() - w.t0
        if h1 and w.holders0 and dt > 0:
            inp["holder_growth_pct_h"] = (h1 - w.holders0) / w.holders0 * 100 * 3600 / dt
        inp["holders"] = [w.holders0, h1]
        inp["smart_wallets"] = self._smart_wallets_in(fresh.mint)

        comps = score_components(inp)
        s = Score(fresh, total(comps), {k: round(v * rules.ANALYST_WEIGHTS[k], 1) for k, v in comps.items()}, inp)
        passed = s.total >= rules.ANALYST_MIN_SCORE
        self.log.log("score", mint=fresh.mint, symbol=fresh.symbol, total=s.total, passed=passed,
                     parts=s.parts, inputs=inp)
        return s

    def _smart_wallets_in(self, mint: str):
        if not self.smart_wallets:
            return None
        n = 0
        for w in self.smart_wallets:
            try:
                if self.src.rpc.token_balance(w, mint) > 0:
                    n += 1
            except DataUnavailable:
                continue
        return n
