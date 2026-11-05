# Solana memecoin desk

Six agents with hard handoffs. Protecting the balance comes first, profit second.

```
SCOUT ──pairs──▶ FILTER ──survivors──▶ ANALYST (watch holders 5 min) ──▶ FILTER re-check (fresh data)
      ──▶ ANALYST score ──70+──▶ EXECUTOR ◀──stop set first── RISK ──stop/trail hit──▶ EXECUTOR sells
LOGGER records every step → data/<mode>/events.jsonl, recap-YYYY-MM-DD.md
```

The thresholds are constants in [`desk/rules.py`](desk/rules.py). Config files and environment
variables can't change them, so paper and live run on exactly the same rules.

## Agents

| Agent | What it does | Data |
|---|---|---|
| **Scout** | Pump.fun (`pumpfun`, `pumpswap`) and Raydium pairs, 30 min–72 h old, mcap $300k–$20M. Each coin is passed to Filter once. | DexScreener (profiles, boosts, search) and GeckoTerminal (trending, per-DEX pools) for discovery. DexScreener for every metric. |
| **Filter** | 8 kill rules (below). One hit kills the coin for good. **Missing data also kills**: if a rule can't be verified, the coin fails it. | RugCheck report, cross-checked against Solana RPC `getAccountInfo` for the authorities |
| **Analyst** | Watches holder count for `DESK_HOLDER_WINDOW_S` (300 s), sends the coin back through Filter on fresh data, then scores it 0–100. 70+ goes to Executor, under 70 dies. | DexScreener, GeckoTerminal (unique buyers/sellers in the last hour), RugCheck holders, RPC (smart wallets) |
| **Executor** | 8% of the current balance per trade. One entry per coin, ever. Never averages down. Asks Risk for the stop before it orders. | Paper fills or Jupiter swaps |
| **Risk** | Stop at −6% from the expected fill, set before the order. Stops can only move up: lowering one is blocked and logged as a violation. At +12% from entry it switches to a trailing stop 6% under the peak, which locks in at least +5.3%. Exits only when the stop or the trail is hit. Checks every 10 s. | DexScreener prices |
| **Logger** | Logs every kill with the exact rule, and every trade with entry, size, score, exit and reason. Writes the morning recap at 08:00 local and on shutdown. | — |

### Filter rules (`R1`…`R8`)
1. Mint or freeze authority not revoked. RPC is the ground truth, and if RugCheck disagrees the coin dies.
2. Less than 95% of the traded pair's LP burned or locked.
3. Liquidity under $40k.
4. 1h volume under $50k.
5. Top 10 holders own more than 25%. Pool vaults and lockers are excluded, since they aren't holders.
6. Dev wallet holds more than 3%, or one of the dev's last 5 launches is flagged `rugged` by RugCheck. An unknown dev also dies.
7. Bundled share over 10%: the most RugCheck shows either in its linked insider networks (wallets funded together or buying in bundles) or in the top holders it flags as insiders.
8. Price already up 80% or more in the last hour.

Rules 3, 4 and 8 are checked first because they need no extra API calls. When a coin fails several
rules, the lowest-numbered one is recorded as the killer and the rest are listed in `detail`.

### Score weights
momentum 20 · unique buyers 15 · buy/sell ratio 20 · holder growth 15 · liquidity/mcap 15 · smart wallets 15.
See `score_components()` in [`desk/agents/analyst.py`](desk/agents/analyst.py) for the curves. A
component with no data scores 0, so the desk never guesses a score upward. **With no
`smart_wallets.txt` the maximum score is 85**, so a coin needs 70 of the remaining 85 points.

## Running

```bash
pip install -r requirements.txt
python -m desk run                 # paper, runs until stopped (Ctrl-C writes the recap)
python -m desk run --hours 10      # overnight paper run
python -m desk recap               # recap for the current paper session
python -m desk status
python -m pytest -q tests
```

Overnight it runs silently: no alerts and no notifications. Read `data/paper/recap-<date>.md` in the morning.

Paper fills are deliberately pessimistic: 1.5% slippage and 0.3% fee per side, plus $0.05 network
fee. They're set with `DESK_PAPER_*`, and the starting balance with `DESK_PAPER_BALANCE_USD` (default $1000).

## Going live

`python -m desk run --live` refuses to start until **all** of these are true:
- the paper log has at least 20 closed trades (`DESK_LIVE_MIN_PAPER_TRADES`)
- the paper log has **zero** rule violations
- `DESK_CONFIRM_LIVE=yes` is set
- `SOLANA_PRIVATE_KEY` is set (base58, for a dedicated hot wallet holding only what you can afford to lose), and `pip install solders` has been run

Live mode buys and sells through Jupiter (`lite-api.jup.ag`) with 3% max slippage, and keeps 0.05
SOL back for fees. The balance is the wallet's SOL in USD. If a sell fails, it's retried with
wider slippage each time (6%, then 12%, then 24%) so exits get out. Use a private RPC
(`SOLANA_RPC_URL`, e.g. Helius) because the public one rate-limits.

### Honest limitations
- **A −6% stop is not guaranteed on Solana.** There are no native stop orders, so the desk watches
  the price every 10 s and sells with a market swap. A rug or a sharp candle can gap far below the stop.
- The bundle and dev-rug checks are only as good as RugCheck's data. When RugCheck is down,
  everything dies (fail closed), so an outage means an empty night, not unsafe trades.
- Smart wallets means *your* list. Holding the token counts as buying it.
