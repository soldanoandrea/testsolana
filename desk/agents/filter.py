"""FILTER: 8 kill rules. One hit = dead. Missing data = dead (we cannot prove it is safe).

Safety rules (1, 2, 5, 6, 7) kill for good. Market rules (3, 4, 8) bench the coin instead: the
verdict is just as final for this look, but the coin is allowed back after rules.FILTER_BENCH_S.
"""
from __future__ import annotations

import time

from .. import rules
from ..holders import Holders, HolderTable, RugCheckHolders
from ..http import DataUnavailable
from ..models import Pair, Safety, Verdict

R = rules
EXCLUDED_HOLDER_TYPES = {"AMM", "LOCKER"}  # kept: desk/holders.py NOT_A_WALLET is the same set


def rugcheck_only(pair: Pair, report: dict) -> HolderTable:
    """The holder table the Filter used to build by hand, for callers that pass none."""
    return Holders([RugCheckHolders()]).table(pair, report)


def build_safety(pair: Pair, report: dict, rpc_mint: dict | None, rugged_lookup,
                 holders: HolderTable | None = None) -> Safety:
    """Turn a RugCheck report (+ optional RPC cross-check) into Safety facts."""
    s = Safety()
    tok = report.get("token") or {}

    # Rule 1 — authorities. RPC is ground truth; RugCheck must agree when both are present.
    rc_mint, rc_freeze = tok.get("mintAuthority"), tok.get("freezeAuthority")
    if rpc_mint is not None:
        mint_auth, freeze_auth = rpc_mint.get("mintAuthority"), rpc_mint.get("freezeAuthority")
        if (mint_auth is None) != (rc_mint is None) or (freeze_auth is None) != (rc_freeze is None):
            s.notes.append("rpc/rugcheck authority mismatch")
            mint_auth = mint_auth or rc_mint or "mismatch"
            freeze_auth = freeze_auth or rc_freeze or "mismatch"
    elif "mintAuthority" in tok and "freezeAuthority" in tok:
        mint_auth, freeze_auth = rc_mint, rc_freeze
    else:
        mint_auth = freeze_auth = "unknown"
    s.mint_authority_revoked = mint_auth is None if mint_auth != "unknown" else None
    s.freeze_authority_revoked = freeze_auth is None if freeze_auth != "unknown" else None

    # Rule 2 — LP of the pair we would trade
    markets = report.get("markets") or []
    m = next((x for x in markets if x.get("pubkey") == pair.pair_address), None)
    if m is None and markets:
        m = max(markets, key=lambda x: float((x.get("lp") or {}).get("lpLockedUSD") or 0)
                + float((x.get("lp") or {}).get("quoteUSD") or 0))
    if m and (m.get("lp") or {}).get("lpLockedPct") is not None:
        s.lp_locked_pct = float(m["lp"]["lpLockedPct"])

    # Rules 5, 6, 7 — holders. `holders` is a HolderTable (desk/holders.py): the chain when an
    # endpoint can answer it, RugCheck's own field otherwise, absent when neither could. With no
    # table the concentration share has no input and R5 kills -- correct, and the reason the
    # provider chain exists at all, since the kill was a third party's uptime and not the coin.
    table = holders if holders is not None else rugcheck_only(pair, report)
    s.holders_source = table.source
    real = table.wallets          # vaults, bonding curves and lockers already removed
    creator = report.get("creator")
    supply = float(tok.get("supply") or 0)
    if table.present:
        s.top10_pct = sum(h.pct for h in real[:10])
        if table.insiders_known:
            s.bundle_pct = sum(h.pct for h in real if h.insider)
        else:
            # This provider does not flag insiders, so R7 rests entirely on the network graph
            # below -- which RugCheck computes independently of the holder table.
            s.bundle_pct = 0.0
            s.notes.append(f"insider flags unavailable from {table.source}")
        if table.excluded:
            s.notes.append("not holders: " + ", ".join(
                f"{h.owner[:8]}…{(' ' + h.known) if h.known else ''} {h.pct:.2f}%"
                for h in table.excluded[:6]))
    else:
        s.notes.append(f"no holder table ({table.note})")
    # The dev's share does not need the holder table: creatorBalance is a share of supply by
    # itself. This used to sit inside the branch above, so one absent field killed on R6 as well
    # as on R5 -- two rules reporting the same gap as two independent problems.
    if creator:
        via_holders = sum(h.pct for h in real if h.owner == creator) if table.present else 0.0
        via_balance = None
        if report.get("creatorBalance") is not None and supply:
            via_balance = float(report["creatorBalance"]) / supply * 100
        if via_balance is not None or table.present:
            s.dev_pct = max(via_holders, via_balance or 0.0)
    # Rule 7 — bundles: wallets RugCheck links into coordinated networks (funded together / bundled buys).
    # Take the larger of: their current holdings across all networks, or insider-flagged top holders.
    nets = report.get("insiderNetworks")
    if nets and supply:
        # RugCheck gives each network's holdings but not its wallet list, so a wallet in two
        # networks is counted twice and this sum can exceed the supply -- MNST logged 216.8% on
        # 2026-11-06. It cannot be deduplicated from this data, so it is a LOWER BOUND on the
        # bundled share, not a share of supply, and it is named that way wherever it is reported.
        # The verdict is unaffected: anything over FILTER_MAX_BUNDLE_PCT kills either way.
        net_pct = sum(float(n.get("currentHolding") or 0) for n in nets) / supply * 100
        s.bundle_pct = max(s.bundle_pct or 0.0, net_pct)
        s.bundle_pct_is_sum = net_pct >= s.bundle_pct
        s.notes.append(f"insider networks: {len(nets)} summing to {net_pct:.1f}% (wallets may overlap)")
    elif report.get("graphInsidersDetected") and not nets:
        s.bundle_pct = None   # insiders detected but sizes missing -> can't verify -> Filter kills
        s.notes.append("insiders detected without network sizes")
    elif "graphInsidersDetected" in report and s.bundle_pct is None:
        # RugCheck ran its insider graph and found no network. That answer is computed without
        # `topHolders`, so it still holds when the holder table is absent -- otherwise one missing
        # field fires R5 and R7 together and reports a single gap as two independent problems.
        s.bundle_pct = 0.0
        s.notes.append("no insider network in the graph (holder table absent)")

    # Rule 6 — dev history. Check the creator's other launches for rugs (fail closed on lookup error).
    if creator:
        prior = [t for t in (report.get("creatorTokens") or []) if t.get("mint") != pair.mint]
        prior.sort(key=lambda t: t.get("createdAt") or "", reverse=True)
        try:
            s.dev_past_rugs = sum(1 for t in prior[:5] if rugged_lookup(t["mint"]))
        except DataUnavailable:
            s.dev_past_rugs = None
            s.notes.append("dev history lookup failed")
    s.holders = report.get("totalHolders")
    return s


