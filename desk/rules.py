"""The desk's trading rules.

These are constants on purpose. They are NOT read from a config file or the
environment, so paper and live run on exactly the same numbers and nobody can
loosen them at runtime. If you change one here, it shows up in git history.
"""
from types import MappingProxyType

# ---- SCOUT ---------------------------------------------------------------
SCOUT_MIN_AGE_MIN = 30            # pair age lower bound (minutes)
SCOUT_MAX_AGE_H = 72              # pair age upper bound (hours)
SCOUT_MIN_MCAP_USD = 300_000
SCOUT_MAX_MCAP_USD = 20_000_000
# DexScreener dexIds for Pump.fun (bonding curve + PumpSwap AMM) and Raydium
SCOUT_DEX_IDS = frozenset({"pumpfun", "pumpswap", "raydium"})

# ---- FILTER (8 kill rules) -----------------------------------------------
FILTER_MIN_LIQUIDITY_USD = 40_000
FILTER_MIN_VOLUME_H1_USD = 50_000
FILTER_MAX_TOP10_PCT = 25.0
FILTER_MAX_DEV_PCT = 3.0
FILTER_MAX_BUNDLE_PCT = 10.0
FILTER_MAX_PRICE_CHANGE_H1_PCT = 80.0   # "already up 80%+": >= 80 kills
FILTER_MIN_LP_LOCKED_PCT = 95.0         # "LP burned or locked": >=95% of LP locked/burned

RULE_NAMES = MappingProxyType({
    1: "R1_MINT_OR_FREEZE_AUTHORITY",
    2: "R2_LP_NOT_BURNED_OR_LOCKED",
    3: "R3_LIQUIDITY_UNDER_40K",
    4: "R4_VOLUME_1H_UNDER_50K",
    5: "R5_TOP10_OVER_25PCT",
    6: "R6_DEV_OVER_3PCT_OR_PAST_RUGS",
    7: "R7_BUNDLED_OVER_10PCT",
    8: "R8_UP_80PCT_LAST_HOUR",
})

# Rules 1, 2, 5, 6 and 7 are facts about the token: a coin that fails one is dead for good.
# Rules 3, 4 and 8 are market conditions that change by the hour — thin liquidity at midnight can
# be deep by 3 a.m. Those bench the coin for FILTER_BENCH_S and it gets another look afterwards.
# They are also the cheap rules (no extra API calls), so re-checking a benched coin costs nothing
# until it actually passes them.
FILTER_TRANSIENT_RULES = frozenset({3, 4, 8})
FILTER_BENCH_S = 1800
assert FILTER_TRANSIENT_RULES < frozenset(RULE_NAMES)

# ---- ANALYST -------------------------------------------------------------
ANALYST_MIN_SCORE = 70
ANALYST_WEIGHTS = MappingProxyType({
    "momentum": 20,
    "unique_buyers": 15,
    "buy_sell_ratio": 20,
    "holder_growth": 15,
    "liquidity_to_mcap": 15,
    "smart_wallets": 15,
})
assert sum(ANALYST_WEIGHTS.values()) == 100

# ---- EXECUTOR ------------------------------------------------------------
EXECUTOR_MAX_POSITION_PCT = 5.0   # of current balance, per trade
EXECUTOR_MAX_OPEN_POSITIONS = 6   # hard cap on positions held at the same time

# ---- LIVE GATE -----------------------------------------------------------
# Paper has to have proved something, not just produced rows. Constants, not config: the gate
# that stands between the logic and real money is not something an env var gets to loosen.
LIVE_MIN_CLEAN_PAPER_TRADES = 20
LIVE_MIN_PAPER_PNL_USD = 0.0      # net P/L over those closed trades must be above this

# ---- RISK ----------------------------------------------------------------
# A memecoin at $1-5M mcap moves 6% on noise alone, so a 6% stop is a coin flip
# charged 3.6% round-trip friction. The stop is wide enough to survive the noise
# and the size is cut so that a stop-out still costs ~1.5% of the balance.
RISK_STOP_PCT = -30.0             # hard stop, set before the order goes in
RISK_TRAIL_ACTIVATE_PCT = 60.0    # switch to trailing stop after +60%
RISK_TRAIL_DISTANCE_PCT = 20.0    # trail sits 20% under the peak (never lower than the current stop)

# ---- DESK (daily kill switch) -------------------------------------------
DESK_MAX_DAILY_LOSS_PCT = 15.0    # equity under the day's open by this much -> no new entries today

# Worst case: every open position stops out at once. It has to stay inside the
# daily loss limit, otherwise the kill switch could never fire before the damage.
MAX_CONCURRENT_RISK_PCT = EXECUTOR_MAX_OPEN_POSITIONS * EXECUTOR_MAX_POSITION_PCT * -RISK_STOP_PCT / 100
assert MAX_CONCURRENT_RISK_PCT < DESK_MAX_DAILY_LOSS_PCT
