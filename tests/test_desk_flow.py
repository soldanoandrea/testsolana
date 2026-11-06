"""End-to-end: scout -> filter -> analyst -> executor -> risk -> exit, with fake market data."""
import dataclasses
import json
import threading
import time

import pytest

import desk.desk as deskmod
from conftest import make_report
from desk import rules
from desk.config import Config
from desk.http import DataUnavailable


def ds_pair(mint, price, created_s_ago=3 * 3600, **kw):
    p = {"pairAddress": "PAIR", "baseToken": {"address": mint, "symbol": "GOOD"}, "dexId": "pumpswap",
         "pairCreatedAt": int((time.time() - created_s_ago) * 1000), "priceUsd": str(price),
         "marketCap": 1_000_000, "liquidity": {"usd": FAKE_MARKET.liquidity}, "volume": {"h1": 300_000},
         "priceChange": {"m5": 2, "h1": FAKE_MARKET.chg_h1}, "txns": {"h1": {"buys": 900, "sells": 300}}}
    p.update(kw)
    return p


class Market:
    def __init__(self):
        self.reset()

    def reset(self):
        self.price, self.holders = 1.0, 1000
        self.liquidity = 220_000
        self.chg_h1 = 30
        self.dex_down = self.jup_down = False
        self.vanish = False          # the pair stops being returned at all (delisted, or out of scope)


class FakeSources:
    def __init__(self, http, rpc_url, jupiter_base=None):
        m = self.market = FAKE_MARKET

        class Dex:
            def discover_mints(self): return {"GOODMINT"}
            def pairs_for_mints(self, mints):
                if m.vanish or "GOODMINT" not in mints:
                    return []
                return [ds_pair("GOODMINT", m.price)]
            def prices(self, mints):
                if m.dex_down:
                    raise DataUnavailable("dexscreener down")
                return {"GOODMINT": {"price": m.price}}

        class Jup:
            def prices(self, mints):
                if m.jup_down:
                    raise DataUnavailable("jupiter down")
                return {x: {"price": m.price, "source": "jupiter"} for x in mints}

        class GT:
            def discover_mints(self): return set()
            def pool_h1_activity(self, addr): return {"buys": 900, "sells": 300, "buyers": 500, "sellers": 150}

        class Rug:
            def report(self, mint):
                return make_report(totalHolders=m.holders)
            def was_rugged(self, mint): return False

        class RPC:
            def mint_info(self, mint): return {"mintAuthority": None, "freezeAuthority": None, "decimals": 6}
            def token_balance(self, o, mint): return 1.0

        self.dex, self.gt, self.rug, self.rpc, self.jup = Dex(), GT(), Rug(), RPC(), Jup()


FAKE_MARKET = Market()


def test_full_cycle(tmp_path, monkeypatch):
    monkeypatch.setattr(deskmod, "Sources", FakeSources)
    wallets = tmp_path / "sw.txt"
    wallets.write_text("W1\nW2\n")
    cfg = dataclasses.replace(Config(), data_dir=tmp_path, holder_window_s=300, smart_wallets_file=wallets)
    d = deskmod.Desk(cfg)
    d.log.echo = False

    d.pipeline_tick()                      # scout+filter+observe, holders snapshot #1
    FAKE_MARKET.holders = 1100             # holders grow
    d.analyst.watching["GOODMINT"].t0 -= 600
    d.pipeline_tick()                      # re-filter + score + enter
    assert "GOODMINT" in d.state.positions, [e for e in d.log.events() if e["event"] in ("score", "kill")]
    pos = d.state.positions["GOODMINT"]
    assert abs(pos.cost_usd - 50) < 1e-9

    FAKE_MARKET.price = 1.70; d.risk_tick()   # +67% over the ~1.019 fill -> trail on
    assert pos.trailing
    FAKE_MARKET.price = 1.35; d.risk_tick()   # 1.70*0.80=1.36 -> trailing stop exit
    assert not d.state.positions
    ev = d.log.events()
    close = [e for e in ev if e["event"] == "trade_close"][0]
    assert close["reason"] == "trailing_stop" and close["pnl_usd"] > 0

    d.pipeline_tick()                      # same coin shows up again: no second entry
    assert not d.state.positions
    recap = d.write_recap(since=0)
    assert "W/L: **1/0**" in recap and "Trades opened: **1**" in recap
    assert not [e for e in ev if e["event"] == "violation"]


