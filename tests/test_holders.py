"""The holder table: who counts as a holder, which provider answered, and what a kill means.

The addresses here are real and were measured on 2026-11-07 against the 9 coins whose holder
table the desk managed to capture. They are the whole point of the curve test: every account
RugCheck labelled AMM or LOCKER is off the curve, every unlabelled owner but two is on it, and
those two turned out to be Pump.fun bonding-curve accounts RugCheck simply had no label for.
"""
import pytest
from conftest import make_pair, make_report
from desk.agents.filter import Filter, build_safety, safety_rules
from desk.holders import (SYSTEM_PROGRAM, Holder, Holders, HolderTable, RpcHolders,
                          RugCheckHolders, is_program_derived, is_wallet_address,
                          market_accounts)
from desk.http import DataUnavailable
from test_filter import FakeRPC, FakeRug, Src

RAYDIUM_AUTHORITY = "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1"   # PDA, no account of its own
FUSE_AMM_VAULT_OWNER = "GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL"  # RugCheck: known AMM
PUMP_CURVE = "41uBVQjTcdCqPZXvWoYXbbGGmNzhrQbaMDrZiLA3MJVu"          # owned by the pump.fun program
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_ESCROW = "Cfq1ts1iFr1eUWWBm8eFxUzm5R3YA3UvMZznwiShbgZt"      # off-curve, System-owned, 27.83%
WALLET = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
WALLET2 = "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9"
SUPPLY = 1_000_000_000_000


class FakeHolderRPC(FakeRPC):
    """A keyed endpoint: it answers the indexed call. The public one does not -- see RpcHolders."""

    def __init__(self, largest=(), owners=None, programs=None, raises=None, info=None):
        super().__init__(info)
        self.largest, self.owners = list(largest), dict(owners or {})
        self.programs, self.raises = dict(programs or {}), raises
        self.calls = []

    def token_largest_accounts(self, mint):
        self.calls.append(mint)
        if self.raises:
            raise self.raises
        return self.largest

    def token_account_owners(self, addrs):
        return {a: self.owners.get(a) for a in addrs}

    def account_programs(self, addrs):
        return {a: self.programs.get(a, SYSTEM_PROGRAM) for a in addrs}


def acct(address, amount):
    return {"address": address, "amount": str(amount), "uiAmount": amount / 1e6}


def rpc_chain(**kw):
    rpc = FakeHolderRPC(**kw)
    return Holders([RpcHolders(rpc), RugCheckHolders()]), rpc


# ------------------------------------------------------- what counts as a holder
@pytest.mark.parametrize("addr", [RAYDIUM_AUTHORITY, FUSE_AMM_VAULT_OWNER, PUMP_CURVE])
def test_a_program_derived_address_is_not_a_wallet(addr):
    assert is_program_derived(addr) and not is_wallet_address(addr)


@pytest.mark.parametrize("addr", [WALLET, WALLET2])
def test_a_real_wallet_is_a_wallet(addr):
    assert is_wallet_address(addr) and not is_program_derived(addr)


def test_the_coins_own_pools_are_read_out_of_the_report():
    """Alice's two pools, exactly as RugCheck reports them. No list of DEX programs involved."""
    report = make_report(markets=[
        {"pubkey": "FuF1ZiZMLcdxyWkJPPAFfwVQBgYaj8ri9CJmaxQqETCP", "marketType": "pump_fun_amm",
         "liquidityA": "Gkc7r7DLjnCNAPCAVh9q5EdbS3vrDUSwMoapBzYCoMqj",
         "liquidityB": "CQSpjLVbJ2w6nQiJgTgzqxMvpfFn9bySoQwAjRvtTuam",
         "mintLP": "9g1CtgwVKdtmDDwDxg21mRNwgfZsz5Zu4kMn3buVVdeg",
         "liquidityAAccount": {"owner": "FuF1ZiZMLcdxyWkJPPAFfwVQBgYaj8ri9CJmaxQqETCP"},
         "lp": {"lpLockedPct": 100}},
        {"pubkey": "HUk9zmA4QQgpJM84U5TE3JgRxSjRsJAufyK6n4nRns8k", "marketType": "meteoraDlmm",
         "mintLP": SYSTEM_PROGRAM, "lp": {"lpLockedPct": 100}}])
    pools = market_accounts(report, "PAIR")
    assert "FuF1ZiZMLcdxyWkJPPAFfwVQBgYaj8ri9CJmaxQqETCP" in pools
    assert "HUk9zmA4QQgpJM84U5TE3JgRxSjRsJAufyK6n4nRns8k" in pools
    assert "Gkc7r7DLjnCNAPCAVh9q5EdbS3vrDUSwMoapBzYCoMqj" in pools and "PAIR" in pools
    # meteoraDlmm has no LP mint and fills the field with the System Program
    assert SYSTEM_PROGRAM not in pools


