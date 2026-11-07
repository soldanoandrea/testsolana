# Portfolio layer: what rebalancing can and cannot buy

A design note, not a change. It answers one proposal: stop trading direction, hold a portfolio that
rebalances continuously on the signals and actor data the desk already collects. Three parts —
what the proposal gets right, where its arithmetic runs the other way, and what survives of it that
is worth building.

## The record this argues against

Two days of paper, two rule sets:

| | |
|---|---|
| scans | 797 |
| coins handed to Filter | 564 times, 55 distinct symbols |
| Filter kills | 548 |
| coins that reached the Analyst | 16 scores |
| highest score ever reached | **61.2** (`APPLEPAID`) against a threshold of 70 |
| trades opened | **0** |

So "we have found some edge over time" needs splitting in two. The desk has found nothing about
price: it has never taken a position, and the score has never once crossed its own threshold. What
it *has* found is structural — `LIA`'s 3,060 buys being 23 wallets, a creator at 81.39% of supply,
top-20 at 100.00%, 2,002 linked addresses, a pool at $0 eight minutes after the refusal. That is a
real and unusual measurement, and it is not a price forecast. It is a forecast about *actors*.

Keeping those two apart matters, because the proposal treats the second as evidence for the first.

## Where the proposal is right

1. **The directional thesis is unmeasured, and the binding constraint is not fees.** The score's
   effective ceiling is 85 (`smart_wallets.txt` is empty), and 70 of 85 with a quarter of coins
   missing `buyers_h1` — which caps them at 70 — means entries are close to structurally
   unreachable. Zero trades in two days is a *gate* problem, not a fee problem. Fees have never
   been paid, so they cannot yet be what is losing.
2. **The actor data is the asset, and the desk throws it away.** `filter.py` reads `creator`,
   `creatorBalance`, `insiderNetworks` and the insider-flagged top holders, collapses all of it into
   one `bundle_pct` float plus a note string, and emits a binary kill. `state.json` persists
   `dead`, `benched`, `traded` — mints, nothing about who was behind them. Every wallet cluster the
   desk has ever identified is gone except as prose in `events.jsonl`. "The actors and their
   relations" is exactly the right instinct, and right now there is no structure holding them.
3. **"Liquidity can only be drained, it is only timing" is a better-posed problem than price.**
   For an asset whose terminal value is zero, the quantity to estimate is not return but
   **survival time**, and survival time is a hazard model over actor structure — the one input the
   desk measures well. This is the strongest idea in the proposal and it is the one the rest of
   this note builds on.

## Where the arithmetic runs the other way

**Rebalancing is the most fee-intensive policy that exists.** Fees are charged per trade, and
continuous rebalancing maximises trade count by construction. It cannot be the answer to a fee
problem; it is a multiplier on one. Pure turnover drag, no alpha assumed, at the desk's own
pessimistic fill model (1.5% slippage + 0.3% fee = 1.8% per side) and at a realistic cost for deep
majors (~0.15% per side):

| turnover | memecoin leg (1.8%/side) | majors leg (0.15%/side) |
|---|---|---|
| 20% / day | 0.360%/day → **73% of capital per year** | 0.030%/day → 10.4%/year |
| 20% / week | 0.051%/day → 17.1%/year | 0.004%/day → 1.6%/year |
| 20% / month | 0.012%/day → 4.3%/year | 0.001%/day → 0.4%/year |

The frequency in "si bilancia in continuo" is worth 73% a year on the memecoin leg before anything
else happens. On the majors leg the same policy is survivable — which is the first sign that these
are two different sleeves and not one portfolio.

**Constant-mix is a short-momentum bet, and both stated premises are the opposite of that.**
Rebalancing to fixed weights sells what rose and buys what fell. It is paid when assets oscillate
around a stable relative level, and it pays *out* when they trend or when they die. The two
premises offered are:

- BTC and ETH tend to rise → a rebalancer **trims the winner**, continuously. Buy-and-hold beats
  constant-mix in a trend; that is not an opinion, it is what the policy does.
