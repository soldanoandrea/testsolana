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
| **Scout** | Pump.fun (`pumpfun`, `pumpswap`) and Raydium pairs, 30 min–72 h old, mcap $300k–$20M. Each coin is passed to Filter once, plus once more each time a market rule benched it. | DexScreener (profiles, boosts, search) and GeckoTerminal (trending, per-DEX pools) for discovery. DexScreener for every metric. |
| **Filter** | 8 kill rules (below). One hit is final for this look. A safety rule (1, 2, 5, 6, 7) kills the coin **for good**; a market rule (3, 4, 8) **benches** it for 30 min and it gets another look. **Missing data also kills**: if a rule can't be verified, the coin fails it. | RugCheck report, cross-checked against Solana RPC `getAccountInfo` for the authorities |
| **Analyst** | Watches holder count for `DESK_HOLDER_WINDOW_S` (300 s), sends the coin back through Filter on fresh data, then scores it 0–100. 70+ goes to Executor, under 70 dies — unless it is a **near miss**, which earns up to 2 more looks an hour apart (below). | DexScreener, GeckoTerminal (unique buyers/sellers in the last hour), RugCheck holders, RPC (smart wallets) |
| **Executor** | 5% of the current balance per trade, **at most 6 positions open at once**. One entry per coin, ever. Never averages down. Asks Risk for the stop before it orders. Refuses every entry while the desk is halted. Writes the order to `state.json` **before** placing it, so a crash between a confirmed swap and the record cannot leave an unwatched bag in the wallet. | Paper fills or Jupiter swaps |
| **Risk** | Stop committed before the order goes in, then placed at exactly −30% from the **real fill** (paper and live land on the same number; the slippage setting cannot reach it). Stops can only move up: lowering one is blocked and logged as a violation, including a stop read back from `state.json`. At +60% from entry it switches to a trailing stop 20% under the peak, which locks in at least +25%. Exits only when the stop or the trail is hit. Checks every 10 s, and falls back to Jupiter for any coin DexScreener does not price. | DexScreener prices, Jupiter as fallback |
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

**Permanent vs benched.** Rules 1, 2, 5, 6 and 7 are facts about the token: revoked authorities, a
locked LP, who holds the supply, who launched it. Those do not improve, so the coin is dead for good
(`permanent: true` on the kill). Rules 3, 4 and 8 describe the hour: thin liquidity at midnight can
be deep by 3 a.m., and a coin up 90% now is flat later. Those bench the coin for
`rules.FILTER_BENCH_S` (30 min) and the Scout hands it over once more when the bench expires; the
Filter then either benches it again or settles the matter. The bench costs nothing until the coin
passes those rules, because they are the ones that need no extra API calls.

This matters more than it sounds. In a real overnight paper run, 26 of 32 scans handed **zero**
coins to the Filter because everything had already been buried, and 15 of 16 kills were market
rules — conditions that had since changed.

### Position sizing and the daily kill switch

A memecoin at $1–5M mcap moves 6% on noise alone, so a 6% stop is a coin flip charged 3.6%
round-trip friction — it stops you out before the move you bought can happen. The stop is
therefore wide (−30%) and the size small (5%), so the arithmetic per trade is:

| | per trade | of the balance |
|---|---|---|
| stop-out (incl. slippage + fees) | −31.4% | **−1.57%** |
| smallest trailing win (+60% peak, 20% trail) | +25.6% | +1.28% |
| → break-even win rate | **55%** | |
| peak +100% → exit | +57% | |
| peak +300% → exit | +214% | |

Two caps sit on top, both constants in `rules.py`:
- **6 open positions** at once. Worst case — every one stops out together — is 9% of the balance,
  checked by an `assert` in `rules.py` against the daily limit below.
- **−15% in a local day** and the desk stops opening positions until the next day
  (`day_open`/`halt`/`resume` in the log, and a line in the recap). Open positions are deliberately
  *left alone*: their stops keep running. Market-selling everything into a falling market would
  realise the worst price of the day, which is the opposite of protecting the balance.

### Near misses get another look

Five of the six components — momentum, unique buyers, buy/sell ratio, holder growth,
liquidity/mcap — are **conditions of the hour**, 85 of the 100 points. Only `smart_wallets` is
structural: it depends on your own list, and with no list it is a fixed 0. Burying a coin for good
on the other 85 contradicts the Filter, where R3 (thin liquidity — the same quantity
`liquidity_to_mcap` measures as a ratio) only benches the coin for 30 minutes.

So a score within `ANALYST_RETRY_WITHIN` (15 points) of the threshold benches the coin instead of
killing it: at most `ANALYST_MAX_RETRIES` (2) extra looks, each after `ANALYST_RETRY_AFTER_S`
(1 hour). Anything further below still dies on the spot, and a spent budget is permanent.

The retry is deliberately much stricter than the Filter's bench, because the looks cost very
different amounts: rules 3, 4 and 8 need no extra API calls, while an Analyst look costs a 300 s
observation window plus RugCheck and RPC on the re-check. Re-observing every reject would starve
the discovery of new coins. A benched coin always returns through **Scout → Filter**, so all 8 kill
rules are replayed on fresh data — it can never come back straight to the Analyst.

