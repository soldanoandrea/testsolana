from conftest import make_pair
from desk.agents.analyst import score_components, total
from desk.agents.scout import Scout, in_scope
import time


def test_perfect_and_empty_scores():
    best = dict(chg_h1=30, chg_m5=3, buyers_h1=500, buy_sell_ratio=2.5, holder_growth_pct_h=30,
                liq_to_mcap=0.25, smart_wallets=3)
    assert total(score_components(best)) == 100
    assert total(score_components({})) == 0


def test_no_smart_wallet_data_scores_zero_there():
    c = score_components(dict(smart_wallets=None))
    assert c["smart_wallets"] == 0


def test_dumping_m5_kills_momentum():
    assert score_components(dict(chg_h1=30, chg_m5=-10))["momentum"] == 0


def test_scout_scope():
    assert in_scope(make_pair())
    now = time.time() * 1000
    assert not in_scope(make_pair(created_at_ms=int(now - 29 * 60000)))
    assert not in_scope(make_pair(created_at_ms=int(now - 73 * 3600000)))
    assert not in_scope(make_pair(market_cap=299_999))
    assert not in_scope(make_pair(market_cap=20_000_001))
    assert not in_scope(make_pair(dex_id="orca"))


class _CountingDex:
    """Records the mint sets the Scout actually asks DexScreener about."""

    def __init__(self, discovered):
        self.discovered, self.asked = discovered, []

    def discover_mints(self):
        return set(self.discovered)

    def pairs_for_mints(self, mints):
        self.asked.append(set(mints))
        return []


class _NoGT:
    def discover_mints(self):
        return set()


def _scout(discovered, seen, benched):
    src = type("S", (), {})()
    src.dex, src.gt = _CountingDex(discovered), _NoGT()
    s = Scout(src, type("L", (), {"log": lambda *a, **k: None})())
    s.seen, s.benched = set(seen), dict(benched)
    return s, src.dex


def test_a_running_bench_holds_even_with_an_empty_seen_set():
    """The bench must not depend on `seen`, which is rebuilt from dead|traded at every startup.

    Within a session the two agree, so this only ever bit after a restart: a benched coin looked
    like a coin never seen, came straight back with its hour ignored, and -- because its entry was
    still sitting in `benched` at the old expiry -- came back a second time when it fell due.
    """
    s, dex = _scout({"MINT"}, seen=set(), benched={"MINT": time.time() + 3600})
    assert s.scan() == [] and dex.asked == []        # not even asked about
    assert "MINT" in s.benched                       # and the bench is left to run


def test_a_bench_that_is_due_comes_back_whatever_seen_says():
    """The other half: `| due` has to override both `seen` and the wait."""
    s, dex = _scout({"MINT"}, seen={"MINT"}, benched={"MINT": time.time() - 1})
    s.scan()
    assert dex.asked == [{"MINT"}]
