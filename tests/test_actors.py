"""The actor store: what it indexes, what it refuses to index, and what it records only once."""
import json

import pytest
from conftest import make_pair, make_report
from desk.actors import NOT_AN_ACTOR, ActorStore, backfill_from_events
from desk.agents.filter import Filter
from desk.models import Safety
from test_filter import Src


@pytest.fixture
def store(tmp_path):
    return ActorStore(tmp_path / "actors.jsonl")


def rec(store, mint, symbol, report, verdict="SURVIVED"):
    return store.record_raw(mint, symbol, report, verdict, safety=Safety())


# ---------------------------------------------------------------- identities
def test_creator_links_two_coins(store):
    rec(store, "M1", "ONE", make_report(creator="DEV1"))
    rec(store, "M2", "TWO", make_report(creator="DEV1"))
    assert store.summary()["repeat_creators"] == {"DEV1": ["ONE", "TWO"]}


def test_a_coin_is_never_its_own_prior(store):
    """The same coin looked at twice must not report itself as a repeat actor."""
    r = make_report(creator="DEV1")
    rec(store, "M1", "ONE", r)
    rec(store, "M1", "ONE", r)
    p = store.priors("M1", r)
    assert p.creator_coins == [] and p.repeat_wallets == {}
    assert not p.any


def test_priors_find_a_repeat_holder_wallet(store):
    r1 = make_report(topHolders=[{"owner": "WHALE", "address": "A", "pct": 9.0, "insider": True}])
    rec(store, "M1", "ONE", r1)
    r2 = make_report(topHolders=[{"owner": "WHALE", "address": "B", "pct": 4.0, "insider": False}])
    p = store.priors("M2", r2)
    assert p.repeat_wallets == {"WHALE": ["ONE"]}
    assert p.any


def test_symbol_is_not_an_identity_but_the_mint_is(store):
    """Two different mints shared the symbol MrBeast in the real log. They are different coins."""
    rec(store, "M1", "SAME", make_report(creator="DEV1"))
    rec(store, "M2", "SAME", make_report(creator="DEV2"))
    assert len(store.coins) == 2
    assert store.summary()["repeat_creators"] == {}


# ---------------------------------------------------------------- exclusions
@pytest.mark.parametrize("kind", sorted(NOT_AN_ACTOR))
def test_pool_vaults_and_lockers_are_not_actors(store, kind):
    """The Pump Fun AMM holds a slice of every pumpswap coin: counting it would bury real actors."""
    holders = [{"owner": "VAULT", "address": "A", "pct": 30.0, "insider": False}]
    r = make_report(topHolders=holders, knownAccounts={"VAULT": {"type": kind}})
    rec(store, "M1", "ONE", r)
    assert "VAULT" not in store.by_wallet
    assert store.priors("M2", r).repeat_wallets == {}


def test_insider_networks_are_recorded_but_never_indexed_as_actors(store):
    """RugCheck never gives a network's wallets, and its slug ids were measured not to recur."""
    nets = [{"id": "peaceful-champagne-stork", "size": 1997, "type": "transfer",
             "currentHolding": 606_316_168_952}]
    r = rec(store, "M1", "ONE", make_report(insiderNetworks=nets))
    assert r["networks"][0]["id"] == "peaceful-champagne-stork"
    assert r["networks"][0]["holding_pct"] == pytest.approx(60.63, abs=0.01)
    assert "peaceful-champagne-stork" not in store.by_wallet
    assert store.network_ids["peaceful-champagne-stork"] == {"M1"}


# ---------------------------------------------------------------- perishability
def test_an_empty_holder_table_is_flagged_not_read_as_no_concentration(store):
    """Every one of the 67 mints the desk had seen returned an empty topHolders when re-queried."""
    assert rec(store, "M1", "ONE", make_report(topHolders=[]))["holders_erased"] is True
    assert rec(store, "M2", "TWO", make_report(topHolders=None))["holders_erased"] is True
    assert rec(store, "M3", "THREE", make_report())["holders_erased"] is False


def test_a_coin_is_written_again_so_a_change_can_be_seen(store):
    """LIA's creator read 32.73% then 81.39% minutes later. Dedup would have hidden that."""
    s1, s2 = Safety(), Safety()
    s1.dev_pct, s2.dev_pct = 32.73, 81.39
    store.record_raw("M1", "LIA", make_report(), "R2", safety=s1)
    store.record_raw("M1", "LIA", make_report(), "R2", safety=s2)
    pcts = [json.loads(l)["creator_pct"] for l in store.path.read_text().splitlines()]
    assert pcts == [32.73, 81.39]
    assert store.records == 2 and len(store.coins) == 1


# ---------------------------------------------------------------- persistence
def test_the_index_is_rebuilt_from_the_file_not_stored(store):
    rec(store, "M1", "ONE", make_report(creator="DEV1",
        topHolders=[{"owner": "WHALE", "address": "A", "pct": 9.0, "insider": True}]))
    rec(store, "M2", "TWO", make_report(creator="DEV1"))
    reloaded = ActorStore(store.path)
    assert reloaded.summary() == store.summary()
    assert reloaded.by_creator["DEV1"] == {"M1", "M2"}
    assert [s.role for s in reloaded.by_wallet["WHALE"]] == ["holder"]


