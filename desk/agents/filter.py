"""FILTER: 8 kill rules. One hit = dead. Missing data = dead (we cannot prove it is safe).

Safety rules (1, 2, 5, 6, 7) kill for good. Market rules (3, 4, 8) bench the coin instead: the
verdict is just as final for this look, but the coin is allowed back after rules.FILTER_BENCH_S.
"""
from __future__ import annotations

import time

from .. import rules
from ..http import DataUnavailable
from ..models import Pair, Safety, Verdict

R = rules
EXCLUDED_HOLDER_TYPES = {"AMM", "LOCKER"}  # pool vaults / lockers aren't "holders" for concentration


def build_safety(pair: Pair, report: dict, rpc_mint: dict | None, rugged_lookup) -> Safety:
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

    # Rules 5, 6, 7 — holders
    known = report.get("knownAccounts") or {}
    excluded = {a for a, k in known.items() if (k or {}).get("type") in EXCLUDED_HOLDER_TYPES}
    excluded.add(pair.pair_address)
    holders = report.get("topHolders")
    creator = report.get("creator")
    if holders is not None:
        real = [h for h in holders if h.get("owner") not in excluded and h.get("address") not in excluded]
        real.sort(key=lambda h: float(h.get("pct") or 0), reverse=True)
        s.top10_pct = sum(float(h.get("pct") or 0) for h in real[:10])
        s.bundle_pct = sum(float(h.get("pct") or 0) for h in real if h.get("insider"))
        if creator:
            via_holders = sum(float(h.get("pct") or 0) for h in real if h.get("owner") == creator)
            supply = float(tok.get("supply") or 0)
            via_balance = float(report.get("creatorBalance") or 0) / supply * 100 if supply else 0.0
            s.dev_pct = max(via_holders, via_balance)
    # Rule 7 — bundles: wallets RugCheck links into coordinated networks (funded together / bundled buys).
    # Take the larger of: their current holdings across all networks, or insider-flagged top holders.
    supply = float(tok.get("supply") or 0)
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
    def __init__(self, sources, logger):
        self.src = sources
        self.log = logger
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
        self.log.kill(v, permanent=bool(permanent))
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
        safety = build_safety(pair, report, rpc_mint, self.src.rug.was_rugged)
        hits = safety_rules(safety)
        if hits:
            return self._kill(pair, hits, safety)
        self.log.log("survived_filter", **pair.brief(), top10=safety.top10_pct, dev=safety.dev_pct,
                     bundle=safety.bundle_pct, lp_locked=safety.lp_locked_pct, holders=safety.holders,
                     notes=safety.notes)
        return Verdict(pair, True, safety=safety)
