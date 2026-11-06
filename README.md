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
| **Scout** | Pump.fun (`pumpfun`, `pumpswap`) and Raydium pairs, 30 min–72 h old, mcap $250k–$20M. Each coin is passed to Filter once, plus once more each time a market rule benched it. | DexScreener (profiles, boosts, search) and GeckoTerminal (trending, per-DEX pools) for discovery. DexScreener for every metric. |
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
7. Bundled share over 10%: the most RugCheck shows either in its linked insider networks (wallets funded together or buying in bundles) or in the top holders it flags as insiders. The figure is a **lower bound**: RugCheck gives each insider network's holdings but not its wallet list, so a wallet in two networks is counted twice and the sum can exceed the supply (`MNST`, 2026-11-06, logged 216.8%). It cannot be deduplicated from this data, so it is reported as `>=X%` and said to be a sum. The verdict is unaffected — anything over 10% kills either way.
8. Price already up 80% or more in the last hour.

Rules 3, 4 and 8 are checked first because they need no extra API calls. When a coin fails several
rules, the lowest-numbered one is recorded as the killer and the rest are listed in `detail`.

**On the threshold itself.** R8 is the only rule where the threshold value kills: "already up
80%+" is inclusive (`>= 80`), while "liquidity under $40k", "volume under $50k" and the three
holder-concentration rules all let the exact figure through. That asymmetry follows the wording of
the spec and is deliberate; `tests/test_filter.py` pins all seven numeric thresholds and the
direction of each, because making the comparisons look consistent is a tempting tidy-up that would
silently change which coins die.

**Permanent vs benched.** Rules 1, 2, 5, 6 and 7 are facts about the token: revoked authorities, a
locked LP, who holds the supply, who launched it. Those do not improve, so the coin is dead for good
(`permanent: true` on the kill). Rules 3, 4 and 8 describe the hour: thin liquidity at midnight can
be deep by 3 a.m., and a coin up 90% now is flat later. Those bench the coin for
`rules.FILTER_BENCH_S` (30 min) and the Scout hands it over once more when the bench expires; the
Filter then either benches it again or settles the matter.

**Why the mcap floor sits at $250k.** It was $300k, and the floor was sampled before it moved.
Of the tokens failing the scope test on that floor alone, the median mcap was ~$6k. Over 7 samples
taken 6 minutes apart, only **2 distinct coins** would have cleared the Filter's own liquidity and
volume floors — both just under $300k, both with liquidity ~17% of mcap — and lowering the floor to
$200k or $150k added candidates without adding a single further passer:

| Floor | Extra candidates / scan | Coins clearing R3+R4+R8 |
|---|---|---|
| $250k | 2.3 | 0.43 avg, 1 max, in 3 of 7 samples |
| $200k | 3.4 | identical — same coins |
| $150k | 5.9 | identical — same coins |

So the floor stops at $250k: below it the cost grows and the catch does not. Against a measured
10.3 new in-scope coins per hour, this is about **+32% of flow, not a doubling**. It widens what the
Scout looks *at*; every kill rule and every score weight is unchanged, so nothing that protects the
balance moved. It is not expected to produce trades on its own — with 6 scores and a 56.6 maximum
against a threshold of 70, more flow buys more score attempts, not a pass.

**What each scan actually spends itself on.** A mint only enters `seen` once it has been handed
over, so one that is out of scope is asked about again on every scan, for as long as the feeds keep
listing it. Measured on a live scan (09:23 on 2026-11-06): 153 candidates, 153 of which came back
with a live pair, and **152 out of scope** — one hand-off. Classifying those 132 out-of-scope
tokens by *why* they failed:

| Disqualification | Share | Reversible? |
|---|---|---|
| age over 72 h | 50% (59 plain + 7 also on an unsupported dex) | no — age only grows |
| mcap under $300k | 50% (66) | **yes** — it can grow into the band |

