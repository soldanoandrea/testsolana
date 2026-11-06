"""The desk: wires the six agents together with hard handoffs.

    SCOUT --pairs--> FILTER --survivors--> ANALYST (watch holders) --> FILTER (re-check, fresh data)
          --> ANALYST (score) --70+--> EXECUTOR <--stop first-- RISK --exit signal--> EXECUTOR
    LOGGER records every step.
"""
from __future__ import annotations

import os
import signal
import threading
import time
from datetime import datetime

from . import rules
from .agents.analyst import Analyst
from .agents.executor import Executor
from .agents.filter import Filter
from .agents.logger import Logger
from .agents.risk import Risk
from .agents.scout import Scout
from .brokers import LiveBroker, PaperBroker
from .config import Config
from .http import DataUnavailable, Http
from .models import Pair
from .sources import Sources
from .state import State


class LiveGateError(RuntimeError):
    pass


def check_live_gate(cfg: Config) -> dict:
    """Live mode only once paper has proved something.

    Counting rows is not proof: twenty losing trades used to open live exactly like twenty
    winning ones. The gate now also asks whether the money went up and whether the daily loss
    limit ever had to fire. Thresholds come from rules.py, so no env var can lower the bar.
    """
    paper_log = Logger(cfg.data_dir / "paper" / "events.jsonl", echo=False)
    ev = paper_log.events()
    closed = [e for e in ev if e["event"] == "trade_close"]
    violations = [e for e in ev if e["event"] == "violation"]
    halts = [e for e in ev if e["event"] == "halt"]
    recovered = [e for e in ev if e["event"] == "recovered"]
    pnl = sum(float(e.get("pnl_usd") or 0) for e in closed)
    wins = [e for e in closed if float(e.get("pnl_usd") or 0) > 0]

    problems = []
    if len(closed) < rules.LIVE_MIN_CLEAN_PAPER_TRADES:
        problems.append(f"only {len(closed)} closed paper trades (need {rules.LIVE_MIN_CLEAN_PAPER_TRADES})")
    if violations:
        problems.append(f"{len(violations)} rule violations in paper log")
    if closed and pnl <= rules.LIVE_MIN_PAPER_PNL_USD:
        problems.append(f"paper P/L is ${pnl:+,.2f} over {len(closed)} closed trades "
                        f"({len(wins)} winners): the logic has not made money yet")
    if halts:
        problems.append(f"the daily loss limit fired {len(halts)} time(s) in paper")
    if os.environ.get("DESK_CONFIRM_LIVE") != "yes":
        problems.append("DESK_CONFIRM_LIVE=yes not set")
    if not os.environ.get("SOLANA_PRIVATE_KEY"):
        problems.append("SOLANA_PRIVATE_KEY not set")
    if problems:
        raise LiveGateError("live mode refused: " + "; ".join(problems))
    return {"closed_paper_trades": len(closed), "paper_pnl_usd": round(pnl, 2),
            "paper_winners": len(wins), "recovered_entries": len(recovered)}


