"""Who holds the token, from whichever provider can answer.

R5 (top-10 concentration) used to read RugCheck's `topHolders` and nothing else. That field is
absent intermittently, server-side, on live and dead tokens alike: PUMP -- $22M liquidity,
1,341,045 holders -- returned 20 holders and then `None` twenty-five minutes later, `None` on six
consecutive probes through two different HTTP clients with `totalHolders` unchanged. When it is
absent `top10_pct` is None, R5 reads "holders unverifiable" and the Filter kills the coin. That is
the right default and the wrong reason: the kill is about a third party's uptime, not about the
coin. Of the 75 actor records on file, 66 have no holder table.

So the table gets its own module, with providers tried in order and the answer saying which one
spoke. Two exist now:

- `RpcHolders` -- the chain itself. Ground truth, and it also answers `R5` for coins RugCheck has
  no opinion about. It needs an endpoint that keeps the spl-token secondary index (see the class).
- `RugCheckHolders` -- the field the desk reads today. No extra call: the report is already paid
  for. Kept as the fallback, because on a keyless endpoint it is the only one that answers.

A third (Helius DAS, Solscan, Birdeye) is a class with one method; nothing else changes.

### What counts as a holder

A pool vault holds tokens, but it is not somebody's position, and counting it would make every
pumpswap coin look concentrated. RugCheck labels some vaults in `knownAccounts` (`AMM`, `LOCKER`)
and the Filter has always excluded those. The label list is incomplete: on `Alice`, an account
holding 1.10% carried no label and turned out to be owned by the Pump.fun bonding-curve program
`6EF8rrecth…`.

Two exclusions replace the list, and neither needs it to be complete:

1. **the coin's own markets**, read out of `markets[]` in the same report -- each pool's `pubkey`,
   its two `liquidity*` token accounts, their authority and the LP mint. Derived per coin, so it
   covers a DEX nobody here has heard of. On `Alice` this catches the PumpSwap pool (12.83%) and
   the Meteora DLMM pool (2.77%) without touching the label list.
2. **owned by a program** -- an account the System Program does not own cannot be signed for by a
   person; its tokens move by that program's rules. One `getMultipleAccounts`. On `Alice` this is
   what catches the unlabelled bonding-curve account.

### The test that was tried and rejected

A program-derived address is off the ed25519 curve by construction, so "off the curve" looks like
a complete test for "not a person" with no list and no call at all. It reproduced RugCheck's
labels exactly on the 9 coins on file -- all 7 `AMM`/`LOCKER` accounts off the curve, all 94
unlabelled owners on it -- and then it was measured against `PUMP`, where **11 of the 20 top
holders are off-curve addresses the System Program owns**, together about 58% of supply: escrowed
or vesting allocations, not pools. Excluding those would have cut `top10_pct` to 12.11% and made
the most concentrated token the desk has looked at read as the safest. An escrow can release and
sell, which is exactly what R5 exists to measure, so it is counted.

`is_program_derived` therefore stays, but only as information -- the actor store uses it to avoid
indexing an address nobody holds a key for as an actor -- and never as a reason to leave a holder
out of a concentration rule. The direction matters: leaving a holder out can only let a coin past
R5, counting one that was really a vault can only kill a coin that was fine.

Measured on the 9 records with a holder table, the exclusions above change no `top10_pct` by more
than 1.1 points and flip no verdict, including `WEALTH`, killed on R5 at 25.29% against 25%.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from .http import DataUnavailable

SYSTEM_PROGRAM = "11111111111111111111111111111111"
NOT_A_WALLET = frozenset({"AMM", "LOCKER"})   # RugCheck labels for accounts that belong to the market


def market_accounts(report: dict, pair_address: str | None = None) -> set[str]:
    """Every address in the report that belongs to a pool rather than to a person.

    Read out of `markets[]`: for each market the token trades in, the pool account, its two
    liquidity token accounts, the authority those are owned by, and the LP mint. Per coin and from
    the report the Filter has already paid for, so it needs no list of DEX programs.
    """
    out = {a for a in (pair_address,) if a}
    for m in report.get("markets") or []:
        for k in ("pubkey", "liquidityA", "liquidityB", "mintLP"):
            if m.get(k):
                out.add(m[k])
        for k in ("liquidityAAccount", "liquidityBAccount"):
            owner = (m.get(k) or {}).get("owner")
            if owner:
                out.add(owner)
    out.discard(SYSTEM_PROGRAM)       # meteoraDlmm reports no LP mint and fills the field with it
    return out

# ---- ed25519, enough of it to tell a wallet from a program address ------------------
_P = 2**255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58decode(s: str) -> bytes:
    n = 0
    for c in s:
        i = _B58.find(c)
        if i < 0:
            raise ValueError(f"not base58: {s!r}")
        n = n * 58 + i
    body = n.to_bytes((n.bit_length() + 7) // 8 or 1, "big")
    return b"\0" * (len(s) - len(s.lstrip("1"))) + body


def is_wallet_address(addr: str) -> bool:
    """True if `addr` is a point on the ed25519 curve, i.e. an address somebody can hold a key for.

    A PDA is found by hashing until the result is NOT a valid point, so every program-derived
    address -- pool vault authority, bonding curve, escrow -- fails this.
    """
    try:
        b = b58decode(addr)
    except (ValueError, AttributeError, TypeError):
        return False
    if len(b) != 32:
        return False
    y = int.from_bytes(b, "little") & ((1 << 255) - 1)
    if y >= _P:
        return False
    v = (_D * y * y + 1) % _P
    if v == 0:
        return False
    x2 = (y * y - 1) * pow(v, _P - 2, _P) % _P
    if x2 == 0:
        return (b[31] >> 7) == 0
    return pow(x2, (_P - 1) // 2, _P) == 1


def is_program_derived(addr: str) -> bool:
    """True only when the address can be **proved** to be program-derived: it parses as a 32-byte
    address and is off the curve.

    The asymmetry is deliberate and it is the difference between the two directions of being
    wrong. Excluding a holder lowers `top10_pct`, which can only let a coin past R5; counting one
    that is really a vault can only kill a coin that was fine. So nothing is excluded on a guess:
    an address that does not parse is counted as somebody's position.
    """
    try:
        b = b58decode(addr)
    except (ValueError, AttributeError, TypeError):
        return False
    return len(b) == 32 and not is_wallet_address(addr)


# ---- the table ----------------------------------------------------------------------
@dataclass(frozen=True)
class Holder:
    owner: str
    pct: float
    address: str | None = None          # the token account, when the provider says
    insider: bool | None = None         # None: this provider has no opinion
    known: str | None = None            # RugCheck's label, when there is one
    program_owned: bool = False         # the owner account is owned by a program, not a person
    market: bool = False                # one of this coin's own pool accounts

    @property
    def is_wallet(self) -> bool:
        """Somebody's position, which is what a concentration rule is about.

        Excluded only on proof -- a pool account of this coin, a program that owns the account, or
        RugCheck's own label. Being off the curve is NOT enough: see the module docstring.
        """
        return not (self.market or self.program_owned or self.known in NOT_A_WALLET)


@dataclass(frozen=True)
class HolderTable:
    source: str                          # "rpc" | "rugcheck" | "none"
    holders: tuple[Holder, ...] = ()
    present: bool = False                # False: nobody answered, so R5 has no input and kills
    insiders_known: bool = False         # whether `insider` means anything in this table
    note: str = ""

    @property
    def wallets(self) -> list[Holder]:
        """Holders that are somebody's position, largest first. Vaults and curves are gone."""
        return sorted((h for h in self.holders if h.is_wallet), key=lambda h: -h.pct)

    @property
    def excluded(self) -> list[Holder]:
        return [h for h in self.holders if not h.is_wallet]


