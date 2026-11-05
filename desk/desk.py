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


def check_live_gate(cfg: Config) -> None:
    """Live mode only after paper logs are clean: enough closed paper trades and zero rule violations."""
    paper_log = Logger(cfg.data_dir / "paper" / "events.jsonl", echo=False)
    ev = paper_log.events()
    closed = [e for e in ev if e["event"] == "trade_close"]
    violations = [e for e in ev if e["event"] == "violation"]
    problems = []
    if len(closed) < cfg.live_min_clean_paper_trades:
        problems.append(f"only {len(closed)} closed paper trades (need {cfg.live_min_clean_paper_trades})")
    if violations:
        problems.append(f"{len(violations)} rule violations in paper log")
    if os.environ.get("DESK_CONFIRM_LIVE") != "yes":
        problems.append("DESK_CONFIRM_LIVE=yes not set")
    if not os.environ.get("SOLANA_PRIVATE_KEY"):
        problems.append("SOLANA_PRIVATE_KEY not set")
    if problems:
        raise LiveGateError("live mode refused: " + "; ".join(problems))


class Desk:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.http = Http()
        self.src = Sources(self.http, cfg.solana_rpc_url)
        self.log = Logger(cfg.path("events.jsonl"))
        self.state = State.load(cfg.path("state.json"), cfg.paper_start_balance_usd, log=self.log)
        if cfg.mode == "live":
            check_live_gate(cfg)
            self.broker = LiveBroker(cfg, self.http, self.src.rpc, os.environ["SOLANA_PRIVATE_KEY"])
            if not self.state.start_balance or self.state.start_balance == cfg.paper_start_balance_usd:
                self.state.start_balance = self.state.cash = self.broker.balance_usd()
                self.state.save()
        else:
            self.broker = PaperBroker(cfg, self.state)
        self.lock = threading.RLock()
        self.stop_evt = threading.Event()
        self.last_prices: dict[str, float] = {}

        self.scout = Scout(self.src, self.log)
        self.filter = Filter(self.src, self.log)
        self.filter.dead = self.state.dead            # shared: dead is dead across restarts
        self.analyst = Analyst(self.src, self.log, cfg.smart_wallets(), cfg.holder_window_s)
        self.risk = Risk(self.log)
        self.executor = Executor(self.broker, self.risk, self.state, self.log)
        self.scout.handed_off |= self.state.dead | self.state.traded

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
                self.state.dead.add(fresh.mint)
                continue
            with self.lock:
                self.executor.enter(s)
        self._save()

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
    def risk_tick(self) -> None:
        with self.lock:
            positions = list(self.state.positions.values())
        if not positions:
            return
        try:
            quotes = self.src.dex.prices([p.mint for p in positions])
        except DataUnavailable as e:
            self.log.log("error", agent="risk", detail=f"price feed down: {e}")
            return
        for pos in positions:
            q = quotes.get(pos.mint)
            if not q or q["price"] <= 0:
                self.log.log("error", agent="risk", mint=pos.mint, detail="no price")
                continue
            price = q["price"]
            self.last_prices[pos.mint] = price
            with self.lock:
                reason = self.risk.update(pos, price)
                if reason:
                    self.executor.exit(pos, price, reason)
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
        with self.lock:
            cash = self.broker.balance_usd()
            marked = sum(p.qty * self.last_prices.get(p.mint, p.entry_price) for p in self.state.positions.values())
        return cash + marked

    def write_recap(self, since: float | None = None) -> str:
        since = since if since is not None else self.state.session_start
        opens = [p.to_dict() for p in self.state.positions.values()]
        txt = self.log.recap(since, self.equity(), self.state.start_balance, opens)
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
            self.log.log("stop", open_positions=len(self.state.positions))
            print(self.write_recap())