def test_daily_loss_limit_halts_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(deskmod, "Sources", FakeSources)
    d = deskmod.Desk(dataclasses.replace(Config(), data_dir=tmp_path))
    d.log.echo = False

    d._daily_guard()                                   # records the day's opening equity
    assert d.state.day_start_equity == 1000.0 and not d.executor.halted

    d.state.cash = 1000.0 * (1 - rules.DESK_MAX_DAILY_LOSS_PCT / 100) - 1.0   # a bad day
    d._daily_guard()
    assert d.executor.halted
    halt = [e for e in d.log.events() if e["event"] == "halt"][0]
    assert halt["reason"] == "daily_loss_limit" and halt["drawdown_pct"] < -rules.DESK_MAX_DAILY_LOSS_PCT
    assert "Entries halted" in d.write_recap(since=0)

    d.state.day_key = "1970-01-01"                     # next local day lifts the halt and re-bases
    d._daily_guard()
    assert not d.executor.halted
    assert d.state.day_start_equity == d.state.cash


def test_loss_limit_leaves_open_positions_alone(tmp_path, monkeypatch):
    """The kill switch stops buying; it must not market-sell what is already open."""
    monkeypatch.setattr(deskmod, "Sources", FakeSources)
    d = deskmod.Desk(dataclasses.replace(Config(), data_dir=tmp_path))
    d.log.echo = False
    FAKE_MARKET.price = 1.0
    d.pipeline_tick()
    d.analyst.watching["GOODMINT"].t0 -= 600
    d.pipeline_tick()
    assert "GOODMINT" in d.state.positions
    d.state.day_start_equity = d.equity() * 10        # pretend a huge drawdown
    d._daily_guard()
    assert d.executor.halted
    assert "GOODMINT" in d.state.positions            # still held, stop still running
    assert not [e for e in d.log.events() if e["event"] == "trade_close"]


def test_day_baseline_waits_for_a_real_mark(tmp_path, monkeypatch):
    """The day's opening equity must not be based on entry prices for an unmarked position."""
    monkeypatch.setattr(deskmod, "Sources", FakeSources)
    d = deskmod.Desk(dataclasses.replace(Config(), data_dir=tmp_path))
    d.log.echo = False
    FAKE_MARKET.price = 1.0
    d.pipeline_tick()
    d.analyst.watching["GOODMINT"].t0 -= 600
    d.pipeline_tick()
    assert "GOODMINT" in d.state.positions

    d.state.day_key = "1970-01-01"          # a new day arrives...
    d.last_prices.clear()                   # ...before the risk loop has marked anything
    d._daily_guard()
    assert d.state.day_key == "1970-01-01"  # deferred, not based on a guess

    d.risk_tick()                           # first real mark
    d._daily_guard()
    assert d.state.day_key != "1970-01-01" and d.state.day_start_equity > 0


def open_one(tmp_path, monkeypatch):
    """A desk with exactly one open position, at a ~1.019 fill."""
    monkeypatch.setattr(deskmod, "Sources", FakeSources)
    FAKE_MARKET.reset()
    d = deskmod.Desk(dataclasses.replace(Config(), data_dir=tmp_path))
    d.log.echo = False
    d.pipeline_tick()
    d.analyst.watching["GOODMINT"].t0 -= 600
    d.pipeline_tick()
    assert "GOODMINT" in d.state.positions
    return d, d.state.positions["GOODMINT"]


def test_stop_still_fires_when_dexscreener_is_down(tmp_path, monkeypatch):
    """One source down must not silence the stop monitor."""
    d, pos = open_one(tmp_path, monkeypatch)
    try:
        FAKE_MARKET.dex_down = True
        FAKE_MARKET.price = pos.stop_price * 0.99      # under the stop
        d.risk_tick()
        assert not d.state.positions, "the stop must fire off the fallback price"
        ev = d.log.events()
        assert [e for e in ev if e["event"] == "price_fallback"]
        assert [e for e in ev if e["event"] == "trade_close"][0]["reason"] == "stop_loss"
    finally:
        FAKE_MARKET.reset()