class RugCheckHolders:
    """`topHolders` from the report the Filter has already paid for. Intermittent; see the module."""

    name = "rugcheck"

    def table(self, mint: str, pair_address: str | None, report: dict, supply: float) -> HolderTable:
        raw = report.get("topHolders")
        if not raw:
            raise DataUnavailable("rugcheck: report carries no topHolders")
        known = {a: (k or {}).get("type") for a, k in (report.get("knownAccounts") or {}).items()}
        pools = market_accounts(report, pair_address)
        out = []
        for h in raw:
            owner = h.get("owner")
            if not owner:
                continue
            out.append(Holder(
                owner=owner, pct=float(h.get("pct") or 0.0), address=h.get("address"),
                insider=bool(h.get("insider")),
                known=known.get(owner) or known.get(h.get("address") or ""),
                market=bool(pools & {owner, h.get("address")})))
        return HolderTable("rugcheck", tuple(out), present=True, insiders_known=True,
                           note=f"{len(out)} rows")


class RpcHolders:
    """The largest token accounts for the mint, straight from the chain, resolved to their owners.

    Three calls: `getTokenLargestAccounts`, then `getMultipleAccounts` over those token accounts
    for their `owner`, then `getMultipleAccounts` over the owners to see which are program
    accounts. Two token accounts of the same wallet are summed into one holder, which RugCheck's
    table does not do.

    `getTokenLargestAccounts` is an **indexed** call: the node has to keep the spl-token secondary
    index, and endpoints without a key refuse it -- the public one answers
    `429 "Too many requests for a specific RPC call"`, publicnode says "Indexed requests require a
    personal token", drpc says the chain is not on the free plan. So this provider answers only on
    a keyed endpoint, set through `SOLANA_RPC_URL`; everywhere else it raises and RugCheck is
    asked instead. That is why it is a provider and not a replacement.
    """

    name = "rpc"

    def __init__(self, rpc):
        self.rpc = rpc

    def table(self, mint: str, pair_address: str | None, report: dict, supply: float) -> HolderTable:
        if not supply:
            raise DataUnavailable("rpc holders: supply unknown, no denominator")
        largest = self.rpc.token_largest_accounts(mint)
        if not largest:
            raise DataUnavailable("rpc holders: mint has no token accounts")
        addrs = [a["address"] for a in largest if a.get("address")]
        owners = self.rpc.token_account_owners(addrs)
        amount = {}
        account_of = {}
        for a in largest:
            owner = owners.get(a.get("address"))
            if not owner:
                continue                      # a token account we could not resolve is not credited
            amount[owner] = amount.get(owner, 0.0) + float(a.get("amount") or 0)
            account_of.setdefault(owner, a["address"])
        if not amount:
            raise DataUnavailable("rpc holders: no owner resolved for any token account")
        programs = self.rpc.account_programs(sorted(amount))
        known = {a: (k or {}).get("type") for a, k in (report.get("knownAccounts") or {}).items()}
        pools = market_accounts(report, pair_address)
        out = [Holder(owner=owner, pct=amt / supply * 100.0, address=account_of.get(owner),
                      insider=None, known=known.get(owner),
                      program_owned=programs.get(owner) not in (None, SYSTEM_PROGRAM),
                      market=bool(pools & {owner, account_of.get(owner)}))
               for owner, amt in amount.items()]
        return HolderTable("rpc", tuple(out), present=True, insiders_known=False,
                           note=f"{len(addrs)} token accounts -> {len(out)} owners")