**How a benched coin can end without a verdict.** The bench is spent the moment the Scout has
fresh data for it, so a coin that no longer has an in-scope pair leaves the pipeline right there:
it is never handed over again, and no agent ever rules on it. That is the intended behaviour — it
stops a delisted coin costing calls for ever — but it used to happen silently, which made a
near miss look like it had simply evaporated halfway through its budget. The Scout now logs
`bench_expired` naming the mint, the recap counts them (and says how many were near misses), and
the desk hands the retry budget back, since a coin that cannot reach the Analyst has no use for
one. The same sweep covers the other ways a returning coin stops being eligible: the Filter
burying it on the re-check, or its pair vanishing. A coin still in flight keeps its budget, or a
near miss would buy a fresh look every tick.

**The bench holds on its own.** `benched` is persisted, but for a long time the thing that
actually kept a benched coin out of the next scan was the Scout's `seen` set, which is not: it is
rebuilt from `dead | traded` at startup. Within a session the two agreed, so this only bit after a
restart, where a benched coin looked like a coin never seen, came straight back with its wait
ignored, and came back again when it fell due. On the paper desk's own state that was 14 of 18
scouted coins returning to the Filter instead of 4. `scan()` now excludes everything in `benched`
outright and lets `| due` be the single way back in, so the wait no longer depends on a set that
does not survive a restart.

A restart is the one case where "in flight" cannot be read off the disk, because neither the
Analyst's window nor the Scout's `seen` set is persisted. A budget with no bench behind it is
therefore a coin that was mid-pipeline when the desk went down — and since `seen` is gone too, it
will be discovered again. Dropping the budget there would refund the attempts it had already
spent and hand it a whole fresh one, so the desk re-benches it instead (`retry_rebenched`): the
attempts stand, the hour's cooldown applies as it would have, and the coin returns through
Scout → Filter like any other. If nothing in scope comes back for it, the Scout retires it and the
sweep above clears the budget — so the re-bench converges rather than becoming a new way for an
entry to live for ever.

Observed case: `COGE` scored **57.9** (momentum 13.3/20, buyers 15/15, buy/sell 13.8/20, holder
growth 15/15, liquidity/mcap **0.7**/15, smart wallets 0/15) and was buried for good. An hour later
it was **+39%**, with liquidity/mcap still 0.051 against the 0.057 it was marked down for. One coin
is an anecdote, not evidence — the argument is structural: that component measures the depth of the
exit, not the return, and it was issuing a permanent verdict on an hourly number.

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
- the paper log has at least 20 closed trades (`rules.LIVE_MIN_CLEAN_PAPER_TRADES`)
- those trades are **net profitable** — counting rows is not proof, and twenty losing trades used
  to open live exactly like twenty winning ones
- the daily loss limit **never fired** in paper
- the paper log has **zero** rule violations
- `DESK_CONFIRM_LIVE=yes` is set
- `SOLANA_PRIVATE_KEY` is set (base58, for a dedicated hot wallet holding only what you can afford to lose), and `pip install solders` has been run

The thresholds are constants in `rules.py`, not settings. They used to live in `config.py`, which
meant `DESK_LIVE_MIN_PAPER_TRADES=0` opened live mode with no paper record at all. When the gate
passes it logs `live_gate_passed` with the paper stats it accepted.

Live mode buys and sells through Jupiter (`lite-api.jup.ag`) with 3% max slippage, and keeps 0.05
SOL back for fees. The balance is the wallet's SOL in USD. If a sell fails, it's retried with
wider slippage (6%, then 12%, then 24%, capped there) so exits get out, and the escalation is kept
on the position so the next tick carries on widening instead of restarting at 3%. Use a private RPC
(`SOLANA_RPC_URL`, e.g. Helius) because the public one rate-limits.

### Honest limitations
- **A −30% stop is not guaranteed on Solana.** There are no native stop orders, so the desk watches
  the price every 10 s and sells with a market swap. A rug or a sharp candle can gap far below the stop.
- **The stop lives in this process, not on-chain.** The monitor reads DexScreener and falls back to
  Jupiter, so one source going down no longer silences it — but if *neither* answers, the tick is
  logged as `unprotected` and the stop simply is not checked. The desk never guesses a price and
  never sells blind to escape the uncertainty. Recap counts those ticks.
- **A crashed entry is reconciled, not guessed.** On startup, and whenever a buy raises, the desk
  compares its pending-order record against what the wallet actually holds: tokens held with no
  position recorded are adopted with a fresh stop (`recovered` in the log and the recap), an order
  that never landed burns the coin so it cannot be entered twice, and a wallet it cannot read keeps
  the record for the next attempt. "Not confirmed" is never read as "did not happen".
- **Stopping the desk removes the stop.** Open positions keep no stop at all once the process exits.
  Shutdown logs them as `unprotected` and the recap names them with their stop price, so you know
  what you are holding unwatched.
- **The numbers above are a prior, not a fit.** Nothing has been backtested: the paper log is what
  will say whether 70+ scores actually reach +60% often enough to pay for the stop-outs.
- The bundle and dev-rug checks are only as good as RugCheck's data. When RugCheck is down,
  everything dies (fail closed), so an outage means an empty night, not unsafe trades.
- Smart wallets means *your* list. Holding the token counts as buying it.