def test_a_pool_the_report_names_is_not_a_holder():
    assert not Holder(owner="POOL", pct=13.0, market=True).is_wallet


def test_an_unlabelled_vault_is_caught_by_the_program_that_owns_it():
    """PUMP_CURVE held 1.10% of Alice with no knownAccounts entry and is not in `markets[]`."""
    assert not Holder(owner=PUMP_CURVE, pct=1.1, program_owned=True).is_wallet
    assert Holder(owner=PUMP_CURVE, pct=1.1).is_wallet      # off the curve is not enough


def test_an_escrow_the_system_program_owns_is_counted():
    """11 of PUMP's 20 top holders are off-curve addresses the System Program owns -- ~58% of
    supply in escrowed or vesting allocations. Excluding those made the most concentrated token
    the desk has seen read as the safest (top10 12.11%), which is why off-curve is not a reason."""
    assert is_program_derived(PUMP_ESCROW)
    assert Holder(owner=PUMP_ESCROW, pct=27.83).is_wallet


@pytest.mark.parametrize("addr", ["W0", "", "not base58 !!", "short"])
def test_an_owner_we_cannot_parse_is_counted_not_excluded(addr):
    """Excluding a holder lowers top10_pct and R5 is a kill rule, so nothing is excluded on a
    guess: only an address provably off the curve is dropped."""
    assert Holder(owner=addr, pct=30.0).is_wallet


def test_an_on_curve_address_a_program_owns_is_still_not_a_wallet():
    assert not Holder(owner=WALLET, pct=40.0, program_owned=True).is_wallet


# ------------------------------------------------------- which provider answers
def test_the_chain_prefers_the_rpc_when_it_answers():
    chain, rpc = rpc_chain(largest=[acct("TA", 100_000_000_000)], owners={"TA": WALLET})
    t = chain.table(make_pair(), make_report(), {"supply": SUPPLY})
    assert t.source == "rpc" and t.present
    assert [(h.owner, round(h.pct, 2)) for h in t.wallets] == [(WALLET, 10.0)]


def test_rugcheck_answers_when_the_rpc_refuses_the_indexed_call():
    """The public endpoint says 429 'Too many requests for a specific RPC call' for every mint."""
    chain, rpc = rpc_chain(raises=DataUnavailable("rpc getTokenLargestAccounts: 429"))
    t = chain.table(make_pair(), make_report(), {"supply": SUPPLY})
    assert t.source == "rugcheck" and t.present and t.insiders_known
    assert rpc.calls == ["MINT"]


def test_a_provider_that_breaks_does_not_break_the_verdict():
    """An endpoint missing the method raises AttributeError, not DataUnavailable."""
    class Broken:
        name = "broken"
        def table(self, *a):
            raise AttributeError("no attribute 'token_largest_accounts'")

    t = Holders([Broken(), RugCheckHolders()]).table(make_pair(), make_report())
    assert t.source == "rugcheck" and t.present


def test_when_nobody_answers_the_table_is_absent_and_names_both_failures():
    chain, _ = rpc_chain(raises=DataUnavailable("429"))
    t = chain.table(make_pair(), make_report(topHolders=None), {"supply": SUPPLY})
    assert not t.present and t.source == "none"
    assert "rpc: " in t.note and "rugcheck: " in t.note


def test_the_chain_divides_by_the_chains_own_supply():
    """A ground-truth numerator over a third party's denominator is neither."""
    chain, _ = rpc_chain(largest=[acct("TA", 100_000_000_000)], owners={"TA": WALLET})
    t = chain.table(make_pair(), make_report(token={"supply": 2 * SUPPLY}), {"supply": SUPPLY})
    assert round(t.wallets[0].pct, 2) == 10.0


# ------------------------------------------------------- the rpc table itself
def test_two_token_accounts_of_one_wallet_are_one_holder():
    """RugCheck's table lists token accounts; a wallet holding through two of them reads as two
    smaller holders there and as one position here."""
    chain, _ = rpc_chain(largest=[acct("TA1", 60_000_000_000), acct("TA2", 40_000_000_000)],
                         owners={"TA1": WALLET, "TA2": WALLET})
    t = chain.table(make_pair(), make_report(), {"supply": SUPPLY})
    assert len(t.wallets) == 1 and round(t.wallets[0].pct, 2) == 10.0


def test_the_rpc_path_drops_a_bonding_curve_by_its_owning_program():
    chain, _ = rpc_chain(largest=[acct("TA1", 800_000_000_000), acct("TA2", 50_000_000_000)],
                         owners={"TA1": PUMP_CURVE, "TA2": WALLET},
                         programs={PUMP_CURVE: PUMP_PROGRAM})
    t = chain.table(make_pair(), make_report(), {"supply": SUPPLY})
    assert [h.owner for h in t.wallets] == [WALLET]
    assert [h.owner for h in t.excluded] == [PUMP_CURVE]