class Holders:
    """Ask each provider in order; the first that answers wins and the table says which it was.

    `rpc` is used for one thing beyond the providers: asking the chain which of the owners are
    program accounts. That is a plain `getMultipleAccounts`, which the public endpoint answers --
    unlike the indexed call `RpcHolders` needs -- so the cheap half of the ground truth is
    available even where the provider itself cannot answer. On `Alice` it is what catches the
    1.10% bonding-curve account RugCheck had no label for.
    """

    def __init__(self, providers, rpc=None):
        self.providers = list(providers)
        self.rpc = rpc

    def classify(self, table: HolderTable) -> HolderTable:
        """Mark the holders whose owner account belongs to a program. Never raises: a table that
        could not be classified is the table as the provider gave it, and R5 then counts an
        account it might have excluded -- the safe direction."""
        if self.rpc is None or not table.present:
            return table
        try:
            programs = self.rpc.account_programs(sorted({h.owner for h in table.holders}))
        except Exception as e:                          # noqa: BLE001 - see the docstring
            return dataclasses.replace(table, note=f"{table.note}; owners unclassified ({e})")
        return dataclasses.replace(table, holders=tuple(
            dataclasses.replace(h, program_owned=h.program_owned or
                                programs.get(h.owner) not in (None, SYSTEM_PROGRAM))
            for h in table.holders))

    def table(self, pair, report: dict, rpc_mint: dict | None = None) -> HolderTable:
        # Prefer the chain's own supply as the denominator when we have it: a ground-truth
        # numerator divided by a third party's denominator is neither.
        supply = 0.0
        for src in (rpc_mint or {}, report.get("token") or {}):
            try:
                supply = float(src.get("supply") or 0) or supply
            except (TypeError, ValueError):
                pass
            if supply:
                break
        tried = []
        for p in self.providers:
            try:
                return self.classify(
                    p.table(pair.mint, getattr(pair, "pair_address", None), report, supply))
            except DataUnavailable as e:
                tried.append(f"{p.name}: {e}")
            except Exception as e:                      # noqa: BLE001
                # Deliberately broad: a provider that breaks must not break the verdict, and the
                # fallback here is a kill (present=False -> R5 "holders unverifiable"), never a
                # pass. The reason travels in the note and into the kill log.
                tried.append(f"{p.name}: {type(e).__name__} {e}")
        return HolderTable("none", present=False, note="; ".join(tried) or "no providers")


def default_chain(sources) -> Holders:
    """The chain first, the paid report as fallback, owners classified against the chain either way."""
    return Holders([RpcHolders(sources.rpc), RugCheckHolders()], rpc=sources.rpc)