def market_rules(p: Pair) -> list[tuple[int, str]]:
    hits = []
    if p.liquidity_usd < R.FILTER_MIN_LIQUIDITY_USD:
        hits.append((3, f"liquidity ${p.liquidity_usd:,.0f} < ${R.FILTER_MIN_LIQUIDITY_USD:,}"))
    if p.volume_h1 < R.FILTER_MIN_VOLUME_H1_USD:
        hits.append((4, f"1h volume ${p.volume_h1:,.0f} < ${R.FILTER_MIN_VOLUME_H1_USD:,}"))
    if p.price_change_h1 >= R.FILTER_MAX_PRICE_CHANGE_H1_PCT:
        hits.append((8, f"1h change {p.price_change_h1:+.1f}% >= +{R.FILTER_MAX_PRICE_CHANGE_H1_PCT:.0f}%"))
    return hits


def safety_rules(s: Safety) -> list[tuple[int, str]]:
    hits = []
    if s.mint_authority_revoked is None or s.freeze_authority_revoked is None:
        hits.append((1, "authorities unverifiable"))
    elif not (s.mint_authority_revoked and s.freeze_authority_revoked):
        hits.append((1, f"mint revoked={s.mint_authority_revoked} freeze revoked={s.freeze_authority_revoked}"))
    if s.lp_locked_pct is None:
        hits.append((2, "LP lock unverifiable"))
    elif s.lp_locked_pct < R.FILTER_MIN_LP_LOCKED_PCT:
        hits.append((2, f"LP locked/burned {s.lp_locked_pct:.1f}% < {R.FILTER_MIN_LP_LOCKED_PCT:.0f}%"))
    if s.top10_pct is None:
        hits.append((5, "holders unverifiable"))
    elif s.top10_pct > R.FILTER_MAX_TOP10_PCT:
        hits.append((5, f"top10 {s.top10_pct:.1f}% > {R.FILTER_MAX_TOP10_PCT:.0f}%"))
    if s.dev_pct is None or s.dev_past_rugs is None:
        hits.append((6, "dev wallet/history unverifiable"))
    elif s.dev_pct > R.FILTER_MAX_DEV_PCT:
        hits.append((6, f"dev holds {s.dev_pct:.2f}% > {R.FILTER_MAX_DEV_PCT:.0f}%"))
    elif s.dev_past_rugs > 0:
        hits.append((6, f"dev has {s.dev_past_rugs} past rug(s)"))
    if s.bundle_pct is None:
        hits.append((7, "bundle share unverifiable"))
    elif s.bundle_pct > R.FILTER_MAX_BUNDLE_PCT:
        qual = " (summed over networks, wallets may overlap)" if getattr(s, "bundle_pct_is_sum", False) else ""
        hits.append((7, f"bundled/insider wallets hold >={s.bundle_pct:.1f}%{qual} > {R.FILTER_MAX_BUNDLE_PCT:.0f}%"))
    return hits