def test_both_price_sources_down_is_logged_not_guessed(tmp_path, monkeypatch):
    """With no price anywhere the desk holds and says so; it never sells blind."""
    d, _ = open_one(tmp_path, monkeypatch)
    try:
        FAKE_MARKET.dex_down = FAKE_MARKET.jup_down = True
        d.risk_tick()
        assert "GOODMINT" in d.state.positions
        u = [e for e in d.log.events() if e["event"] == "unprotected" and e.get("agent") == "risk"]
        assert u and u[0]["mints"] == ["GOODMINT"]
        assert not [e for e in d.log.events() if e["event"] == "trade_close"]
        assert "Stop not checked on 1 tick(s)" in d.write_recap(since=0)
    finally:
        FAKE_MARKET.reset()


def test_shutdown_says_the_open_position_is_no_longer_watched(tmp_path, monkeypatch):
    d, _ = open_one(tmp_path, monkeypatch)
    txt = d._stop_and_recap()
    assert "no longer watched" in txt and "GOOD" in txt
    u = [e for e in d.log.events() if e["event"] == "unprotected" and e.get("agent") == "desk"]
    assert u and u[0]["mints"] == ["GOODMINT"]


def scouted(d):
    return [e for e in d.log.events() if e["event"] == "scouted"]


def test_a_market_rule_benches_the_coin_and_it_comes_back(tmp_path, monkeypatch):
    """R3/R4/R8 describe the hour. The coin must get another look, not a permanent grave."""
    monkeypatch.setattr(deskmod, "Sources", FakeSources)
    FAKE_MARKET.reset()
    FAKE_MARKET.liquidity = 10_000                  # under R3
    d = deskmod.Desk(dataclasses.replace(Config(), data_dir=tmp_path))
    d.log.echo = False
    try:
        d.pipeline_tick()
        assert "GOODMINT" not in d.state.dead       # benched, not killed
        assert "GOODMINT" in d.state.benched
        assert len(scouted(d)) == 1

        d.pipeline_tick()                           # bench still running: not handed again
        assert len(scouted(d)) == 1

        FAKE_MARKET.liquidity = 220_000             # liquidity arrives
        d.state.benched["GOODMINT"] = time.time() - 1
        d.pipeline_tick()
        assert len(scouted(d)) == 2                 # looked at again
        assert "GOODMINT" in d.analyst.watching     # and through the Filter this time
        assert "GOODMINT" not in d.state.benched
    finally:
        FAKE_MARKET.reset()


def test_a_safety_kill_is_never_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(deskmod, "Sources", FakeSources)
    FAKE_MARKET.reset()
    d = deskmod.Desk(dataclasses.replace(Config(), data_dir=tmp_path))
    d.log.echo = False
    d.filter.src = type("S", (), {
        "rug": type("R", (), {"report": staticmethod(lambda m: make_report(
            topHolders=[{"owner": f"W{i}", "address": f"A{i}", "pct": 3.0} for i in range(15)])),
            "was_rugged": staticmethod(lambda m: False)})(),
        "rpc": d.src.rpc})()
    d.pipeline_tick()
    assert "GOODMINT" in d.state.dead and "GOODMINT" not in d.state.benched
    d.pipeline_tick()
    assert len(scouted(d)) == 1                     # never handed over again


def lock_free(lock, timeout=0.3) -> bool:
    """Can a DIFFERENT thread take this lock right now? (an RLock lies to its own thread)"""
    box = {}

    def probe():
        if lock.acquire(timeout=timeout):
            lock.release()
            box["ok"] = True
        else:
            box["ok"] = False

    t = threading.Thread(target=probe)
    t.start()
    t.join()
    return box["ok"]


def test_a_sell_does_not_hold_the_desk_lock(tmp_path, monkeypatch):
    """A live swap can take minutes; holding the lock would freeze every other stop."""
    d, pos = open_one(tmp_path, monkeypatch)
    seen, real = {}, d.broker.sell

    def probing_sell(mint, qty, ref_price, attempt=0):
        seen["free"] = lock_free(d.lock)
        return real(mint, qty, ref_price, attempt=attempt)

    d.broker.sell = probing_sell
    FAKE_MARKET.price = pos.stop_price * 0.99
    d.risk_tick()
    assert not d.state.positions                    # it really did sell
    assert seen["free"] is True, "the stop monitor was blocked during the swap"
    FAKE_MARKET.reset()


