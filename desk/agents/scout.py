"""SCOUT: find Pump.fun / Raydium pairs 30min–72h old with $300k–$20M mcap. Hands each new one to Filter."""
from __future__ import annotations

from .. import rules
from ..http import DataUnavailable
from ..models import Pair


def in_scope(p: Pair) -> bool:
    return (p.dex_id in rules.SCOUT_DEX_IDS
            and rules.SCOUT_MIN_AGE_MIN <= p.age_min <= rules.SCOUT_MAX_AGE_H * 60
            and rules.SCOUT_MIN_MCAP_USD <= p.market_cap <= rules.SCOUT_MAX_MCAP_USD)


class Scout:
    def __init__(self, sources, logger):
        self.src = sources
        self.log = logger
        self.handed_off: set[str] = set()   # mints already passed to Filter (one look per coin)

    def scan(self) -> list[Pair]:
        mints = set()
        for feed in (self.src.dex.discover_mints, self.src.gt.discover_mints):
            try:
                mints |= feed()
            except DataUnavailable as e:
                self.log.log("error", agent="scout", detail=str(e))
        mints -= self.handed_off
        try:
            raw = self.src.dex.pairs_for_mints(mints)
        except DataUnavailable as e:
            self.log.log("error", agent="scout", detail=str(e))
            return []

        # one pair per mint: the most liquid in-scope pair
        best: dict[str, Pair] = {}
        for r in raw:
            try:
                p = Pair.from_dexscreener(r)
            except (KeyError, TypeError):
                continue
            if p.mint in self.handed_off or not in_scope(p):
                continue
            if p.mint not in best or p.liquidity_usd > best[p.mint].liquidity_usd:
                best[p.mint] = p

        out = list(best.values())
        for p in out:
            self.handed_off.add(p.mint)
            self.log.log("scouted", **p.brief())
        self.log.log("scan", discovered=len(mints), handed_to_filter=len(out))
        return out
