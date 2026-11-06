import time

import pytest
from conftest import make_pair, make_report
from desk import rules
from desk.agents.filter import Filter, build_safety, market_rules, safety_rules
from desk.models import Safety
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


def top10_over_25():
    """A report that trips R5, a safety rule."""
    return make_report(topHolders=[{"owner": f"W{i}", "address": f"A{i}", "pct": 3.0} for i in range(15)])


def test_a_safety_kill_stays_dead(log):
    f = Filter(Src(top10_over_25()), log)
    v = f.check(make_pair())
    assert v.rule == 5 and "MINT" in f.dead and "MINT" not in f.benched
    f.src = Src(make_report())                      # the token is fine now: does not matter
    assert not f.check(make_pair()).alive
    assert f.check(make_pair()).rule_name == "ALREADY_DEAD"


def test_a_market_kill_benches_instead(log):
    """R3/R4/R8 describe the hour, not the token: the coin comes back after the bench."""
    f = Filter(Src(make_report()), log)
    v = f.check(make_pair(liquidity_usd=1))
    assert v.rule == 3
    assert "MINT" not in f.dead                     # not killed for good
    assert f.benched["MINT"] > time.time() + rules.FILTER_BENCH_S - 5
    assert [r for r in log.records if r["event"] == "kill"][-1]["permanent"] is False
    # deeper liquidity later, and the same coin passes
    assert f.check(make_pair()).alive


def test_a_market_hit_benches_before_safety_is_even_checked(log):
    """Market rules run first to save API calls, so a bench can precede knowing the token is bad.

    That is safe: a benched coin is not a traded coin, and the next look (once the hour has
    changed) reaches the safety rules and kills it for good.
    """
    f = Filter(Src(top10_over_25()), log)
    assert f.check(make_pair(price_change_h1=90.0)).rule == 8     # R8 first, R5 not consulted
    assert "MINT" in f.benched and "MINT" not in f.dead
    f.benched.clear()
    assert f.check(make_pair()).rule == 5                          # calmer hour -> R5 lands
    assert "MINT" in f.dead and "MINT" not in f.benched


def test_one_permanent_hit_in_a_mixed_verdict_wins(log):
    """_kill decides on the whole hit list, so any safety rule in it kills for good."""
    f = Filter(Src(make_report()), log)
    f._kill(make_pair(), [(3, "thin"), (5, "concentrated")])
    assert "MINT" in f.dead and "MINT" not in f.benched
    assert [r for r in log.records if r["event"] == "kill"][-1]["permanent"] is True


def test_market_only_hits_are_benched_together(log):
    f = Filter(Src(make_report()), log)
    assert f.check(make_pair(liquidity_usd=1, volume_h1=1, price_change_h1=99.0)).rule == 3
    assert "MINT" in f.benched and "MINT" not in f.dead


def test_lowest_rule_reported_and_all_listed(log):
    v = run(log, pair=make_pair(liquidity_usd=1, volume_h1=1))
    assert v.rule == 3 and "R4" in v.detail


def test_rule7_insider_networks(log):
    supply = 1_000_000_000_000
    nets = [{"currentHolding": supply * 0.06}, {"currentHolding": supply * 0.05}]
    assert run(log, report=make_report(insiderNetworks=nets, graphInsidersDetected=30)).rule == 7
    nets = [{"currentHolding": supply * 0.04}]
    assert run(log, report=make_report(insiderNetworks=nets, graphInsidersDetected=5)).alive


def test_rule7_summed_networks_are_reported_as_a_lower_bound(log):
    """RugCheck gives each network's holdings but not its wallets, so overlaps are counted twice.

    MNST logged "bundled/insider wallets hold 216.8%" on 2026-11-06 — a share of supply that cannot
    exist. The sum cannot be deduplicated from this data, so it is reported as a lower bound and
    said to be a sum, rather than printed as if it were a share.
    """
    supply = 1_000_000_000_000
    nets = [{"currentHolding": supply * 1.2}, {"currentHolding": supply * 0.968}]  # sums to 216.8%
    v = run(log, report=make_report(insiderNetworks=nets, graphInsidersDetected=2000))
    assert v.rule == 7                                   # the verdict is unaffected
    assert ">=216.8%" in v.detail
    assert "wallets may overlap" in v.detail


def test_rule7_insiders_without_sizes_is_dead(log):
    assert run(log, report=make_report(insiderNetworks=None, graphInsidersDetected=30)).rule == 7


def _safe(**kw):
    """A Safety that passes every rule, so one field at a time can be pushed to its threshold."""
    d = dict(mint_authority_revoked=True, freeze_authority_revoked=True, lp_locked_pct=100.0,
             top10_pct=20.0, dev_pct=0.0, dev_past_rugs=0, bundle_pct=0.0, holders=1500)
    d.update(kw)
    return Safety(**d)


# (threshold, how to build the case, rule, which side kills, does the threshold value itself kill?)
BOUNDARIES = [
    ("FILTER_MIN_LIQUIDITY_USD", lambda v: market_rules(make_pair(liquidity_usd=v)), 3, "below", False),
    ("FILTER_MIN_VOLUME_H1_USD", lambda v: market_rules(make_pair(volume_h1=v)), 4, "below", False),
    ("FILTER_MAX_PRICE_CHANGE_H1_PCT", lambda v: market_rules(make_pair(price_change_h1=v)), 8, "above", True),
    ("FILTER_MIN_LP_LOCKED_PCT", lambda v: safety_rules(_safe(lp_locked_pct=v)), 2, "below", False),
    ("FILTER_MAX_TOP10_PCT", lambda v: safety_rules(_safe(top10_pct=v)), 5, "above", False),
    ("FILTER_MAX_DEV_PCT", lambda v: safety_rules(_safe(dev_pct=v)), 6, "above", False),
    ("FILTER_MAX_BUNDLE_PCT", lambda v: safety_rules(_safe(bundle_pct=v)), 7, "above", False),
]


@pytest.mark.parametrize("const,build,rule,kills_on,at_threshold_kills", BOUNDARIES)
def test_every_rule_threshold_is_pinned(const, build, rule, kills_on, at_threshold_kills):
    """Exactly ON each threshold, which way does the rule fall?

    Seven numeric thresholds, and R8 is the only one where the threshold value itself kills:
    "already up 80%+" is inclusive, while "liquidity under $40k" and the rest are not. That
    asymmetry is correct and deliberate, and it is exactly the kind of thing a later tidy-up
    flips to make the comparisons look consistent. Pin all seven, including the sign.
    """
    t = getattr(rules, const)
    eps = t * 1e-6
    at = [n for n, _ in build(t)]
    assert (rule in at) is at_threshold_kills, f"{const}: exactly {t} should " \
        f"{'kill' if at_threshold_kills else 'survive'}"
    # one step the forbidden way always kills; one step the safe way never does
    worse, better = (t + eps, t - eps) if kills_on == "above" else (t - eps, t + eps)
    assert rule in [n for n, _ in build(worse)], f"{const}: {worse} should kill"
    assert rule not in [n for n, _ in build(better)], f"{const}: {better} should survive"
