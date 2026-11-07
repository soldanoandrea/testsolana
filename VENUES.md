# Venues: how much of this works on a CEX?

Asked on 2026-11-07, alongside the proposal to build providers so the product does not depend on
one exchange. The provider seam is the right instinct and it is cheap. What it would carry across
is the half of the desk that is *not* the edge, and that asymmetry decides what to build and in
which order.

## The eight kill rules, one at a time

| rule | what it reads | on a CEX |
|---|---|---|
| R1 mint / freeze authority | the SPL mint account | **readable**, but from the chain, not the exchange — and it no longer protects the position: the exchange custodies the tokens |
| R2 LP burned or locked ≥ 95% | the pool's LP supply | **does not exist**. There is no LP. The book belongs to the exchange and its market makers |
| R3 liquidity ≥ $40k | pool reserves | **portable** with a different definition: depth within a band of mid |
| R4 1h volume ≥ $50k | pair volume | **portable, and better** — no wash prints between two wallets of the same hand |
| R5 top 10 holders ≤ 25% | the holder table | **invisible**. Everything the exchange custodies sits in one omnibus wallet. The rule cannot be written, not even badly |
| R6 dev ≤ 3%, no past rugs | `creator`, `creatorTokens` | **readable from the chain** while the token is also on chain |
| R7 bundled / insider ≤ 10% | RugCheck's insider graph | **half**: the on-chain cluster is visible, accounts inside the exchange are not |
| R8 not already +80% in 1h | price | **portable** |

Three rules port cleanly (3, 4, 8) and those are exactly `FILTER_TRANSIENT_RULES` — the market
conditions that bench a coin rather than kill it. Three more (1, 6, 7) port only because the asset
is also on chain, so they are chain rules that happen to be checked for a CEX position. Two (2, 5)
have no meaning on an exchange at all, and they are the two that describe the specific way the
thing this desk hunts dies.

The actor memory goes the same way. A wallet is an identity, which is what makes
[the creator graph](README.md#actor-memory-who-was-behind-the-coin) possible — one wallet behind
six coins the desk had judged separately. Inside an exchange there are no wallet identities to
join on.

## The score cannot pass on CEX data

| part | points | on a CEX |
|---|---|---|
| momentum | 20 | portable |
| buy / sell ratio | 20 | portable, from real taker flow |
| liquidity / mcap | 15 | portable as depth / mcap |
| unique buyers | 15 | **no**: no exchange API counts distinct accounts |
| holder growth | 15 | **no**: no holders to count |
| smart wallets | 15 | **no**: no wallets to recognise |

That leaves 55 points reachable against `ANALYST_MIN_SCORE = 70`. Not a tuning problem: a CEX
version of the Analyst is a different scoring function, not the same one with sources swapped.

## Where a CEX is strictly better

**Fees, by an order of magnitude.** The turnover table in [PORTFOLIO.md](PORTFOLIO.md) was computed
at 1.8% per side (0.3% LP fee + 1.5% slippage, the paper fill model). Same arithmetic, same 20%
of capital turned over per day, exchange costs instead:

| cost per side | drag per day | cost of a year of daily rebalancing |
|---|---|---|
| 1.8% (on-chain, today's fill model) | 0.360% | **73% of capital** |
| 0.30% (thin CEX book: fee + spread) | 0.060% | 20% |
| 0.10% (major, taker) | 0.020% | **7.0%** |
| 0.05% (major, maker) | 0.010% | 3.6% |

This is the one place where the rebalancing argument changes answer. On-chain, continuous
rebalancing cannot pay for itself; at 0.10% per side the drag stops being the thing that decides.
The arithmetic that killed the proposal was about the venue, not about the idea.

**The stop becomes real.** Today the stop lives in the desk's own loop — the README limitation
"the stop only runs while the desk runs" is the honest version, and a morning recap has already
had to print it. An exchange holds a stop order whether the process is up or not. That is a
genuine reduction in risk, not a cost saving.

**No sandwich, no failed swap, no priority fee.** And in exchange, custody risk: the balance is
the exchange's promise rather than a key we hold.

## The asset is not the same asset

`SCOUT_MIN_AGE_MIN = 30`, `SCOUT_MAX_AGE_H = 72`, mcap $250k–$20M. Nothing in that band is listed
on Binance or Bybit; by the time it is listed, it is past the window the Scout is built for. MEXC,
Gate and Bitget do list early and small, which is why they are the venues where a CEX port would
even be about the same kind of coin — and they are also where fees and depth are worst, so the
0.10% line above is not the one that would apply.

## What the provider seam has to get right

The desk's first principle is `missing data = kill`: fail closed, because we cannot prove a coin is
safe. That is correct for the on-chain case, where the fact exists and a source would not answer —
which is exactly the bug just fixed in [desk/holders.py](desk/holders.py): R5 was killing coins for
RugCheck's intermittency rather than for concentration, and the fix was a second provider plus a
table that says who answered.

A venue abstraction meets the other case, and the two must not be conflated:

- **unavailable** — the fact exists, nobody answered. Kill. (`HolderTable.present == False`.)
- **inapplicable** — the fact does not exist on this venue. There is nothing to kill for.

A venue that declares "I cannot answer R2 and R5" must not quietly run with six rules instead of
eight: that is how a gate built for rug risk ends up trading an asset class it was never shaped
for, with its two strongest rules silently absent. So the seam is not just a data interface:

```
Venue -> declares which facts it can supply
Rule  -> declares which facts it needs
Gate  -> a venue missing a rule's facts must supply a REPLACEMENT rule, or the desk
         refuses to trade that venue at all
```

The replacement for R5 on an exchange is not a worse version of R5; it is a different question
(unlock schedule, float, market-maker agreement, listing age) with its own thresholds in
`rules.py`, where git history can see them.

## What I would build, in order

1. **Nothing yet, for the memecoin leg.** On MEXC/Gate the asset is similar and the edge is gone:
   no holder table, no creator graph, no LP lock, and the score cannot reach 70. A provider
   interface built now would be filled with facts no venue supplies.
2. **A CEX for the core sleeve, where it has a job.** [PORTFOLIO.md](PORTFOLIO.md) § A is a band-only
   BTC/ETH sleeve, and that sleeve is all venue and no edge: it wants the cheapest fills, a stop the
   venue holds, and nothing else this repo knows. One venue, one asset pair, read-only first. That
   is a real use for the seam and it is small.
3. **Then the seam, shaped by two live venues** instead of by a guess about them. One venue is a
   config value; two is an interface, and by then the capability/replacement split above has
   something concrete to be right about.