Only age is cached (`Scout.aged_out`). A small mcap can grow into the band, and a `dex_id` belongs
to the pair we happened to pick rather than to the token, so neither is ever written off. A token is
cached only when *every* pair that came back for it is past the ceiling, and a missing
`pairCreatedAt` — which `from_dexscreener` reads as `0`, i.e. an age of ~29 million minutes — is
never treated as old: that mistake would drop a new coin silently and for ever. The set lives in RAM
because it is recomputed from the next answer, so losing it costs one request, whereas persisting a
decision whose input is not persisted is exactly what broke the bench. Worth ~48% of the
~700 DexScreener calls a 4 h run was spending.

**A bench has no attempt counter**, and that is worth stating because the near-miss retry does.
Nothing ends the loop except the coin ageing past `SCOUT_MAX_AGE_H` and dropping out of the
Scout's scope, so a coin first seen at 2 h old can be looked at (72-2)*2 = 140 times. The cap is
therefore a consequence of two unrelated numbers, not a decision — but it is a cheap loop by
construction, because `check()` evaluates the market rules before touching RugCheck or the RPC and
returns early. A coin that keeps failing the same market rule costs no extra calls at all. Over a
4 h paper run: 105 Filter verdicts, of which 8 reached the paid sources, and of the 11 coins
benched four times or more exactly 1 ever paid for a RugCheck report. The bench costs nothing until the coin
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

**The retry and the weak curve are coupled, and one of the two retries granted so far rests on
it.** Both near misses cleared the 55-point floor by very little with `buy_sell_ratio` at full
marks: `COGE` (ES9Fint) by +1.6 with a ratio of 3.52, and `AMD` (oCZHH8) by **+0.9** with a ratio
of **2.042** — two per cent past the point where that curve stops counting. As a sensitivity
check, widening only that band to 1.0→2.5 leaves COGE a near miss (3.52 still saturates) but
drops AMD to 49.8, which is a permanent kill rather than a retry. So the cheapest signal in the
score is what bought AMD its second look. Two coins is not evidence to change a curve on, and
nothing has been changed — but when the bands are revisited this coupling is the reason to look
at the retry floor in the same pass.

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
component with no data scores 0, so the desk never guesses a score upward.

**The maximum is 85 today, not 100.** `smart_wallets.txt` exists in the repo but holds only the
format notes — ten comment lines, no addresses — so `smart_wallets()` yields an empty list and
that component scores 0 on every coin. Checking that the file exists is not enough; it has to
contain addresses you have actually verified. So the threshold is 70 of an effective **85 (82% of
the points that exist)**, and the `start` event logs `smart_wallets` so the live count is on the
record.

**`unique_buyers` is calibrated away from the coins that reach the Analyst.** Measured over the
three scores of the first paper night:

| component | scored | band | observed inputs |
|---|---|---|---|
| `unique_buyers` | 80% of max | 50 → 400 buyers | **185, 1751, 3182** |
| `buy_sell_ratio` | 82% of max | 1.0 → 2.0 | 1.47, 2.89, 3.52 |
| `momentum` | 26% | — | −7.4%, −10.1%, +60.4% |
| `liquidity_to_mcap` | 13% | 0.05 → 0.20 | 0.039, 0.051, **0.108** |
| `holder_growth` | 8% | 0 → 20%/h | 0, 0, 4.7%/h |

A coin only reaches the Analyst by clearing R4 ($50k hourly volume), and at that volume the buyer
count is in the hundreds to thousands — 4 to 8 times past where `unique_buyers` tops out, so it
saturates for structural reasons rather than because the coin is good. `buy_sell_ratio` is weak
for a different reason: it saturates at a ratio of 2.0 and never looks at the price, so it paid
20/20 on an hour in which `RAMBO` fell 10%. `unique_buyers` itself is not a constant — 15.0, 15.0,
5.8 — and does discriminate.

`liquidity_to_mcap` is **not** the mirror of this. Its band is reachable: the 56.6 score sat at
0.108, inside the band, and was paid 5.8/15 for it. Its low aggregate says these coins are thin,
which is the component working, not a band in the wrong place.

