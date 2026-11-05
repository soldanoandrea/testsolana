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
EXECUTOR_MAX_POSITION_PCT = 8.0   # of current balance, per trade

# ---- RISK ----------------------------------------------------------------
RISK_STOP_PCT = -6.0              # hard stop, set before the order goes in
RISK_TRAIL_ACTIVATE_PCT = 12.0    # switch to trailing stop after +12%
RISK_TRAIL_DISTANCE_PCT = 6.0     # trail sits 6% under the peak (never lower than the current stop)