- other coins only get drained → a rebalancer **tops the loser up**, continuously, all the way to
  zero. This is averaging down into a rug, which `rules.py` already forbids by name ("never
  averages down") for good reason.

Put together, a portfolio that continuously rebalances between an asset you believe always rises and
assets you believe always go to zero is the single configuration where rebalancing is guaranteed to
lose: it is a pump that moves capital from the thing that works into the thing that is being
drained, and charges 1.8% a side to do it. A toy simulation of exactly that — majors sleeve with
positive drift, satellite sleeve with negative drift and an absorbing barrier, satellites replaced
on death — lands where the structure says it must: daily and band rebalancing both lose in 100% of
400 runs, against 72% for holding. The parameters are invented, so the simulation is an
illustration; the direction is not parameter-dependent.

**And "BTC always grows" is not a portfolio strategy.** It is a directional long on a long horizon.
If it is believed, the correct implementation is to hold it — which needs no agents, no RugCheck,
no insider graph, and no desk. Nothing in this repository is required to hold BTC, so it should not
be the reason the repository changes shape.

## What survives, as three separate mechanisms

The proposal's good parts are not one portfolio. They are three mechanisms with different clocks,
and merging them is what produced the contradiction above.

### A. Core sleeve — slow, one-way, band-only

Majors as the resting place for capital that is not in a satellite position, instead of idle cash.
Rebalanced **only** on wide drift bands (±25% of target weight), and preferentially when capital is
already moving for another reason — a satellite entry or exit. Target turnover is monthly, which
from the table above is ~0.4%/year of drag. No intelligence, no agent, no signals. This sleeve
exists to not be a decision.

### B. Satellite sleeve — hazard, not price, and strictly one-way

Enter once, never add. The new primitive is an **estimated time-to-drain** per coin, from actor
structure. It replaces three things that are fixed constants today:

- **Size** scales inversely with hazard. Today every position is 5% of balance regardless of what
  the Filter saw. A coin that passed every rule but shows two linked insider clusters is not the
  same bet as one that shows none, and the desk currently sizes them identically.
- **A time stop**, which the desk does not have at all. `Risk.update()` returns only `stop_loss`
  and `trailing_stop`: a position exits on a −30% break or a trail from +60%, and in no other way.
  A coin that goes sideways for two days while the creator distributes into it has **no exit**.
  For an asset whose terminal value is zero, a maximum holding period is not a refinement — it is
  the exit that matches the thesis. This is the cheapest and highest-value change on this list.
- **Structural exit**, independent of price. Risk watches price and nothing else. The events that
  actually end these coins — the creator's wallet moving, an LP unlock falling due, an insider
  cluster beginning to sell — are visible in sources the desk already calls, and currently cannot
  trigger an exit. `LIA`'s lesson was that refusing to enter was the protection that mattered;
  the same data should be able to force an exit, not just a refusal.

Note what B is: still directional. A one-way sleeve with hazard-scaled size and a time stop is a
directional bet on survival rather than on price. It is not rebalancing, and it should not be
dressed as rebalancing.

### C. Allocator — risk budget across sleeves, not weights across tokens

This is what "a portfolio that rebalances continuously" should mean here. It moves the **risk
budget** between the core sleeve and the satellite sleeve, and between signal families inside the
satellite sleeve, on measured results: the satellite budget grows only as its own realised hit rate
and PnL justify it, and shrinks on drawdown. The daily −15% kill switch is the crude version of this
already in `rules.py`; the allocator is its continuous form.

The allocator pays almost no fees, because it does not trade to express itself. It changes the size
of flows that were going to happen anyway. That is the only kind of continuous rebalancing that is
affordable at 1.8% a side, and it is a flow-control layer, not an alpha source — it cannot create
edge, only stop capital compounding into the part of the desk that is losing.

## The two gaps everything above depends on

Both measured in the current tree, both small:

1. **No time stop.** `desk/agents/risk.py` exits on stop or trail only. There is no
   `RISK_MAX_HOLD_*` constant.
2. **No actor memory.** The insider graph is read, reduced to a float, and dropped. Nothing
   survives a coin's kill, so the same creator launching again next week is met by a desk with no
   recollection of the last one.

Gap 2 is the one that compounds, and it is nearly free: the data is already fetched and paid for.
Persisting it turns 548 kills from a count into a labelled dataset — actor structures whose
outcomes can be fetched after the fact. That is the honest route to "what the maths does not tell us
yet": not a new model on the same six components, but a new column.

## Sequencing

1. **Persist the actor graph** (creator, funding sets, insider clusters, per-coin, cross-coin).
   Cheap, uses data already bought, compounds from the first write.
2. **Time stop and structural exit in Risk.** The exit side is where "it can only be drained"
   actually cashes out, and it is where the desk is currently blind.
3. **Backfill outcomes** for the 548 kills and 16 scores. First real measurement of whether any
   observed structure predicts survival time. Until this exists, hazard-scaled sizing is a guess.
4. **The allocator, last.** It has nothing to allocate today: zero trades, zero realised PnL, no
   measured hit rate for any sleeve. Written before step 3 it would be a weighting scheme over
   numbers that do not exist.

Step 4 is the part of the proposal that is genuinely new, and it is last because it is the only one
that needs the other three to mean anything.