def test_a_buy_does_not_hold_the_desk_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(deskmod, "Sources", FakeSources)
    FAKE_MARKET.reset()
    d = deskmod.Desk(dataclasses.replace(Config(), data_dir=tmp_path))
    d.log.echo = False
    seen, real = {}, d.broker.buy

    def probing_buy(mint, usd, ref):
        seen["free"] = lock_free(d.lock)
        return real(mint, usd, ref)

    d.broker.buy = probing_buy
    d.pipeline_tick()
    d.analyst.watching["GOODMINT"].t0 -= 600
    d.pipeline_tick()
    assert "GOODMINT" in d.state.positions
    assert seen["free"] is True, "the stop monitor was blocked during the buy"


def test_a_sell_in_flight_is_not_started_twice(tmp_path, monkeypatch):
    d, pos = open_one(tmp_path, monkeypatch)
    d.exiting.add("GOODMINT")                       # as if a swap were already confirming
    FAKE_MARKET.price = pos.stop_price * 0.99
    d.risk_tick()
    assert "GOODMINT" in d.state.positions          # left to the sell already running
    assert not [e for e in d.log.events() if e["event"] == "trade_close"]
    FAKE_MARKET.reset()


def near_miss_desk(tmp_path, monkeypatch, chg_h1=5):
    """A market that scores inside the near-miss band: liq/mcap at the floor, weak momentum."""
    monkeypatch.setattr(deskmod, "Sources", FakeSources)
    FAKE_MARKET.reset()
    FAKE_MARKET.liquidity = 50_000       # liq/mcap 0.05 -> 0 of 15, but still past R3
    FAKE_MARKET.chg_h1 = chg_h1
    d = deskmod.Desk(dataclasses.replace(Config(), data_dir=tmp_path))
    d.log.echo = False
    return d


def one_look(d, first=False):
    """A full Analyst look: Scout -> Filter -> observe -> window -> score.

    Holders grow during every window, so holder_growth stays maxed and the shortfall comes only
    from liq/mcap and momentum — the same shape as the real COGE score (57.9).
    """
    if not first:
        d.state.benched["GOODMINT"] = time.time() - 1      # its hour is up
    d.pipeline_tick()
    FAKE_MARKET.holders += 200
    d.analyst.watching["GOODMINT"].t0 -= 600
    d.pipeline_tick()
    return [e for e in d.log.events() if e["event"] == "score"][-1]["total"]


def test_a_near_miss_score_is_benched_not_buried(tmp_path, monkeypatch):
    """85 of the 100 points are hour conditions, so a near miss must not be a permanent verdict."""
    d = near_miss_desk(tmp_path, monkeypatch)
    try:
        total = one_look(d, first=True)
        assert rules.ANALYST_MIN_SCORE - rules.ANALYST_RETRY_WITHIN <= total < rules.ANALYST_MIN_SCORE, total
        assert "GOODMINT" not in d.state.dead
        assert "GOODMINT" in d.state.benched
        assert d.state.analyst_retries["GOODMINT"] == 1
        assert "GOODMINT" not in d.analyst.scored          # observe() would refuse it otherwise
        b = [e for e in d.log.events() if e["event"] == "analyst_bench"][0]
        assert b["attempt"] == 1 and b["score"] == total

        # the hour turns: deeper liquidity, real momentum, holders growing
        FAKE_MARKET.liquidity, FAKE_MARKET.chg_h1 = 220_000, 30
        assert one_look(d) >= rules.ANALYST_MIN_SCORE       # Scout -> Filter -> observed -> scored
        assert "GOODMINT" in d.state.positions             # second look, and this time it trades
        assert "GOODMINT" not in d.state.analyst_retries   # budget cleared on a pass
    finally:
        FAKE_MARKET.reset()