With the buyer side delivering ~28 of 35, a pass needs **83% of momentum + liquidity/mcap +
holder growth** — and no single one of those three is worth enough to supply it. Each is capped at
15 or 20 points, so from inside the near-miss band at least two of them have to move a long way at
once. Worked through on `AMD` (oCZHH8, 55.9, needing +14.1): holder growth at its band top gives
67.3, liquidity/mcap at its band top gives 67.4, and momentum maxes at 62.1 — each alone falls
short. Momentum is also the one that improves by the pump *cooling* toward +50%/h, because the
fade runs from there to the R8 kill line: a coin that keeps climbing dies instead of scoring
higher.

That fade opens a **dead zone between the retry floor and R8**. Momentum decays monotonically from
+50%/h to the +80% kill line, losing up to half its 20 points, so a coin sitting in the near-miss
band can be pushed under the 55-point floor by the pump alone. For `AMD` at 55.9 the crossing is
at **+71.48%/h**, 2.8 points above where it sat: from there to +80% it still passes all eight kill
rules and is then buried by the Analyst for being too hot — permanent, budget half spent, and a
different ending from R8 even though both are permanent. Worth separating, because in hindsight
the two read as the same outcome. Three coins is an anecdote, so nothing here has been reweighted — but on this
evidence `unique_buyers`' upper bound and `buy_sell_ratio`'s price blindness are the specific
things to look at, not the 70.

**The buy/sell ratio counts heads, not size, and does not look at the price.** It is unique buyers
over unique sellers in the last hour, and `lin(x, 1.0, 2.0)` saturates at 2.0 — so many small
buyers absorbing a few large sells reads as maximally bullish. Observed: `RAMBO` scored the full
20/20 there (185 buyers, ratio 2.89) during an hour in which the price fell **10%**, which is the
signature of distribution rather than accumulation. The total came out right anyway, 29.3, because
momentum and liquidity/mcap both scored 0 on the same data — two components caught what this one
missed. That is redundancy doing the work, not this curve being correct, and it is worth knowing
before anyone reweights the six. No kill rule catches a falling price either: R8 catches a pump,
and a dump is meant to be priced by `momentum`, which is what happened here.

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

### A missing buyer count is a silent disqualification

`buyers_h1` comes from GeckoTerminal's hourly pool activity, and over the first day of paper it was
**absent in 3 of 13 scores (23%)** — `ROCCO` 42.0, `401kinu` 27.9, `WHITE` 26.6. A component with no
data scores 0, which is the right rule: the desk never guesses a score upward. But the arithmetic
of that zero deserves stating plainly.

| Available | Ceiling |
|---|---|
| everything | 100 |
| no `smart_wallets.txt` | 85 |
| no list **and** no buyer count | **70** |

The threshold is 70, so when both are missing a coin can only pass with a *perfect* score on all
four remaining components at once — momentum 20/20, buy/sell 20/20, holder growth 15/15 and
liquidity/mcap 15/15. That is reachable on paper (it totals exactly 70.0 and passes) and not
reachable in practice: none of the thirteen scores came within 30 points of it.

So for roughly a quarter of the coins that reach the Analyst, the desk is not deciding on the
market — it is sitting out because a third-party field was unavailable, and the log says
`buyers_h1: null` without saying that this made entry unreachable. Two things follow. The 15 points
of an empty wallet list cost more than they appear to, because they are what turns a data gap into a
hard ceiling at the threshold. And a retry spent on a coin whose buyer count was missing is a
window, a RugCheck call and an RPC call spent on a verdict that was decided before the look began.

Nothing is changed here: both the zero and the threshold are deliberate, and 13 scores is not enough
to re-weight anything. It is written down because the shape of the problem — a measurement absent
rather than bad — is the kind that reads as a market verdict in the log.

### Open question: is the near-miss bench worth its cost?