class Filter:
    def __init__(self, sources, logger, actors=None, holders=None):
        self.src = sources
        self.log = logger
        # Holder providers, tried in order (desk/holders.py). None = RugCheck's field alone, which
        # is what the Filter did before there was a chain.
        self.holders = holders
        # Actor memory. Optional so the Filter can be tested without a store, and because it
        # records and reports only: no rule below reads it, and no verdict depends on it.
        self.actors = actors
        self.dead: set[str] = set()
        self.benched: dict[str, float] = {}     # mint -> earliest retry, market conditions only

    def _kill(self, pair: Pair, hits: list[tuple[int, str]], safety=None) -> Verdict:
        rule, detail = min(hits)
        extra = "; also " + "; ".join(f"R{n}: {d}" for n, d in sorted(hits)[1:]) if len(hits) > 1 else ""
        v = Verdict(pair, False, rule, R.RULE_NAMES[rule], detail + extra, safety)
        # One permanent rule among the hits is enough to kill it for good.
        permanent = [n for n, _ in hits if n not in R.FILTER_TRANSIENT_RULES]
        if permanent:
            self.dead.add(pair.mint)
            self.benched.pop(pair.mint, None)
        else:
            self.benched[pair.mint] = time.time() + R.FILTER_BENCH_S
        self.log.kill(v, permanent=bool(permanent),
                      holders_src=safety.holders_source if safety is not None else None)
        return v

    def check(self, pair: Pair) -> Verdict:
        if pair.mint in self.dead:  # dead is dead
            return Verdict(pair, False, None, "ALREADY_DEAD", "killed earlier")
        # cheap market rules first — no need to hit on-chain sources for a coin that's already dead
        hits = market_rules(pair)
        if hits:
            return self._kill(pair, hits)
        try:
            report = self.src.rug.report(pair.mint)
        except DataUnavailable as e:
            return self._kill(pair, [(1, f"safety data unavailable: {e}")])
        try:
            rpc_mint = self.src.rpc.mint_info(pair.mint)
        except DataUnavailable:
            rpc_mint = None
        table = self.holders.table(pair, report, rpc_mint) if self.holders else None
        safety = build_safety(pair, report, rpc_mint, self.src.rug.was_rugged, holders=table)
        # Priors are read BEFORE this look is recorded, so a coin can never be its own prior.
        priors = self.actors.priors(pair.mint, report) if self.actors else None
        if priors is not None and priors.any:
            self.log.log("actor_prior", mint=pair.mint, symbol=pair.symbol, **priors.brief())
        hits = safety_rules(safety)
        if hits:
            v = self._kill(pair, hits, safety)
        else:
            self.log.log("survived_filter", **pair.brief(), top10=safety.top10_pct, dev=safety.dev_pct,
                         bundle=safety.bundle_pct, lp_locked=safety.lp_locked_pct, holders=safety.holders,
                         notes=safety.notes, holders_src=safety.holders_source)
            v = Verdict(pair, True, safety=safety)
        if self.actors is not None:
            # Every report the desk paid for is written down, pass or kill. The holder table is
            # there or not there by RugCheck's own intermittency, so a look that has one is the
            # only chance to write it down.
            try:
                self.actors.record(pair, report, safety, v.rule_name or "SURVIVED", holders=table)
            except OSError as e:
                self.log.log("error", agent="actors", mint=pair.mint, detail=f"actor record failed: {e}")
        return v