def test_a_bad_score_still_dies_for_good(tmp_path, monkeypatch):
    """"Under 70 dies" still means dies: only a near miss buys another look."""
    d = near_miss_desk(tmp_path, monkeypatch, chg_h1=0)
    try:
        total = one_look(d, first=True)
        assert total < rules.ANALYST_MIN_SCORE - rules.ANALYST_RETRY_WITHIN, total
        assert "GOODMINT" in d.state.dead and "GOODMINT" not in d.state.benched
        assert not [e for e in d.log.events() if e["event"] == "analyst_bench"]
        assert not d.state.analyst_retries
    finally:
        FAKE_MARKET.reset()


def test_the_retry_budget_runs_out(tmp_path, monkeypatch):
    """The Analyst's look is expensive, so a coin cannot keep buying new ones forever."""
    d = near_miss_desk(tmp_path, monkeypatch)
    try:
        one_look(d, first=True)                 # look 1 -> benched, attempt 1
        for _ in range(rules.ANALYST_MAX_RETRIES):
            one_look(d)
        assert "GOODMINT" in d.state.dead                   # budget spent, now permanent
        assert "GOODMINT" not in d.state.benched
        assert not d.state.analyst_retries
        benches = [e for e in d.log.events() if e["event"] == "analyst_bench"]
        assert [b["attempt"] for b in benches] == list(range(1, rules.ANALYST_MAX_RETRIES + 1))
    finally:
        FAKE_MARKET.reset()


def test_a_benched_near_miss_must_pass_the_filter_again(tmp_path, monkeypatch):
    """It returns through Scout -> Filter: it can never come back straight to the Analyst."""
    d = near_miss_desk(tmp_path, monkeypatch)
    try:
        one_look(d, first=True)
        assert "GOODMINT" in d.state.benched
        FAKE_MARKET.liquidity = 10_000                      # now it fails R3
        d.state.benched["GOODMINT"] = time.time() - 1
        d.pipeline_tick()
        assert "GOODMINT" not in d.analyst.watching         # killed by the Filter, not re-observed
        assert [e for e in d.log.events() if e["event"] == "kill" and e["rule"] == 3]
    finally:
        FAKE_MARKET.reset()


def expired(d):
    return [e for e in d.log.events() if e["event"] == "bench_expired"]


def test_a_bench_that_expires_with_nothing_coming_back_names_the_coin(tmp_path, monkeypatch):
    """A coin can end by simply not coming back. That ending has to be readable in the log.

    Before this, the only trace was the gap between retried_from_bench and handed_to_filter in the
    scan event, which names no mint: from the log the coin had just vanished.
    """
    monkeypatch.setattr(deskmod, "Sources", FakeSources)
    FAKE_MARKET.reset()
    FAKE_MARKET.liquidity = 10_000                          # under R3 -> benched by the Filter
    d = deskmod.Desk(dataclasses.replace(Config(), data_dir=tmp_path))
    d.log.echo = False
    try:
        d.pipeline_tick()
        assert "GOODMINT" in d.state.benched and not expired(d)

        FAKE_MARKET.vanish = True                           # delisted while it sat on the bench
        d.state.benched["GOODMINT"] = time.time() - 1
        d.pipeline_tick()
        assert [e["mint"] for e in expired(d)] == ["GOODMINT"]
        assert "GOODMINT" not in d.state.benched            # the bench is spent either way

        FAKE_MARKET.vanish = False                          # and it is not handed over again
        d.pipeline_tick()
        assert len(scouted(d)) == 1 and len(expired(d)) == 1
    finally:
        FAKE_MARKET.reset()


def test_a_near_miss_that_never_comes_back_hands_its_budget_back(tmp_path, monkeypatch):
    """The real case: attempt 1 spent, bench expired with no pair, entry left in state for ever."""
    d = near_miss_desk(tmp_path, monkeypatch)
    try:
        one_look(d, first=True)
        assert d.state.analyst_retries["GOODMINT"] == 1

        FAKE_MARKET.vanish = True
        d.state.benched["GOODMINT"] = time.time() - 1
        d.pipeline_tick()

        assert expired(d) and expired(d)[0]["mint"] == "GOODMINT"
        assert not d.state.analyst_retries                  # no entry outliving the coin
        assert not json.loads(                              # and gone from the file, not just RAM
            d.cfg.path("state.json").read_text())["analyst_retries"]
        assert "near miss whose second look never happened" in d.write_recap(since=0)
    finally:
        FAKE_MARKET.reset()