def test_a_token_account_whose_owner_will_not_resolve_is_not_credited():
    chain, _ = rpc_chain(largest=[acct("TA1", 500_000_000_000), acct("TA2", 10_000_000_000)],
                         owners={"TA2": WALLET})
    t = chain.table(make_pair(), make_report(), {"supply": SUPPLY})
    assert [h.owner for h in t.holders] == [WALLET]


def test_the_rpc_path_needs_a_denominator():
    chain, _ = rpc_chain(largest=[acct("TA", 1)], owners={"TA": WALLET})
    t = chain.table(make_pair(), make_report(token={}), None)
    assert t.source == "rugcheck"   # fell through: no supply, no share of supply


# ------------------------------------------------------- what the rules then read
def test_rpc_holders_carry_no_insider_flags_so_r7_rests_on_the_graph():
    chain, _ = rpc_chain(largest=[acct("TA", 10_000_000_000)], owners={"TA": WALLET})
    report = make_report()
    s = build_safety(make_pair(), report, None, lambda m: False,
                     holders=chain.table(make_pair(), report, {"supply": SUPPLY}))
    assert s.bundle_pct == 0.0 and any("insider flags unavailable" in n for n in s.notes)
    assert 7 not in [n for n, _ in safety_rules(s)]


def test_an_insider_graph_without_sizes_still_kills_on_r7_with_an_rpc_table():
    chain, _ = rpc_chain(largest=[acct("TA", 10_000_000_000)], owners={"TA": WALLET})
    report = make_report(graphInsidersDetected=3, insiderNetworks=None)
    s = build_safety(make_pair(), report, None, lambda m: False,
                     holders=chain.table(make_pair(), report, {"supply": SUPPLY}))
    assert s.bundle_pct is None and 7 in [n for n, _ in safety_rules(s)]


def test_an_absent_holder_table_kills_on_r5_alone(log):
    """It used to fire R5, R6 and R7 at once: three rules reporting one missing field. The dev's
    share comes from creatorBalance and the bundle from the insider graph, neither of which needs
    the holder table."""
    f = Filter(Src(make_report(topHolders=None, creatorBalance=0)), log)
    v = f.check(make_pair())
    assert v.rule == 5 and "also" not in v.detail
    assert v.safety.dev_pct == 0.0 and v.safety.bundle_pct == 0.0
    assert v.safety.holders_source == "none"


def test_the_dev_share_is_read_from_the_balance_when_there_is_no_table(log):
    f = Filter(Src(make_report(topHolders=None, creatorBalance=90_000_000_000)), log)
    v = f.check(make_pair())
    assert v.safety.dev_pct == 9.0
    assert sorted(n for n, _ in safety_rules(v.safety)) == [5, 6]


def test_the_filter_logs_which_provider_answered(log):
    chain, _ = rpc_chain(largest=[acct("TA", 10_000_000_000)], owners={"TA": WALLET})
    src = Src(make_report(), rpc={"mintAuthority": None, "freezeAuthority": None, "supply": SUPPLY})
    v = Filter(src, log, holders=chain).check(make_pair())
    assert v.alive
    assert [r["holders_src"] for r in log.records if r["event"] == "survived_filter"] == ["rpc"]


def test_a_kill_says_which_provider_answered(log):
    f = Filter(Src(make_report(topHolders=None)), log)
    f.check(make_pair())
    assert [r.get("holders_src") for r in log.records if r["event"] == "kill"] == ["none"]


# ------------------------------------------------------- the cheap half of the ground truth
def test_the_chain_classifies_owners_even_when_rugcheck_answered():
    """`getMultipleAccounts` is not an indexed call, so the public endpoint answers it. On Alice
    this is what catches the 1.10% bonding-curve account RugCheck had no label for."""
    rpc = FakeHolderRPC(programs={PUMP_CURVE: PUMP_PROGRAM})
    chain = Holders([RugCheckHolders()], rpc=rpc)
    report = make_report(topHolders=[{"owner": PUMP_CURVE, "address": "A1", "pct": 1.1},
                                     {"owner": WALLET, "address": "A2", "pct": 2.0}],
                         knownAccounts={})
    t = chain.table(make_pair(), report)
    assert t.source == "rugcheck"
    assert [h.owner for h in t.wallets] == [WALLET]
    assert [h.owner for h in t.excluded] == [PUMP_CURVE]


def test_a_failed_classification_leaves_the_table_as_it_was():
    """Unclassified means R5 counts an account it might have excluded: the safe direction."""
    class Down(FakeHolderRPC):
        def account_programs(self, addrs):
            raise DataUnavailable("rpc down")

    chain = Holders([RugCheckHolders()], rpc=Down())
    t = chain.table(make_pair(), make_report())
    assert t.present and len(t.wallets) == 15      # the pool is still out on its label alone
    assert "owners unclassified" in t.note
