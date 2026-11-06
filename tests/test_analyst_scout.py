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


def test_the_scan_event_shows_the_whole_funnel():
    """153 candidates becoming 1 hand-off is the night's real shape; the log has to show it.

    A mint only enters `seen` once it has been handed over, so one that is out of scope is asked
    about again on every scan. With only the two ends logged, that re-asking was invisible: the
    scan event named a count and a hand-off and nothing in between.
    """
    now = time.time() * 1000

    class Dex:
        def discover_mints(self):
            return {"INSCOPE", "TOOOLD", "NOPAIR"}

        def pairs_for_mints(self, mints):
            return [_ds("INSCOPE", now - 3 * 3600_000),      # in scope
                    _ds("TOOOLD", now - 100 * 3600_000)]     # has a pair, aged out

    src = type("S", (), {})()
    src.dex, src.gt = Dex(), _NoGT()
    events = []
    s = Scout(src, type("L", (), {"log": lambda self, e, **k: events.append((e, k))})())
    out = s.scan()

    assert [p.mint for p in out] == ["INSCOPE"]
    scan = dict(next(k for e, k in events if e == "scan"))
    assert scan["candidates"] == 3          # all three asked about
    assert scan["with_pair"] == 2           # NOPAIR came back with nothing
    assert scan["out_of_scope"] == 1        # TOOOLD had a pair but failed in_scope
    assert scan["handed_to_filter"] == 1
    assert "TOOOLD" not in s.seen and "NOPAIR" not in s.seen   # so both are asked again next scan


def _ds(mint, created_ms):
    return {"pairAddress": "P", "baseToken": {"address": mint, "symbol": "X"}, "dexId": "pumpswap",
            "pairCreatedAt": int(created_ms), "priceUsd": "1.0", "marketCap": 1_000_000,
            "liquidity": {"usd": 220_000}, "volume": {"h1": 300_000},
            "priceChange": {"m5": 2, "h1": 30}, "txns": {"h1": {"buys": 900, "sells": 300}}}


class _RowDex(_CountingDex):
    """Like _CountingDex, but answers with DexScreener-shaped rows."""

    def __init__(self, discovered, rows):
        super().__init__(discovered)
        self.rows = rows

    def pairs_for_mints(self, mints):
        self.asked.append(set(mints))
        return [r for r in self.rows if r["baseToken"]["address"] in mints]


def _row(mint, *, age_h=3.0, mcap=1_000_000, dex="pumpswap", created_at_ms=None, pair="P"):
    ms = int((time.time() - age_h * 3600) * 1000) if created_at_ms is None else created_at_ms
    return {"pairAddress": pair, "baseToken": {"address": mint, "symbol": mint}, "dexId": dex,
            "pairCreatedAt": ms, "priceUsd": "0.001", "marketCap": mcap,
            "liquidity": {"usd": 150_000}, "volume": {"h1": 200_000},
            "priceChange": {"h1": 25.0, "m5": 2.0}, "txns": {"h1": {"buys": 9, "sells": 4}}}


def _scout_rows(discovered, rows):
    src = type("S", (), {})()
    src.dex, src.gt = _RowDex(discovered, rows), _NoGT()
    return Scout(src, type("L", (), {"log": lambda *a, **k: None})()), src.dex


def test_a_token_past_the_age_ceiling_is_only_asked_about_once():
    """Age is the one disqualification that cannot reverse, so re-asking is pure waste.

    Measured on a live scan: 153 candidates, 152 out of scope, and half of those were older than
    72h -- asked again every two minutes for as long as the feeds kept listing them.
    """
    s, dex = _scout_rows({"OLD"}, [_row("OLD", age_h=73)])
    assert s.scan() == [] and dex.asked == [{"OLD"}]
    assert "OLD" in s.aged_out
    assert s.scan() == [] and dex.asked == [{"OLD"}]      # not asked a second time


def test_an_unknown_creation_time_is_never_cached_as_old():
    """`created_at_ms` falls back to 0, which reads as an age of ~29 million minutes.

    Caching that would drop a brand-new pair for ever, silently -- the exact shape of bug this
    cache could introduce, so the guard matters more than the saving.
    """
    s, dex = _scout_rows({"NEW"}, [_row("NEW", created_at_ms=0)])
    s.scan()
    assert s.aged_out == set()
    s.scan()
    assert dex.asked == [{"NEW"}, {"NEW"}]               # asked again, as it must be


def test_a_small_mcap_is_asked_about_again():
    """The other half of what each scan wastes is mcap under 300k, which can grow into the band."""
    s, dex = _scout_rows({"SMALL"}, [_row("SMALL", mcap=299_999)])
    s.scan()
    s.scan()
    assert s.aged_out == set() and dex.asked == [{"SMALL"}, {"SMALL"}]


def test_one_old_pool_does_not_cache_a_token_that_also_has_a_fresh_one():
    """A token is cached only when every pair that came back for it is past the ceiling.

    `best` keeps the most liquid in-scope pair, so the old pool is the one we may happen to see
    first; it says nothing about the token.
    """
    rows = [_row("MIXED", age_h=73, pair="OLDPOOL"),
            _row("MIXED", age_h=3, mcap=299_999, pair="NEWPOOL")]
    s, dex = _scout_rows({"MIXED"}, rows)
    assert s.scan() == []                                # out of scope on mcap, not handed over
    assert s.aged_out == set()                           # but not written off either
    s.scan()
    assert dex.asked == [{"MIXED"}, {"MIXED"}]
