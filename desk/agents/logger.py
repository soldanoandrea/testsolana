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
    def kill(self, verdict) -> None:
        self.log("kill", rule=verdict.rule, rule_name=verdict.rule_name, detail=verdict.detail, **verdict.pair.brief())

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

    def recap(self, since: float, balance_now: float, start_balance: float, open_positions: list[dict]) -> str:
        ev = self.events(since)
        scanned = {e["mint"] for e in ev if e["event"] == "scouted"}
        kills = Counter(e["rule_name"] for e in ev if e["event"] == "kill")
        low_scores = [e for e in ev if e["event"] == "score" and not e.get("passed")]
        opens = [e for e in ev if e["event"] == "trade_open"]
        closes = [e for e in ev if e["event"] == "trade_close"]
        wins = [c for c in closes if c["pnl_usd"] > 0]
        losses = [c for c in closes if c["pnl_usd"] <= 0]
        violations = [e for e in ev if e["event"] == "violation"]
        errors = [e for e in ev if e["event"] == "error"]

        L = [f"# Morning recap — {datetime.now():%Y-%m-%d %H:%M}",
             f"Window: {datetime.fromtimestamp(since):%Y-%m-%d %H:%M} → now", "",
             f"- Scanned (pairs handed to Filter): **{len(scanned)}**",
             f"- Killed by Filter: **{sum(kills.values())}**"]
        for n, name in rules.RULE_NAMES.items():
            L.append(f"  - Rule {n} `{name}`: {kills.get(name, 0)}")
        L += [f"- Scored under {rules.ANALYST_MIN_SCORE} (Analyst kill): **{len(low_scores)}**",
              f"- Trades opened: **{len(opens)}**, closed: **{len(closes)}**, still open: **{len(open_positions)}**",
              f"- W/L: **{len(wins)}/{len(losses)}**"]
        if closes:
            best = max(closes, key=lambda c: c["pnl_pct"])
            worst = min(closes, key=lambda c: c["pnl_pct"])
            L.append(f"- Best: {best['symbol']} {best['pnl_pct']:+.2f}% (${best['pnl_usd']:+.2f}, {best['reason']})")
            L.append(f"- Worst: {worst['symbol']} {worst['pnl_pct']:+.2f}% (${worst['pnl_usd']:+.2f}, {worst['reason']})")
        pnl = balance_now - start_balance
        L += [f"- Final balance: **${balance_now:,.2f}** (start ${start_balance:,.2f}, {pnl:+,.2f})",
              f"- Rule violations blocked: {len(violations)}   Data errors: {len(errors)}", ""]
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
