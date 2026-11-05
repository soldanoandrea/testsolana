import json

import pytest
from conftest import make_pair
from desk import rules
from desk.agents.risk import Risk
from desk.agents.executor import Executor
from desk.brokers import Fill, PaperBroker, sell_slippage_bps
from desk.http import DataUnavailable
from desk.config import Config
from desk.models import Position, Score
from desk.state import State


STOP = 1 + rules.RISK_STOP_PCT / 100              # 0.70
TRAIL = 1 - rules.RISK_TRAIL_DISTANCE_PCT / 100   # 0.70
ON = 1 + rules.RISK_TRAIL_ACTIVATE_PCT / 100      # 1.60


def pos(entry=1.0):
    return Position("M", "T", "P", 80, entry, 100, 100, entry * STOP, 0, entry)


def test_stop_at_minus_30(log):
    assert abs(Risk(log).stop_for(1.0) - STOP) < 1e-12
    assert rules.RISK_STOP_PCT == -30.0


def test_stop_exit(log):
    r, p = Risk(log), pos()
    assert r.update(p, STOP + 0.01) is None
    assert r.update(p, STOP) == "stop_loss"


def test_trailing_after_60_and_never_lowers(log):
    r, p = Risk(log), pos()
    assert r.update(p, ON - 0.01) is None and not p.trailing
    r.update(p, ON)
    assert p.trailing and abs(p.stop_price - ON * TRAIL) < 1e-9
    r.update(p, 2.00)
    s = p.stop_price
    assert abs(s - 2.00 * TRAIL) < 1e-9
    r.update(p, 1.50)            # price dips, stop stays
    assert p.stop_price == s
    assert r.update(p, s - 0.01) == "trailing_stop"


def test_trail_locks_in_a_gain(log):
    """The trail can only ever activate above the entry, so it never locks in a loss."""
    assert ON * TRAIL > 1.0


def test_lowering_stop_blocked(log):
    r, p = Risk(log), pos()
    r.guard_stop(p, 0.5)
    assert p.stop_price == STOP
    assert any(x["event"] == "violation" for x in log.records)
    assert p.raise_stop(0.1) is False


def make_exec(tmp_path, log, cash=1000.0):
    st = State(path=tmp_path / "s.json", cash=cash, start_balance=cash)
    cfg = Config(data_dir=tmp_path)
    return Executor(PaperBroker(cfg, st), Risk(log), st, log), st


def test_size_5pct_and_stop_before_order(tmp_path, log):
    ex, st = make_exec(tmp_path, log)
    p = ex.enter(Score(make_pair(), 75, {}, {}))
    assert p.cost_usd == 50.0 and st.cash == 950.0
    events = [r["event"] for r in log.records]
    assert events.index("stop_set") < events.index("trade_open")
    assert p.stop_price <= p.entry_price * STOP + 1e-12


def test_stop_out_costs_about_1_5pct_of_balance(tmp_path, log):
    """Wider stop, smaller size: a stop-out must still cost ~1.5% of the balance."""
    ex, st = make_exec(tmp_path, log)
    p = ex.enter(Score(make_pair(price_usd=1.0), 80, {}, {}))
    before = st.cash + p.cost_usd
    ex.exit(p, p.entry_price * STOP, "stop_loss")
    hit_pct = (st.cash - before) / before * 100
    assert -2.0 < hit_pct < -1.3, hit_pct


def test_max_open_positions_cap(tmp_path, log):
    ex, st = make_exec(tmp_path, log, cash=100_000.0)
    for i in range(rules.EXECUTOR_MAX_OPEN_POSITIONS):
        assert ex.enter(Score(make_pair(mint=f"M{i}"), 80, {}, {})) is not None
    assert len(st.positions) == rules.EXECUTOR_MAX_OPEN_POSITIONS
    assert ex.enter(Score(make_pair(mint="OVER"), 99, {}, {})) is None
    assert "OVER" not in st.traded      # refused, not burned: it can be looked at again
    assert any(r["event"] == "skip" and "open positions" in r["reason"] for r in log.records)


def test_halt_blocks_entries_and_resume_lifts_it(tmp_path, log):
    ex, st = make_exec(tmp_path, log)
    ex.halt("daily loss limit")
    assert ex.enter(Score(make_pair(), 80, {}, {})) is None
    assert st.cash == 1000.0 and not st.traded
    assert any(r["event"] == "skip" and "halted" in r["reason"] for r in log.records)
    ex.resume()
    assert ex.enter(Score(make_pair(), 80, {}, {})) is not None


def test_size_never_breaches_the_cap(tmp_path, log):
    """round() used to round up half a cent and trip the Executor's own assert."""
    ex, _ = make_exec(tmp_path, log)
    cap = rules.EXECUTOR_MAX_POSITION_PCT / 100
    for balance in (85_737.5, 1000.0, 1.0, 12_345.67, 99_999.99):
        assert ex.size_for(balance) <= balance * cap


class WorstCaseQuoteBroker:
    """Like LiveBroker: expected_buy_price quotes the worst case allowed by slippage,
    while the real fill comes back at the reference price."""

    def __init__(self, slip_pct):
        self.slip = slip_pct / 100

    def balance_usd(self):
        return 1000.0

    def expected_buy_price(self, mint, usd, ref):
        return ref * (1 + self.slip)

    def buy(self, mint, usd, ref):
        return Fill(price=ref, qty=usd / ref, usd=usd, tx="sig")


def live_exec(tmp_path, log, slip):
    st = State(path=tmp_path / f"s{slip}.json", cash=1000.0, start_balance=1000.0)
    return Executor(WorstCaseQuoteBroker(slip), Risk(log), st, log), st


