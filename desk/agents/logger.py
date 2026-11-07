"""LOGGER: append-only JSONL of every kill, score, trade and violation, plus the morning recap."""
from __future__ import annotations

import json
import threading
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from .. import rules


class Logger:
    def __init__(self, path: Path, echo: bool = True):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.echo = echo

    def log(self, event: str, **data) -> dict:
        rec = {"ts": time.time(), "iso": datetime.now().isoformat(timespec="seconds"), "event": event, **data}
        line = json.dumps(rec, default=str)
        with self.lock:
            with self.path.open("a") as f:
                f.write(line + "\n")
        if self.echo:
            print(_pretty(rec), flush=True)
        return rec

    # convenience wrappers so every agent logs the same shape
    def kill(self, verdict, **extra) -> None:
        self.log("kill", rule=verdict.rule, rule_name=verdict.rule_name, detail=verdict.detail,
                 **extra, **verdict.pair.brief())

    def violation(self, what: str, **data) -> None:
        """An invariant was about to be broken and was blocked. Live mode refuses to start if any exist."""
        self.log("violation", what=what, **data)

    def events(self, since: float = 0.0) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text().splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("ts", 0) >= since:
                out.append(r)
        return out

    def recap(self, since: float, balance_now: float, start_balance: float, open_positions: list[dict],
              stopped: bool = False) -> str:
        ev = self.events(since)
        # Distinct coins and hand-offs are different numbers now that a benched coin comes back,
        # and the recap used to print the first under a label that promised the second. Every
        # count below says which of the two it is, because they sit next to each other.
        handoffs = [e for e in ev if e["event"] == "scouted"]
        scanned = {e["mint"] for e in handoffs}
        killed_coins = {e["mint"] for e in ev if e["event"] == "kill"}
        kills = Counter(e["rule_name"] for e in ev if e["event"] == "kill")
        low_scores = [e for e in ev if e["event"] == "score" and not e.get("passed")]
        opens = [e for e in ev if e["event"] == "trade_open"]
        closes = [e for e in ev if e["event"] == "trade_close"]
        wins = [c for c in closes if c["pnl_usd"] > 0]
        losses = [c for c in closes if c["pnl_usd"] <= 0]
        violations = [e for e in ev if e["event"] == "violation"]
        halts = [e for e in ev if e["event"] == "halt"]
        blind = [e for e in ev if e["event"] == "unprotected" and e.get("agent") == "risk"]
        recovered = [e for e in ev if e["event"] == "recovered"]
        a_bench = [e for e in ev if e["event"] == "analyst_bench"]
        expired = [e for e in ev if e["event"] == "bench_expired"]
        priors = [e for e in ev if e["event"] == "actor_prior"]
        fallbacks = [e for e in ev if e["event"] == "price_fallback"]
        errors = [e for e in ev if e["event"] == "error"]

        L = [f"# Morning recap — {datetime.now():%Y-%m-%d %H:%M}",
             f"Window: {datetime.fromtimestamp(since):%Y-%m-%d %H:%M} → now", "",
             f"- Coins seen: **{len(scanned)}** distinct, handed to Filter "
             f"**{len(handoffs)}** times (a benched coin comes back for another look)",
             f"- Filter kills: **{sum(kills.values())}** verdicts on "
             f"**{len(killed_coins)}** coins"]
        for n, name in rules.RULE_NAMES.items():
            L.append(f"  - Rule {n} `{name}`: {kills.get(name, 0)}")
        L += [f"- Scored under {rules.ANALYST_MIN_SCORE}: **{len(low_scores)}** scores on "
              f"**{len({e['mint'] for e in low_scores})}** coins"
              + (f", {len(a_bench)} of them benched for another look" if a_bench else ""),
              f"- Trades opened: **{len(opens)}**, closed: **{len(closes)}**, still open: **{len(open_positions)}**",
              f"- W/L: **{len(wins)}/{len(losses)}**"]
        if closes:
            best = max(closes, key=lambda c: c["pnl_pct"])
            worst = min(closes, key=lambda c: c["pnl_pct"])
            L.append(f"- Best: {best['symbol']} {best['pnl_pct']:+.2f}% (${best['pnl_usd']:+.2f}, {best['reason']})")
            L.append(f"- Worst: {worst['symbol']} {worst['pnl_pct']:+.2f}% (${worst['pnl_usd']:+.2f}, {worst['reason']})")
        pnl = balance_now - start_balance
        L += [f"- Final balance: **${balance_now:,.2f}** (start ${start_balance:,.2f}, {pnl:+,.2f})",
              f"- Rule violations blocked: {len(violations)}   Data errors: {len(errors)}"]
        for h in halts:
            L.append(f"- **Entries halted** ({h.get('reason')}): day open ${h.get('day_open_equity'):,.2f} "
                     f"-> ${h.get('equity'):,.2f} ({h.get('drawdown_pct'):+.2f}%)")
        for r in recovered:
            L.append(f"- **Recovered entry** {r.get('symbol')}: held {r.get('held')} - {r.get('detail')}")
        if expired:
            # A coin can also end by simply not coming back, and that ending left no trace until
            # the Scout started naming it. Worth separating: a near miss retired this way spent an
            # attempt without ever producing the second verdict it was given the attempt for.
            near = {e["mint"] for e in a_bench} & {e["mint"] for e in expired}
            L.append(f"- Benched coins retired unseen (bench expired, no in-scope pair came back): "
                     f"**{len(expired)}**"
                     + (f", of which {len(near)} a near miss whose second look never happened"
                        if near else ""))
        if priors:
            # Reported, never acted on: no rule reads the actor store. This line exists so a
            # repeat actor is visible in the morning instead of only in the JSONL.
            repeat = {}
            for e in priors:
                for c in e.get("creator_coins") or []:
                    repeat.setdefault(e.get("creator") or "?", set()).add(c)
                repeat.setdefault(e.get("creator") or "?", set()).add(e.get("symbol") or "?")
            L.append(f"- Coins whose actors the desk had already seen: **{len({e['mint'] for e in priors})}**"
                     f" ({len(priors)} looks). No verdict used this — it is recorded only.")
            for creator, coins in sorted(repeat.items(), key=lambda kv: -len(kv[1])):
                if len(coins) > 1:
                    L.append(f"  - creator `{creator[:12]}…` seen on {len(coins)}: "
                             + ", ".join(sorted(coins)))
            wallets = max((e.get("repeat_wallet_count") or 0) for e in priors)
            if wallets:
                L.append(f"  - up to {wallets} top-holder wallet(s) in one coin had been seen on another")
        if fallbacks:
            L.append(f"- Price fallback used (DexScreener missed, Jupiter answered): {len(fallbacks)} tick(s)")
        if blind:
            L.append(f"- **Stop not checked on {len(blind)} tick(s)**: no price from either source")
        if open_positions and stopped:
            L.append(f"- **{len(open_positions)} position(s) still open and no longer watched** - "
                     "the stop only runs while the desk runs: "
                     + ", ".join(f"{p['symbol']} (stop {p['stop_price']:.8g})" for p in open_positions))
        L.append("")
        if closes or opens:
            L += ["## Trades", "| coin | score | entry | size $ | exit | P/L % | reason |", "|---|---|---|---|---|---|---|"]
            closed_by_mint = {c["mint"]: c for c in closes}
            for o in opens:
                c = closed_by_mint.get(o["mint"])
                L.append(f"| {o['symbol']} | {o['score']:.0f} | {o['entry_price']:.8g} | {o['size_usd']:.2f} | "
                         + (f"{c['exit_price']:.8g} | {c['pnl_pct']:+.2f} | {c['reason']} |" if c else "open | — | — |"))
        if not opens:
            L.append("Nothing passed all gates. Empty night — balance protected.")
        return "\n".join(L)


def _pretty(r: dict) -> str:
    e = r["event"]
    t = r["iso"][11:]
    if e == "kill":
        return f"{t} KILL  {r.get('symbol'):>10}  {r['rule_name']}  {r.get('detail','')}"
    if e == "score":
        return f"{t} SCORE {r.get('symbol'):>10}  {r['total']:.1f}  {'PASS' if r['passed'] else 'dies'}"
    if e in ("trade_open", "trade_close"):
        keep = {k: v for k, v in r.items() if k not in ("ts", "iso", "event")}
        return f"{t} {e.upper()} {keep}"
    keep = {k: v for k, v in r.items() if k not in ("ts", "iso", "event")}
    return f"{t} {e} {json.dumps(keep, default=str)[:300]}"