def test_a_near_miss_still_in_flight_keeps_its_budget(tmp_path, monkeypatch):
    """The guard on the fix above: the sweep must not refund a coin that DID come back.

    Scout drops the bench the moment it hands the pair over, so between that hand-off and the
    Analyst's verdict the budget is the only record that an attempt was already spent. Clear it
    there and a near miss would buy unlimited looks, one per tick.
    """
    d = near_miss_desk(tmp_path, monkeypatch)
    try:
        one_look(d, first=True)
        assert d.state.analyst_retries["GOODMINT"] == 1

        d.state.benched["GOODMINT"] = time.time() - 1
        d.pipeline_tick()                                   # handed over, observed, no verdict yet
        assert "GOODMINT" in d.analyst.watching
        assert "GOODMINT" not in d.state.benched            # bench already spent
        assert d.state.analyst_retries["GOODMINT"] == 1     # ...and the attempt still counts
        assert not expired(d)

        FAKE_MARKET.holders += 200                          # let the window finish: still a miss
        d.analyst.watching["GOODMINT"].t0 -= 600
        d.pipeline_tick()
        assert d.state.analyst_retries["GOODMINT"] == 2
    finally:
        FAKE_MARKET.reset()


def test_a_kill_on_the_way_back_hands_the_budget_back_too(tmp_path, monkeypatch):
    """The Filter burying a returning near miss is the other way the budget was orphaned."""
    d = near_miss_desk(tmp_path, monkeypatch)
    try:
        one_look(d, first=True)
        assert d.state.analyst_retries["GOODMINT"] == 1

        d.filter.src = type("S", (), {                      # 15 whales -> R5, permanent
            "rug": type("R", (), {"report": staticmethod(lambda m: make_report(
                topHolders=[{"owner": f"W{i}", "address": f"A{i}", "pct": 3.0} for i in range(15)])),
                "was_rugged": staticmethod(lambda m: False)})(),
            "rpc": d.src.rpc})()
        d.state.benched["GOODMINT"] = time.time() - 1
        d.pipeline_tick()

        assert "GOODMINT" in d.state.dead
        assert not d.state.analyst_retries
    finally:
        FAKE_MARKET.reset()


def test_a_restart_puts_an_in_flight_near_miss_back_on_the_bench(tmp_path, monkeypatch):
    """A budget with no bench is a coin that was in flight when the desk went down.

    Dropping it would refund the attempts already spent, and `seen` is not persisted either, so
    the coin comes back and would buy a whole fresh budget. Re-benching keeps the attempts and
    applies the cooldown it would have served anyway.
    """
    monkeypatch.setattr(deskmod, "Sources", FakeSources)
    FAKE_MARKET.reset()
    cfg = dataclasses.replace(Config(), data_dir=tmp_path)
    d = deskmod.Desk(cfg)
    d.log.echo = False
    try:
        d.state.analyst_retries.update({"INFLIGHT": 1, "ONBENCH": 2})
        d.state.benched["ONBENCH"] = time.time() + 3600
        d._save()

        back = deskmod.Desk(cfg)                            # restart
        back.log.echo = False
        assert back.state.analyst_retries == {"INFLIGHT": 1, "ONBENCH": 2}   # nothing refunded
        assert "INFLIGHT" in back.state.benched                              # and it can return
        assert back.state.benched["INFLIGHT"] > time.time() + 60             # after the cooldown
        r = [e for e in back.log.events() if e["event"] == "retry_rebenched"]
        assert [e["mint"] for e in r] == ["INFLIGHT"] and r[0]["spent"] == 1
        assert back.state.benched["ONBENCH"] == d.state.benched["ONBENCH"]   # left as it was
    finally:
        FAKE_MARKET.reset()


def test_a_rebenched_budget_is_cleared_if_the_coin_never_comes_back(tmp_path, monkeypatch):
    """The re-bench must not become the new way a budget lives for ever.

    It converges through the path the Scout already has: bench expires, nothing in scope comes
    back, the coin is retired and the sweep clears the budget.
    """
    monkeypatch.setattr(deskmod, "Sources", FakeSources)
    FAKE_MARKET.reset()
    cfg = dataclasses.replace(Config(), data_dir=tmp_path)
    d = deskmod.Desk(cfg)
    d.log.echo = False
    try:
        d.state.analyst_retries["GONE"] = 1
        d._save()
        back = deskmod.Desk(cfg)
        back.log.echo = False
        assert "GONE" in back.state.benched

        FAKE_MARKET.vanish = True                           # nothing comes back for it
        back.state.benched["GONE"] = time.time() - 1
        back.pipeline_tick()
        assert [e["mint"] for e in expired(back)] == ["GONE"]
        assert not back.state.analyst_retries
    finally:
        FAKE_MARKET.reset()