def test_a_truncated_line_does_not_lose_the_rest_of_the_file(store):
    rec(store, "M1", "ONE", make_report(creator="DEV1"))
    with store.path.open("a") as f:
        f.write('{"mint": "M2", "symbol"\n')          # half-written record
    rec(store, "M3", "THREE", make_report(creator="DEV1"))
    assert ActorStore(store.path).by_creator["DEV1"] == {"M1", "M3"}


# ---------------------------------------------------------------- the Filter
def test_the_filter_records_every_report_it_paid_for(store, log):
    f = Filter(Src(make_report(), rpc={"mintAuthority": None, "freezeAuthority": None}), log,
               actors=store)
    assert f.check(make_pair()).alive
    assert [json.loads(l)["verdict"] for l in store.path.read_text().splitlines()] == ["SURVIVED"]


def test_a_kill_is_recorded_with_the_rule_that_killed_it(store, log):
    f = Filter(Src(make_report(markets=[{"pubkey": "PAIR", "lp": {"lpLockedPct": 0}}])), log,
               actors=store)
    assert not f.check(make_pair()).alive
    assert json.loads(store.path.read_text().splitlines()[0])["verdict"] == "R2_LP_NOT_BURNED_OR_LOCKED"


def test_a_coin_killed_on_a_market_rule_costs_no_actor_record(store, log):
    """Market rules run before RugCheck, so there is no report to record."""
    f = Filter(Src(make_report()), log, actors=store)
    assert not f.check(make_pair(liquidity_usd=1_000)).alive
    assert store.records == 0


def test_the_filter_logs_a_prior_and_never_lets_it_change_the_verdict(store, log):
    rec(store, "M1", "ONE", make_report(creator="DEV1"))
    r = make_report(creator="DEV1")
    f = Filter(Src(r, rpc={"mintAuthority": None, "freezeAuthority": None}), log, actors=store)
    v = f.check(make_pair(mint="M2"))
    prior = [e for e in log.records if e["event"] == "actor_prior"]
    assert prior and prior[0]["creator_coins"] == ["ONE"]
    assert v.alive, "a prior is reported, never acted on"


def test_a_store_that_cannot_be_written_does_not_break_a_verdict(store, log, monkeypatch):
    def boom(rec): raise OSError("disk full")
    monkeypatch.setattr(store, "append", boom)
    f = Filter(Src(make_report(), rpc={"mintAuthority": None, "freezeAuthority": None}), log,
               actors=store)
    assert f.check(make_pair()).alive
    assert [e for e in log.records if e["event"] == "error"]


def test_the_filter_works_with_no_store_at_all(log):
    f = Filter(Src(make_report(), rpc={"mintAuthority": None, "freezeAuthority": None}), log)
    assert f.check(make_pair()).alive


# ---------------------------------------------------------------- backfill
def test_backfill_takes_the_verdict_from_the_log_and_skips_coins_on_file(store):
    class Rug:
        def report(self, mint, max_age_s=0): return make_report(creator=f"DEV_{mint}")
    rec(store, "M1", "ONE", make_report(creator="DEV1"))
    events = [{"event": "kill", "mint": "M1", "symbol": "ONE", "rule_name": "R3_LIQUIDITY_UNDER_40K"},
              {"event": "kill", "mint": "M2", "symbol": "TWO", "rule_name": "R2_LP_NOT_BURNED_OR_LOCKED"},
              {"event": "survived_filter", "mint": "M3", "symbol": "THREE"}]
    out = backfill_from_events(store, events, Rug(), sleep_s=0, echo=lambda *a: None)
    assert out == {"coins": 3, "recorded": 2, "no_creator": 0, "failed": 0, "holders_recovered": 2}
    written = {json.loads(l)["mint"]: json.loads(l) for l in store.path.read_text().splitlines()}
    assert written["M2"]["verdict"] == "R2_LP_NOT_BURNED_OR_LOCKED"
    assert written["M3"]["verdict"] == "SURVIVED"
    assert written["M2"]["source"] == "backfill"
    assert written["M1"]["source"] == "live", "a live record is never overwritten by a backfill"


def test_backfill_survives_a_mint_rugcheck_refuses(store):
    class Rug:
        def report(self, mint, max_age_s=0):
            if mint == "BAD": raise RuntimeError("HTTP 400")
            return make_report()
    events = [{"event": "kill", "mint": "BAD", "symbol": "MNST", "rule_name": "R7_BUNDLED_OVER_10PCT"},
              {"event": "kill", "mint": "OK", "symbol": "GOOD", "rule_name": "R3_LIQUIDITY_UNDER_40K"}]
    out = backfill_from_events(store, events, Rug(), sleep_s=0, echo=lambda *a: None)
    assert out["failed"] == 1 and out["recorded"] == 1
