import pytest
from conftest import make_pair, make_report
from desk.agents.filter import Filter, build_safety, market_rules, safety_rules
from desk.http import DataUnavailable


class FakeRug:
    def __init__(self, report, rugged=()):
        self.r, self.rugged = report, set(rugged)
    def report(self, mint): 
        if self.r is None: raise DataUnavailable("down")
        return self.r
    def was_rugged(self, mint): return mint in self.rugged


class FakeRPC:
    def __init__(self, info=None): self.info = info
    def mint_info(self, mint):
        if self.info is None: raise DataUnavailable("rpc down")
        return self.info


class Src:
    def __init__(self, report, rugged=(), rpc=None):
        self.rug, self.rpc = FakeRug(report, rugged), FakeRPC(rpc)


def run(log, pair=None, report=None, **kw):
    f = Filter(Src(report if report is not None else make_report(), **kw), log)
    return f.check(pair or make_pair())


def test_clean_coin_survives(log):
    v = run(log, rpc={"mintAuthority": None, "freezeAuthority": None})
    assert v.alive, log.records


@pytest.mark.parametrize("token,rule", [
    ({"mintAuthority": "X", "freezeAuthority": None, "supply": 1}, 1),
    ({"mintAuthority": None, "freezeAuthority": "X", "supply": 1}, 1),
])
def test_rule1_authorities(log, token, rule):
    assert run(log, report=make_report(token=token)).rule == rule


def test_rule1_rpc_overrides_rugcheck(log):
    v = run(log, rpc={"mintAuthority": "EVIL", "freezeAuthority": None})
    assert v.rule == 1


def test_rule2_lp(log):
    assert run(log, report=make_report(markets=[{"pubkey": "PAIR", "lp": {"lpLockedPct": 40}}])).rule == 2
    assert run(log, report=make_report(markets=[])).rule == 2   # unverifiable = dead


def test_rule3_liquidity(log):
    assert run(log, pair=make_pair(liquidity_usd=39_999)).rule == 3


def test_rule4_volume(log):
    assert run(log, pair=make_pair(volume_h1=49_999)).rule == 4


def test_rule5_top10_excludes_pool(log):
    holders = [{"owner": "PAIR", "address": "V", "pct": 30.0}] + \
              [{"owner": f"W{i}", "address": f"A{i}", "pct": 2.6} for i in range(10)]
    assert run(log, report=make_report(topHolders=holders)).rule == 5
    holders = [{"owner": "PAIR", "address": "V", "pct": 30.0}] + \
              [{"owner": f"W{i}", "address": f"A{i}", "pct": 2.4} for i in range(10)]
    assert run(log, report=make_report(topHolders=holders)).alive


def test_rule6_dev_holdings_and_rugs(log):
    holders = [{"owner": "DEV", "address": "D", "pct": 3.5}]
    assert run(log, report=make_report(topHolders=holders)).rule == 6
    rep = make_report(creatorTokens=[{"mint": "OLD", "createdAt": "2026-01-01"}])
    assert run(log, report=rep, rugged={"OLD"}).rule == 6
    assert run(log, report=make_report(creator=None)).rule == 6   # unknown dev = dead


def test_rule7_bundle(log):
    holders = [{"owner": f"B{i}", "address": f"A{i}", "pct": 2.1, "insider": True} for i in range(5)]
    assert run(log, report=make_report(topHolders=holders)).rule == 7


def test_rule8_pumped(log):
    assert run(log, pair=make_pair(price_change_h1=80)).rule == 8
    assert run(log, pair=make_pair(price_change_h1=79.9)).alive


def test_safety_source_down_kills(log):
    f = Filter(Src(None), log)
    assert not f.check(make_pair()).alive


def test_dead_stays_dead(log):
    f = Filter(Src(make_report()), log)
    assert not f.check(make_pair(liquidity_usd=1)).alive
    assert not f.check(make_pair()).alive   # same mint, now clean data: still dead


def test_lowest_rule_reported_and_all_listed(log):
    v = run(log, pair=make_pair(liquidity_usd=1, volume_h1=1))
    assert v.rule == 3 and "R4" in v.detail


def test_rule7_insider_networks(log):
    supply = 1_000_000_000_000
    nets = [{"currentHolding": supply * 0.06}, {"currentHolding": supply * 0.05}]
    assert run(log, report=make_report(insiderNetworks=nets, graphInsidersDetected=30)).rule == 7
    nets = [{"currentHolding": supply * 0.04}]
    assert run(log, report=make_report(insiderNetworks=nets, graphInsidersDetected=5)).alive


def test_rule7_insiders_without_sizes_is_dead(log):
    assert run(log, report=make_report(insiderNetworks=None, graphInsidersDetected=30)).rule == 7
