"""End-to-end: scout -> filter -> analyst -> executor -> risk -> exit, with fake market data."""
import dataclasses
import time

import desk.desk as deskmod
from conftest import make_report
from desk.config import Config


def ds_pair(mint, price, created_s_ago=3 * 3600, **kw):
    p = {"pairAddress": "PAIR", "baseToken": {"address": mint, "symbol": "GOOD"}, "dexId": "pumpswap",
         "pairCreatedAt": int((time.time() - created_s_ago) * 1000), "priceUsd": str(price),
         "marketCap": 1_000_000, "liquidity": {"usd": 220_000}, "volume": {"h1": 300_000},
         "priceChange": {"m5": 2, "h1": 30}, "txns": {"h1": {"buys": 900, "sells": 300}}}
    p.update(kw)
    return p


class Market:
    def __init__(self): self.price = 1.0; self.holders = 1000


class FakeSources:
    def __init__(self, http, rpc_url, market=None):
        m = self.market = FAKE_MARKET
        outer = self

        class Dex:
            def discover_mints(self): return {"GOODMINT"}
            def pairs_for_mints(self, mints): return [ds_pair("GOODMINT", m.price)] if "GOODMINT" in mints else []
            def prices(self, mints): return {"GOODMINT": {"price": m.price}}

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

        self.dex, self.gt, self.rug, self.rpc = Dex(), GT(), Rug(), RPC()


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
    assert abs(pos.cost_usd - 80) < 1e-9

    FAKE_MARKET.price = 1.30; d.risk_tick()   # trail on
    assert pos.trailing
    FAKE_MARKET.price = 1.20; d.risk_tick()   # 1.30*0.94=1.222 -> trailing stop exit
    assert not d.state.positions
    ev = d.log.events()
    close = [e for e in ev if e["event"] == "trade_close"][0]
    assert close["reason"] == "trailing_stop" and close["pnl_usd"] > 0

    d.pipeline_tick()                      # same coin shows up again: no second entry
    assert not d.state.positions
    recap = d.write_recap(since=0)
    assert "W/L: **1/0**" in recap and "Trades opened: **1**" in recap
    assert not [e for e in ev if e["event"] == "violation"]


def test_live_gate_refuses_without_clean_paper(tmp_path, monkeypatch):
    monkeypatch.delenv("DESK_CONFIRM_LIVE", raising=False)
    cfg = dataclasses.replace(Config(), data_dir=tmp_path, mode="live")
    try:
        deskmod.check_live_gate(cfg)
        assert False, "gate should refuse"
    except deskmod.LiveGateError as e:
        assert "closed paper trades" in str(e)