The retry added in `d63da24` has now run four times, and three of the four scores came back worse.
Every near miss of the first day of paper:

| Coin | Benched at | Retry | Change |
|---|---|---|---|
| `AMD` | 55.9 @ 06:15 | 36.5 @ 07:51 | **−19.4** |
| `APPLEPAID` | 61.2 @ 15:26 | 46.7 @ 16:33 | **−14.5** |
| `401kinu` | 55.1 @ 18:16 | 57.2 @ 19:23 | +2.1 |
| `401kinu` | 57.2 @ 19:23 | 27.9 @ 20:30 | **−29.3** |

Mean change **−15.3 points**, and none of the four reached 70. Four retries is not evidence, but it
has a plausible mechanism behind it: `ANALYST_RETRY_AFTER_S` is an hour, and a coin that was worth a
near miss is typically an hour into a move. An hour later it is past the peak more often than it is
consolidating — every one of these was benched with a 1 h change between +55% and +79%, and
`momentum` was the component that collapsed in all three losing cases, twice to exactly 0.

If that holds, the retry's expected value is **negative**: it spends up to two further 300 s
observation windows plus RugCheck and the RPC to look at coins that are, on average, 15 points worse
than when they were set aside. The counter-argument is `401kinu`'s +2.1 and the fact that none of
these entered, so the budget never cost a bad trade — only the calls.

What would settle it: twenty-odd retries, and whether any retry has ever crossed 70. Until then the
wait is left at an hour, because shortening it to catch the coin mid-move argues for the opposite of
what `FILTER_BENCH_S` argues, and both cannot be tuned on four observations.

### The first trade the desk nearly took: APPLEPAID, 2026-11-06

`APPLEPAID` (`APgbBfggjo79T7PdVqTQcEbVdQWBCif1kLTwjRwrxc8Z`) scored **61.2** at 15:26 — the highest
of the first 16 hours of paper, against the 70 needed — and was benched as a near miss. Its mcap was
$276,796, so it only reached the Analyst at all because the Scout's floor had been lowered to $250k
earlier the same day. An hour later, on the retry, it scored **46.7** and died for good.

What the two looks show, side by side:

| Component | 15:26 | 16:33 |
|---|---|---|
| momentum | 10.2 | **0.0** (1 h change +79.4% → −24.4%) |
| unique_buyers | 15.0 | 15.0 (1,006 → 1,076 buyers) |
| buy_sell_ratio | 8.6 | 4.1 |
| holder_growth | 15.0 | 12.6 (+39.5%/h → +16.9%/h) |
| liquidity_to_mcap | 12.4 | **15.0** |
| smart_wallets | 0.0 | 0.0 |
| **total** | **61.2** | **46.7** |

**`liquidity_to_mcap` rose to full marks because the coin collapsed.** Mcap fell from $294,753 to
$225,138 while liquidity only fell from $50,771 to $45,065, so the ratio reached exactly 0.20, the
band ceiling. That is mechanically right — the pool *is* proportionally deeper — but it means a coin
losing a quarter of its value earns points there. Second time in one day: LIA was paid a full 15/15
on a pool that was emptied eight minutes later.

**And it would have been a losing trade.** Simulated with the real constants — 5% of balance, the
paper fill's 1.5% slippage and 0.3% fee, `RISK_STOP_PCT` −30%, `RISK_TRAIL_ACTIVATE_PCT` +60% —
against the pool's minute candles from the entry:

```
entry 15:26   $50.00   fill 0.0003036   stop 0.0002125   trail arms at 0.0004857
peak          0.0003758  = +23.8% from fill   -> trail never armed
low           0.0002112  = -30.4%             -> stop breached by 0.6%
exit on stop  0.0002125  -> $34.18 back       -> -$15.82, -31.6% of the position
```

Three things worth keeping from it:

