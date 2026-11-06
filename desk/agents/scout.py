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
        self.retired: set[str] = set()       # benches that expired with no pair; drained by the desk

    def scan(self) -> list[Pair]:
        mints = set()
        for feed in (self.src.dex.discover_mints, self.src.gt.discover_mints):
            try:
                mints |= feed()
            except DataUnavailable as e:
                self.log.log("error", agent="scout", detail=str(e))
        now = time.time()
        due = {m for m, t in self.benched.items() if now >= t}
        # A bench has to hold on its own, not because the mint happens to sit in `seen`. Within one
        # session the two agree -- the mint went into `seen` when it was handed over -- but `seen`
        # is rebuilt from dead|traded at startup, so after a restart a benched coin would look like
        # a coin never seen and come straight back, expiry ignored, and again when it fell due.
        candidates = (mints - self.seen - set(self.benched)) | due   # `| due` is the way back in
        if not candidates:
            self.log.log("scan", candidates=0, handed_to_filter=0, benched=len(self.benched))
            return []
        try:
            raw = self.src.dex.pairs_for_mints(candidates)
        except DataUnavailable as e:
            self.log.log("error", agent="scout", detail=str(e))
            return []       # benches keep their expiry and are retried next tick

        # one pair per mint: the most liquid in-scope pair
        best: dict[str, Pair] = {}
        with_pair: set[str] = set()
        for r in raw:
            try:
                p = Pair.from_dexscreener(r)
            except (KeyError, TypeError):
                continue
            if p.mint not in candidates:
                continue
            with_pair.add(p.mint)
            if not in_scope(p):
                continue
            if p.mint not in best or p.liquidity_usd > best[p.mint].liquidity_usd:
                best[p.mint] = p

        # The bench is spent now that we have fresh data for it. Only a fresh bench from the
        # Filter brings the coin back, so a coin that has aged out of scope stops costing calls.
        for m in sorted(due):
            self.benched.pop(m, None)
            self.seen.add(m)
            if m not in best:
                # Nothing came back for it: no pair, or no longer in scope. It is now in `seen`
                # with no bench, so nothing will ever hand it over again -- this line is the only
                # record of how that coin ended. Say so, and let the desk retire what it was owed.
                self.retired.add(m)
                self.log.log("bench_expired", mint=m,
                             detail="bench spent, no in-scope pair came back; retired unseen")
        out = list(best.values())
        for p in out:
            self.seen.add(p.mint)
            self.log.log("scouted", **p.brief())
        # `candidates` is what was asked about, not what was discovered: a mint only enters `seen`
        # once it has been handed over, so one that is out of scope is asked about again every
        # scan. Logging the whole funnel is what makes that visible instead of inferred from the
        # gap between the two ends of it.
        self.log.log("scan", candidates=len(candidates), with_pair=len(with_pair),
                     out_of_scope=len(with_pair) - len(best), handed_to_filter=len(out),
                     retried_from_bench=len(due), benched=len(self.benched))
        return out
