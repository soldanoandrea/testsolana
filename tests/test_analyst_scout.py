from conftest import make_pair
from desk.agents.analyst import score_components, total
from desk.agents.scout import in_scope
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
