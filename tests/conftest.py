import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
from desk.models import Pair


def make_pair(**kw):
    d = dict(pair_address="PAIR", mint="MINT", symbol="TST", dex_id="pumpswap",
             created_at_ms=int((time.time() - 3 * 3600) * 1000), price_usd=0.001, market_cap=1_000_000,
             liquidity_usd=150_000, volume_h1=200_000, price_change_m5=2.0, price_change_h1=25.0,
             buys_h1=900, sells_h1=400)
    d.update(kw)
    return Pair(**d)


def make_report(**kw):
    r = {
        "token": {"mintAuthority": None, "freezeAuthority": None, "supply": 1_000_000_000_000, "decimals": 6},
        "creator": "DEV", "creatorBalance": 0, "creatorTokens": [], "totalHolders": 1500,
        "knownAccounts": {"PAIR": {"type": "AMM"}},
        "markets": [{"pubkey": "PAIR", "lp": {"lpLockedPct": 100}}],
        "topHolders": [{"owner": "PAIR", "address": "V", "pct": 20.0, "insider": False}]
                      + [{"owner": f"W{i}", "address": f"A{i}", "pct": 1.5, "insider": False} for i in range(15)],
        "graphInsidersDetected": 0,
    }
    r.update(kw)
    return r


class Log:
    def __init__(self): self.records = []
    def log(self, event, **d): self.records.append({"event": event, **d}); return d
    def kill(self, v, **extra): self.log("kill", rule=v.rule, rule_name=v.rule_name, detail=v.detail, **extra)
    def violation(self, what, **d): self.log("violation", what=what, **d)


@pytest.fixture
def log():
    return Log()
