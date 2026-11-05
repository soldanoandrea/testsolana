from conftest import make_pair
from desk import rules
from desk.agents.risk import Risk
from desk.agents.executor import Executor
from desk.brokers import PaperBroker
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