@pytest.mark.parametrize("slip", [0.0, 3.0, 10.0, 30.0])
def test_stop_is_exactly_the_rule_off_the_real_fill(tmp_path, log, slip):
    """Regression: the stop used to be quoted off the worst case, leaving live tighter than paper."""
    ex, _ = live_exec(tmp_path, log, slip)
    p = ex.enter(Score(make_pair(price_usd=1.0), 80, {}, {}))
    eff = (p.stop_price / p.entry_price - 1) * 100
    assert abs(eff - rules.RISK_STOP_PCT) < 1e-9, eff


def test_slippage_setting_cannot_move_the_stop(tmp_path, log):
    """DESK_LIVE_SLIPPAGE_BPS is operational config; it must not reach a trading rule."""
    a, _ = live_exec(tmp_path, log, 3.0)
    b, _ = live_exec(tmp_path, log, 12.0)
    pa = a.enter(Score(make_pair(price_usd=1.0), 80, {}, {}))
    pb = b.enter(Score(make_pair(price_usd=1.0), 80, {}, {}))
    assert pa.stop_price == pb.stop_price


def test_stop_committed_before_the_order_then_rebased(tmp_path, log):
    ex, _ = live_exec(tmp_path, log, 10.0)
    ex.enter(Score(make_pair(price_usd=1.0), 80, {}, {}))
    events = [r["event"] for r in log.records]
    assert events.index("stop_set") < events.index("trade_open")   # committed before the order
    assert "stop_rebased" in events                                # and corrected onto the real fill
    reb = [r for r in log.records if r["event"] == "stop_rebased"][0]
    assert reb["committed"] > reb["stop"]        # the quoted worst case was the tighter one


def test_tampered_stop_is_raised_back_and_logged(tmp_path, log):
    ex, st = make_exec(tmp_path, log)
    ex.enter(Score(make_pair(price_usd=1.0), 80, {}, {}))
    st.save()
    d = json.loads(st.path.read_text())
    d["positions"]["MINT"]["stop_price"] = 0.0001      # someone lowered the stop on disk
    st.path.write_text(json.dumps(d))

    pos = State.load(st.path, 1000.0, log=log).positions["MINT"]
    assert abs(pos.stop_price - pos.stop_floor()) < 1e-12
    v = [r for r in log.records if r["event"] == "violation" and r["what"] == "stop_below_floor_on_load"]
    assert v and v[0]["stored"] == 0.0001


def test_a_valid_stop_survives_a_reload(tmp_path, log):
    ex, st = make_exec(tmp_path, log)
    p = ex.enter(Score(make_pair(price_usd=1.0), 80, {}, {}))
    raised = p.entry_price * 0.95                      # the trail had already moved it up
    assert p.raise_stop(raised)
    st.save()
    back = State.load(st.path, 1000.0, log=log).positions["MINT"]
    assert abs(back.stop_price - raised) < 1e-12
    assert not [r for r in log.records if r["event"] == "violation"]


class FailingSellBroker(PaperBroker):
    def __init__(self, cfg, state):
        super().__init__(cfg, state)
        self.attempts = []

    def sell(self, mint, qty, ref_price, attempt=0):
        self.attempts.append(attempt)
        raise DataUnavailable("no route")


def test_sell_slippage_keeps_widening_across_ticks(tmp_path, log, monkeypatch):
    monkeypatch.setattr("desk.agents.executor.time.sleep", lambda s: None)
    st = State(path=tmp_path / "s.json", cash=1000.0, start_balance=1000.0)
    b = FailingSellBroker(Config(data_dir=tmp_path), st)
    ex = Executor(b, Risk(log), st, log)
    p = ex.enter(Score(make_pair(price_usd=1.0), 80, {}, {}))

    assert ex.exit(p, 1.0, "stop_loss") is False       # first tick: 4 tries
    assert b.attempts == [0, 1, 2, 3] and p.sell_attempts == 4
    assert ex.exit(p, 1.0, "stop_loss") is False       # next tick carries on, it does not restart
    assert b.attempts == [0, 1, 2, 3, 4, 5, 6, 7]
    assert "GOODMINT" not in st.positions and p.mint in st.positions   # still open


def test_sell_slippage_doubles_then_caps():
    assert [sell_slippage_bps(300, a) for a in range(6)] == [300, 600, 1200, 2400, 2400, 2400]
    assert sell_slippage_bps(300, -1) == 300


def test_concurrent_risk_stays_inside_the_daily_limit():
    assert rules.MAX_CONCURRENT_RISK_PCT < rules.DESK_MAX_DAILY_LOSS_PCT


def test_one_entry_per_coin(tmp_path, log):
    ex, st = make_exec(tmp_path, log)
    assert ex.enter(Score(make_pair(), 90, {}, {}))
    assert ex.enter(Score(make_pair(), 99, {}, {})) is None
    assert len(st.positions) == 1


def test_sub70_refused(tmp_path, log):
    ex, st = make_exec(tmp_path, log)
    assert ex.enter(Score(make_pair(), 69.9, {}, {})) is None and st.cash == 1000


def test_exit_books_pnl(tmp_path, log):
    ex, st = make_exec(tmp_path, log)
    p = ex.enter(Score(make_pair(price_usd=1.0), 80, {}, {}))
    assert ex.exit(p, STOP, "stop_loss")
    c = [r for r in log.records if r["event"] == "trade_close"][0]
    assert c["pnl_pct"] < rules.RISK_STOP_PCT  # fees + slippage make the paper loss honest
    assert not st.positions
