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
         "priceChange": {"m5": 2, "h1": 30}, "txns": {"h1": {"buys": 900, "sells": 300}}}
    p.update(kw)
    return p


class Market:
    def __init__(self):
        self.reset()

    def reset(self):
        self.price, self.holders = 1.0, 1000
        self.liquidity = 220_000
        self.dex_down = self.jup_down = False


class FakeSources:
    def __init__(self, http, rpc_url, jupiter_base=None):
        m = self.market = FAKE_MARKET

        class Dex:
            def discover_mints(self): return {"GOODMINT"}
            def pairs_for_mints(self, mints): return [ds_pair("GOODMINT", m.price)] if "GOODMINT" in mints else []
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
