"""SCOUT: find Pump.fun / Raydium pairs 30min–72h old with $300k–$20M mcap. Hands each new one to Filter.

A coin is handed over once, unless the Filter benched it on a market rule: a benched coin comes back
when its bench expires, and the Filter either benches it again or settles the matter.
"""
from __future__ import annotations

import time

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
        self.seen: set[str] = set()          # mints already passed to Filter
        self.benched: dict[str, float] = {}  # shared with Filter: mint -> earliest retry

    def scan(self) -> list[Pair]:
        mints = set()
        for feed in (self.src.dex.discover_mints, self.src.gt.discover_mints):
            try:
                mints |= feed()
            except DataUnavailable as e:
                self.log.log("error", agent="scout", detail=str(e))
        now = time.time()
        due = {m for m, t in self.benched.items() if now >= t}
        candidates = (mints - self.seen) | due      # a due bench overrides "already seen"
        if not candidates:
            self.log.log("scan", discovered=0, handed_to_filter=0, benched=len(self.benched))
            return []
        try:
            raw = self.src.dex.pairs_for_mints(candidates)
        except DataUnavailable as e:
            self.log.log("error", agent="scout", detail=str(e))
            return []       # benches keep their expiry and are retried next tick

        # one pair per mint: the most liquid in-scope pair
        best: dict[str, Pair] = {}
        for r in raw:
            try:
                p = Pair.from_dexscreener(r)
            except (KeyError, TypeError):
                continue
            if p.mint not in candidates or not in_scope(p):
                continue
            if p.mint not in best or p.liquidity_usd > best[p.mint].liquidity_usd:
                best[p.mint] = p

        # The bench is spent now that we have fresh data for it. Only a fresh bench from the
        # Filter brings the coin back, so a coin that has aged out of scope stops costing calls.
        for m in due:
            self.benched.pop(m, None)
            self.seen.add(m)
        out = list(best.values())
        for p in out:
            self.seen.add(p.mint)
            self.log.log("scouted", **p.brief())
        self.log.log("scan", discovered=len(candidates), handed_to_filter=len(out),
                     retried_from_bench=len(due), benched=len(self.benched))
        return out