def test_the_analyst_cooldown_is_longer_than_the_filter_bench():
    """A 300 s observation window plus RugCheck and RPC must not be retried as cheaply as R3."""
    assert rules.ANALYST_RETRY_AFTER_S > rules.FILTER_BENCH_S


def paper_log(tmp_path, closes=20, pnl_each=1.0, extra=()):
    """A synthetic paper log for the live gate to read."""
    d = tmp_path / "paper"
    d.mkdir(parents=True, exist_ok=True)
    rows = [{"ts": 1.0 + i, "event": "trade_close", "mint": f"M{i}", "symbol": "T",
             "pnl_usd": pnl_each, "pnl_pct": pnl_each} for i in range(closes)]
    rows += list(extra)
    (d / "events.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return dataclasses.replace(Config(), data_dir=tmp_path, mode="live")


def armed(monkeypatch):
    monkeypatch.setenv("DESK_CONFIRM_LIVE", "yes")
    monkeypatch.setenv("SOLANA_PRIVATE_KEY", "not-a-real-key")


def test_gate_refuses_when_paper_lost_money(monkeypatch, tmp_path):
    """Twenty losing trades used to open live exactly like twenty winning ones."""
    armed(monkeypatch)
    cfg = paper_log(tmp_path, closes=20, pnl_each=-1.0)
    with pytest.raises(deskmod.LiveGateError) as e:
        deskmod.check_live_gate(cfg)
    assert "has not made money" in str(e.value)


def test_gate_refuses_when_paper_broke_even(monkeypatch, tmp_path):
    armed(monkeypatch)
    cfg = paper_log(tmp_path, closes=20, pnl_each=0.0)
    with pytest.raises(deskmod.LiveGateError):
        deskmod.check_live_gate(cfg)


def test_gate_refuses_after_a_daily_loss_halt(monkeypatch, tmp_path):
    armed(monkeypatch)
    cfg = paper_log(tmp_path, closes=20, pnl_each=1.0,
                    extra=[{"ts": 99.0, "event": "halt", "reason": "daily_loss_limit"}])
    with pytest.raises(deskmod.LiveGateError) as e:
        deskmod.check_live_gate(cfg)
    assert "daily loss limit fired" in str(e.value)


def test_gate_passes_on_a_profitable_clean_paper_log(monkeypatch, tmp_path):
    armed(monkeypatch)
    cfg = paper_log(tmp_path, closes=20, pnl_each=1.5)
    summary = deskmod.check_live_gate(cfg)
    assert summary["closed_paper_trades"] == 20
    assert summary["paper_pnl_usd"] == 30.0 and summary["paper_winners"] == 20


def test_gate_thresholds_are_not_config(monkeypatch, tmp_path):
    """DESK_LIVE_MIN_PAPER_TRADES=0 used to open live with no paper record at all."""
    armed(monkeypatch)
    monkeypatch.setenv("DESK_LIVE_MIN_PAPER_TRADES", "0")
    cfg = paper_log(tmp_path, closes=0)
    assert not hasattr(cfg, "live_min_clean_paper_trades")
    with pytest.raises(deskmod.LiveGateError) as e:
        deskmod.check_live_gate(cfg)
    assert "closed paper trades" in str(e.value)
    assert rules.LIVE_MIN_CLEAN_PAPER_TRADES == 20


def test_live_gate_refuses_without_clean_paper(tmp_path, monkeypatch):
    monkeypatch.delenv("DESK_CONFIRM_LIVE", raising=False)
    cfg = dataclasses.replace(Config(), data_dir=tmp_path, mode="live")
    try:
        deskmod.check_live_gate(cfg)
        assert False, "gate should refuse"
    except deskmod.LiveGateError as e:
        assert "closed paper trades" in str(e)