class Desk:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.http = Http()
        self.src = Sources(self.http, cfg.solana_rpc_url, cfg.jupiter_base)
        self.log = Logger(cfg.path("events.jsonl"))
        self.state = State.load(cfg.path("state.json"), cfg.paper_start_balance_usd, log=self.log)
        if cfg.mode == "live":
            self.log.log("live_gate_passed", **check_live_gate(cfg))
            self.broker = LiveBroker(cfg, self.http, self.src.rpc, os.environ["SOLANA_PRIVATE_KEY"])
            if not self.state.start_balance or self.state.start_balance == cfg.paper_start_balance_usd:
                self.state.start_balance = self.state.cash = self.broker.balance_usd()
                self.state.save()
        else:
            self.broker = PaperBroker(cfg, self.state)
        self.lock = threading.RLock()
        self.exiting: set[str] = set()     # mints with a sell in flight
        self.stop_evt = threading.Event()
        self.last_prices: dict[str, float] = {}

        self.scout = Scout(self.src, self.log)
        self.filter = Filter(self.src, self.log)
        self.filter.dead = self.state.dead            # shared: dead is dead across restarts
        self.filter.benched = self.state.benched      # shared: a bench survives a restart too
        self.scout.benched = self.state.benched
        self.analyst = Analyst(self.src, self.log, cfg.smart_wallets(), cfg.holder_window_s)
        self.risk = Risk(self.log)
        self.executor = Executor(self.broker, self.risk, self.state, self.log)
        self.scout.seen |= self.state.dead | self.state.traded
        # The Analyst's window lives in RAM only, so nothing is in flight at startup: a near-miss
        # budget with no bench behind it belongs to a coin that crashed out of the pipeline, and
        # would otherwise sit in state.json for ever. Restarts are the one moment this is knowable.
        for m in [m for m in self.state.analyst_retries if m not in self.state.benched]:
            self.state.analyst_retries.pop(m, None)
            self.log.log("retry_budget_dropped", mint=m,
                         detail="near-miss budget with no bench after restart; coin left the pipeline")
        # An order may have gone out without its position reaching the file. Settle that first:
        # until it is settled there could be a bag in the wallet with no stop watching it.
        self.executor.reconcile_pending()

    # ---------------------------------------------------------------- pipeline
    def pipeline_tick(self) -> None:
        for pair in self.scout.scan():                       # SCOUT -> FILTER
            if self.stop_evt.is_set():
                return
            v = self.filter.check(pair)
            if v.alive:                                      # FILTER -> ANALYST
                self.analyst.observe(v)
        self._save()
        self._daily_guard()                                  # kill switch before any entry
        for w in self.analyst.due():                         # ANALYST window done
            fresh = self._fresh_pair(w.pair)
            if fresh is None:
                self.analyst.watching.pop(w.pair.mint, None)
                self.state.dead.add(w.pair.mint)
                self.log.log("error", agent="analyst", mint=w.pair.mint, detail="pair vanished; dropped")
                continue
            v = self.filter.check(fresh)                     # FILTER re-check on fresh data
            if not v.alive:
                self.analyst.watching.pop(w.pair.mint, None)
                continue
            s = self.analyst.score(w, fresh, v)              # ANALYST -> EXECUTOR (70+ only)
            if s.total < rules.ANALYST_MIN_SCORE:
                self._after_low_score(fresh, s)
                continue
            self.state.analyst_retries.pop(fresh.mint, None)
            # Deliberately not under the desk lock: a live buy can take minutes to confirm, and
            # the stop monitor has to keep running meanwhile. State serialises its own writes.
            self.executor.enter(s)
        self._reconcile_retries()
        self._save()

    def _reconcile_retries(self) -> None:
        """A near-miss budget is only worth keeping while the coin can still reach the Analyst.

        It loses that chance in several places -- the Filter buries it on the way back, its pair
        vanishes, or Scout spends the bench and no in-scope pair comes back -- and an entry left
        behind here survives every restart. Hence one sweep instead of a pop at each of those
        sites, which is the bookkeeping that goes stale the next time such a site is added.

        A coin still in flight (handed over this tick, or sitting in the Analyst's window) is in
        none of these sets, so its budget is left alone and the second look still counts.
        """
        for m in list(self.state.analyst_retries):
            if m in self.state.dead or m in self.scout.retired:
                self.state.analyst_retries.pop(m, None)
        self.scout.retired.clear()

    def _after_low_score(self, pair: Pair, s) -> None:
        """Under 70 dies — but a near miss on the hour's numbers gets a second look, not a grave.

        Five of the six components (85 of the 100 points) are conditions that change by the hour,
        which is the same reason rules 3, 4 and 8 bench instead of killing. The Analyst's look is
        far more expensive than those rules, though, so this is deliberately much stricter: within
        ANALYST_RETRY_WITHIN of the threshold, at most ANALYST_MAX_RETRIES times, after an hour.

        A benched coin returns through Scout -> Filter like any other, so all 8 kill rules are
        replayed on fresh data. It can never come back straight to the Analyst.
        """
        spent = self.state.analyst_retries.get(pair.mint, 0)
        near_miss = s.total >= rules.ANALYST_MIN_SCORE - rules.ANALYST_RETRY_WITHIN
        if not near_miss or spent >= rules.ANALYST_MAX_RETRIES:
            self.state.dead.add(pair.mint)
            self.state.benched.pop(pair.mint, None)
            self.state.analyst_retries.pop(pair.mint, None)
            return
        self.state.analyst_retries[pair.mint] = spent + 1
        self.state.benched[pair.mint] = time.time() + rules.ANALYST_RETRY_AFTER_S
        self.analyst.scored.discard(pair.mint)        # or observe() would refuse it on the way back
        self.log.log("analyst_bench", mint=pair.mint, symbol=pair.symbol, score=s.total,
                     needed=rules.ANALYST_MIN_SCORE, attempt=spent + 1, of=rules.ANALYST_MAX_RETRIES,
                     retry_after_s=rules.ANALYST_RETRY_AFTER_S,
                     detail="near miss on the hour's numbers")

    def _fresh_pair(self, old: Pair) -> Pair | None:
        try:
            raw = self.src.dex.pairs_for_mints([old.mint])
        except DataUnavailable:
            return None
        for r in raw:
            if r.get("pairAddress") == old.pair_address:
                return Pair.from_dexscreener(r)
        return None

    # ---------------------------------------------------------------- daily kill switch
    def _daily_guard(self) -> None:
        """Stop opening positions once equity is MAX_DAILY_LOSS_PCT under the day's open.

        Open positions are deliberately left alone: their stops keep running. Dumping
        them all into a falling market would realise the worst price of the day, which
        is the opposite of protecting the balance. The halt lifts at the next local day.
        """
        day = datetime.now().strftime("%Y-%m-%d")
        if self.state.day_key != day and any(m not in self.last_prices for m in self.state.positions):
            return      # no mark yet for an open position: basing the day on entry prices would be a guess
        eq = self.equity()
        if self.state.day_key != day:
            with self.lock:
                self.state.day_key = day
                self.state.day_start_equity = eq
                self.state.save()
            if self.executor.halted:
                self.log.log("resume", day=day, equity=round(eq, 2))
            self.executor.resume()
            self.log.log("day_open", day=day, equity=round(eq, 2))
        base = self.state.day_start_equity
        if base <= 0 or self.executor.halted:
            return
        dd = (eq / base - 1) * 100
        if dd <= -rules.DESK_MAX_DAILY_LOSS_PCT:
            self.executor.halt(f"daily loss limit {dd:+.1f}% (limit -{rules.DESK_MAX_DAILY_LOSS_PCT:.0f}%)")
            self.log.log("halt", reason="daily_loss_limit", day=day, day_open_equity=round(base, 2),
                         equity=round(eq, 2), drawdown_pct=round(dd, 2),
                         open_positions=len(self.state.positions))

    # ---------------------------------------------------------------- risk
    def _quotes(self, mints: list[str]) -> dict[str, dict]:
        """Prices for the stop monitor: DexScreener first, Jupiter for whatever it did not answer.

        A stop that is not checked is not a stop, so one source being down must not silence the
        monitor for every position. Whatever neither source prices is logged as `unprotected`:
        the desk never guesses a price, and never sells blind to escape the uncertainty.
        """
        quotes: dict[str, dict] = {}
        try:
            quotes = {m: q for m, q in self.src.dex.prices(mints).items()
                      if (q or {}).get("price", 0) > 0}
        except DataUnavailable as e:
            self.log.log("error", agent="risk", detail=f"dexscreener down: {e}")
        missing = [m for m in mints if m not in quotes]
        if missing:
            try:
                fallback = self.src.jup.prices(missing)
            except DataUnavailable as e:
                self.log.log("error", agent="risk", detail=f"jupiter fallback down: {e}")
            else:
                quotes.update(fallback)
                if fallback:
                    self.log.log("price_fallback", agent="risk", source="jupiter",
                                 mints=sorted(fallback))
        blind = [m for m in mints if m not in quotes]
        if blind:
            self.log.log("unprotected", agent="risk", mints=sorted(blind),
                         detail="no price from any source: stop not checked this tick")
        return quotes

    def risk_tick(self) -> None:
        with self.lock:
            positions = list(self.state.positions.values())
        if not positions:
            return
        quotes = self._quotes([p.mint for p in positions])
        for pos in positions:
            q = quotes.get(pos.mint)
            if not q:
                continue
            price = q["price"]
            self.last_prices[pos.mint] = price
            with self.lock:
                if pos.mint in self.exiting:
                    continue                      # a sell is already in flight for this one
                reason = self.risk.update(pos, price)
                if reason:
                    self.exiting.add(pos.mint)
                self._save()
            if not reason:
                continue
            # Same reason as the buy: a swap that waits two minutes for confirmation must not
            # stop every other position from being checked, which is exactly when it matters.
            try:
                self.executor.exit(pos, price, reason)
            finally:
                with self.lock:
                    self.exiting.discard(pos.mint)
                    self._save()

    def _risk_loop(self) -> None:
        while not self.stop_evt.is_set():
            try:
                self.risk_tick()
            except Exception as e:  # never let the stop monitor die
                self.log.log("error", agent="risk", detail=repr(e))
            self.stop_evt.wait(self.cfg.risk_interval_s)

    # ---------------------------------------------------------------- recap
    def equity(self) -> float:
        cash = self.broker.balance_usd()      # live: an RPC and a price call, so not under the lock
        with self.lock:
            marked = sum(p.qty * self.last_prices.get(p.mint, p.entry_price)
                         for p in list(self.state.positions.values()))
        return cash + marked

    def write_recap(self, since: float | None = None, stopped: bool = False) -> str:
        since = since if since is not None else self.state.session_start
        opens = [p.to_dict() for p in self.state.positions.values()]
        txt = self.log.recap(since, self.equity(), self.state.start_balance, opens, stopped=stopped)
        out = self.cfg.path(f"recap-{datetime.now():%Y-%m-%d}.md")
        out.write_text(txt + "\n")
        self.log.log("recap", file=str(out))
        return txt

    def _maybe_recap(self) -> None:
        now = datetime.now()
        day = now.strftime("%Y-%m-%d")
        if now.hour >= self.cfg.recap_hour_local and self.state.last_recap_day != day \
                and time.time() - self.state.session_start > 3600:
            self.write_recap()
            self.state.last_recap_day = day
            self._save()

    def _save(self) -> None:
        with self.lock:
            self.state.save()

    # ---------------------------------------------------------------- run
    def run(self, hours: float | None = None) -> None:
        end = time.time() + hours * 3600 if hours else None
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: self.stop_evt.set())
        self.log.log("start", mode=self.cfg.mode, balance=self.broker.balance_usd(),
                     smart_wallets=len(self.analyst.smart_wallets), open_positions=len(self.state.positions))
        rt = threading.Thread(target=self._risk_loop, name="risk", daemon=True)
        rt.start()
        try:
            while not self.stop_evt.is_set() and (end is None or time.time() < end):
                try:
                    self.pipeline_tick()
                    self._maybe_recap()
                except Exception as e:
                    self.log.log("error", agent="desk", detail=repr(e))
                self.stop_evt.wait(self.cfg.scan_interval_s)
        finally:
            self.stop_evt.set()
            rt.join(timeout=30)
            print(self._stop_and_recap())

    def _stop_and_recap(self) -> str:
        """The stop only runs while the desk runs, so say so plainly on the way out."""
        if self.state.positions:
            self.log.log("unprotected", agent="desk", mints=sorted(self.state.positions),
                         detail="desk stopped: no stop is running for these until it restarts")
        self.log.log("stop", open_positions=len(self.state.positions))
        return self.write_recap(stopped=True)