1. **Nothing protects a gain below +60%.** The position went to +23.8% and came all the way back
   through it with the stop still sitting where entry had put it. Risking 30% while locking in
   nothing until 60% is a deliberate choice in `rules.py`, but it had never been looked at against a
   real path before this one.
2. **The stop's exact level was decided by noise.** The low undercut the stop by 0.6%; a stop at
   −31% would not have fired, and an hour later that position would have been at −23.1% rather than
   −31.6% realised. On a coin that did +23.8%, −30.4% and −22.8% inside ninety minutes, one trade
   teaches nothing about where the stop belongs.
3. **The empty `smart_wallets.txt` is what kept the desk out.** With two wallets from a list holding
   the coin, 61.2 becomes 76.2 and the Executor enters. The 15 points documented elsewhere in this
   file as the cost of having no list would have bought a $15.82 loss here. That is one observation,
   not a principle — a desk that never enters never earns — but it is the reason the list has to be
   verified forward before it is switched on, rather than filled because 15 points are missing.

### What a refusal is worth: LIA, 2026-11-06

The desk had not opened a trade in twelve hours when it looked at `LIA`
(`VLxDCjbbJ853sqtmiXfpWuYMRvDTRrYWf2GuS4GZjNv`) at 12:27 and killed it on **R2** — LP locked or
burned 0.0% against the 95% required — listing R5 (top 10 at 58.1%) and R6 (dev at 32.73%) behind it.

What the pair looked like: mcap $251,811, liquidity $88,825, and **$7,939,040 of 1 h volume** — 31x
the market cap in an hour — up 58% on the hour. Reading the same RugCheck fields eight minutes later
through the same code path put the creator at **81.39%** of supply, not 32.73%: the volume was in
good part the dev reaccumulating. Of 733 holders, the top 12 held 94.32%, and the eleven wallets
below the creator held 1.16–1.20% each — near-identical slices, one actor across many addresses,
which is what RugCheck's insider networks also report (1,973 wallets at 17.73%, plus clusters of 4
to 10).

Eight minutes after the refusal the pool was empty: **liquidity $0**, zero transactions in the
trailing 5 minutes, against 3,060 buys in the preceding hour. The quoted price (+174%) is a price in
a pool nobody can sell into.

Those buys were not 3,060 buyers. GeckoTerminal's distinct counts for the same hour: **23 buyers and
22 sellers** behind 2,686 buys and 2,384 sells — about 117 trades per wallet, the same couple of
dozen addresses on both sides. The top 20 holders held **100.00%** of supply; the other 713 holder
accounts held 0.0009% between them, and RugCheck linked **2,002 addresses**. So the $7.9M of volume
and the +8,311% on the day were manufactured inside a closed loop. Almost nobody independent was
rugged because almost nobody independent was ever in.

Two further things the scoring would have said, had the coin reached the Analyst. It would have
scored **33.2**, and the buyer side — `unique_buyers` plus `buy_sell_ratio`, the 35 points measured
elsewhere in this file as saturating and generous — would have paid **0.9 of 35**: 23 buyers is 0/15
against a band starting at 50, and a 1.045 ratio is 0.9/20. Those bands are one-sided rather than
useless: they cannot separate 185 buyers from 3,182, but they separate 23 from 185 decisively. And
`liquidity_to_mcap` would have paid a full **15/15** on the pool that was about to be withdrawn,
which is the clearest statement of why R2 is a separate question: how deep the pool is and whether it
can be taken away are not the same measurement.

Two things follow, and they are why this is written down rather than told as a story:

1. **R2 named the mechanism, not a proxy for it.** The coin was refused precisely because the LP was
   free to be withdrawn, and withdrawing it is what happened. A generic "looks risky" filter would
   not have earned this.
2. **The stop would not have saved the position.** At 5% of balance the entry would have been ~$50,
   and a −30% stop cannot execute into zero liquidity. This is the first limitation below, observed
   rather than argued: the protection that mattered was refusing to enter, not the exit.

An empty night is the cost of this; LIA is what it buys.

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
