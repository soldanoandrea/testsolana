from conftest import make_pair
from desk import rules
from desk.agents.risk import Risk
from desk.agents.executor import Executor
from desk.brokers import PaperBroker
from desk.config import Config
from desk.models import Position, Score
from desk.state import State


def pos(entry=1.0):
    return Position("M", "T", "P", 80, entry, 100, 100, entry * 0.94, 0, entry)


def test_stop_at_minus_6(log):
    assert abs(Risk(log).stop_for(1.0) - 0.94) < 1e-12


def test_stop_exit(log):
    r, p = Risk(log), pos()
    assert r.update(p, 0.95) is None
    assert r.update(p, 0.94) == "stop_loss"


def test_trailing_after_12_and_never_lowers(log):
    r, p = Risk(log), pos()
    assert r.update(p, 1.11) is None and not p.trailing
    r.update(p, 1.12)
    assert p.trailing and abs(p.stop_price - 1.12 * 0.94) < 1e-9
    r.update(p, 1.50)
    s = p.stop_price
    assert abs(s - 1.50 * 0.94) < 1e-9
    r.update(p, 1.45)            # price dips, stop stays
    assert p.stop_price == s
    assert r.update(p, 1.40) == "trailing_stop"


def test_lowering_stop_blocked(log):
    r, p = Risk(log), pos()
    r.guard_stop(p, 0.5)
    assert p.stop_price == 0.94
    assert any(x["event"] == "violation" for x in log.records)
    assert p.raise_stop(0.1) is False


def make_exec(tmp_path, log, cash=1000.0):
    st = State(path=tmp_path / "s.json", cash=cash, start_balance=cash)
    cfg = Config(data_dir=tmp_path)
    return Executor(PaperBroker(cfg, st), Risk(log), st, log), st


def test_size_8pct_and_stop_before_order(tmp_path, log):
    ex, st = make_exec(tmp_path, log)
    p = ex.enter(Score(make_pair(), 75, {}, {}))
    assert p.cost_usd == 80.0 and st.cash == 920.0
    events = [r["event"] for r in log.records]
    assert events.index("stop_set") < events.index("trade_open")
    assert p.stop_price <= p.entry_price * 0.94 + 1e-12


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
    assert ex.exit(p, 0.94, "stop_loss")
    c = [r for r in log.records if r["event"] == "trade_close"][0]
    assert c["pnl_pct"] < -6  # fees + slippage make the paper loss honest
    assert not st.positions
