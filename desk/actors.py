"""ACTOR MEMORY: who was behind each coin, written down at the moment the Filter paid for it.

Why this exists as a store and not a lookup. On 2026-11-07 every one of the 67 mints the desk had
ever seen was re-queried: **none** returned `topHolders`, and 65 of 67 still returned `creator`.
The field is not erased when a pool dies -- it comes and goes server-side on live tokens too, see
desk/holders.py -- so a wallet-level picture cannot be fetched on demand at all. Whatever look
happens to carry one is the only chance to write it down, which is why this records every report
the Filter pays for instead of looking things up later.

What an actor is here, and what it is not:

- a **wallet address** is an identity: `creator`, and each `topHolders[].owner`. Both are stable
  and join across coins, which is the whole point of the file.
- an **insider network** is not. RugCheck gives each network a `size`, a `type` and a slug `id`
  ("peaceful-champagne-stork") but never its member wallets, so two coins' networks cannot be
  matched by membership. The slugs were checked across all 66 recoverable reports and **not one
  id recurred**, so they are per-report labels, not actor identities. Network shape is therefore
  recorded as a property of the coin, and never indexed as an actor.
- `knownAccounts` entries (AMM vaults, lockers) are recorded but **excluded from every lookup**:
  the Pump Fun AMM holds a slice of every pumpswap coin, so counting it as a repeat actor would
  bury the real ones.

This file only records and reports. No verdict, no score and no threshold reads from it. That is
deliberate: there is not yet a measured relation between a repeat actor and an outcome, and the
store is what will eventually supply one.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .holders import is_program_derived

# Holder types that belong to the market rather than to a person. Mirrors the Filter's own
# exclusion for concentration rules, for the same reason.
NOT_AN_ACTOR = {"AMM", "LOCKER"}


def _is_actor(owner: str | None, known: str | None) -> bool:
    """A person's wallet, as opposed to a pool vault, a bonding curve or a locker escrow.

    Two tests, because RugCheck's labels turned out to be incomplete: the label, and whether the
    address is a point on the ed25519 curve at all (a program-derived address is not -- see
    desk/holders.py). Derived here at index time rather than stored, so records written before
    the second test existed are read correctly: persist the input, derive the decision.
    """
    return bool(owner) and known not in NOT_AN_ACTOR and not is_program_derived(owner)


@dataclass
class Sighting:
    """One wallet, in one role, on one coin, at one look."""
    ts: float
    mint: str
    symbol: str
    role: str                # "creator" | "holder"
    pct: float | None
    insider: bool = False
    verdict: str = ""        # the Filter's verdict on that look


@dataclass
class Priors:
    """What the desk already knew about this coin's actors, before this look."""
    creator: str | None = None
    creator_coins: list[str] = field(default_factory=list)       # other coins we saw from this creator
    creator_launches: int = 0                                     # RugCheck's count, incl. ones we never saw
    creator_launches_seen: list[str] = field(default_factory=list)  # creatorTokens that are in our own log
    repeat_wallets: dict[str, list[str]] = field(default_factory=dict)  # wallet -> other coins we saw it on

    @property
    def any(self) -> bool:
        return bool(self.creator_coins or self.repeat_wallets or self.creator_launches_seen)

    def brief(self) -> dict:
        return {"creator": self.creator, "creator_coins": self.creator_coins,
                "creator_launches": self.creator_launches,
                "creator_launches_seen": self.creator_launches_seen,
                "repeat_wallets": {w[:8] + "…": c for w, c in list(self.repeat_wallets.items())[:10]},
                "repeat_wallet_count": len(self.repeat_wallets)}


class ActorStore:
    """Append-only observations on disk, indices rebuilt from them in RAM.

    The split follows the lesson the bench taught: persist the input, derive the decision. An index
    written to disk would be a decision whose input was not, and that is what let a benched coin
    come back with its wait ignored.
    """

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.by_wallet: dict[str, list[Sighting]] = {}
        self.by_creator: dict[str, set[str]] = {}
        self.coins: dict[str, str] = {}          # mint -> symbol, every coin we have a record for
        self.network_ids: dict[str, set[str]] = {}
        self.records = 0
        self.load()

    # ---------------------------------------------------------------- reading
    def load(self) -> None:
        if not self.path.exists():
            return
        for line in self.path.read_text().splitlines():
            try:
                self._index(json.loads(line))
            except ValueError:
                continue

    def _index(self, rec: dict) -> None:
        mint, sym = rec.get("mint"), rec.get("symbol", "?")
        if not mint:
            return
        self.records += 1
        self.coins[mint] = sym
        ts, verdict = rec.get("ts", 0.0), rec.get("verdict", "")
        creator = rec.get("creator")
        if creator:
            self.by_creator.setdefault(creator, set()).add(mint)
            self.by_wallet.setdefault(creator, []).append(
                Sighting(ts, mint, sym, "creator", rec.get("creator_pct"), False, verdict))
        for h in rec.get("holders") or []:
            owner, known = h.get("owner"), h.get("known")
            if not _is_actor(owner, known):
                continue
            self.by_wallet.setdefault(owner, []).append(
                Sighting(ts, mint, sym, "holder", h.get("pct"), bool(h.get("insider")), verdict))
        for n in rec.get("networks") or []:
            if n.get("id"):
                self.network_ids.setdefault(n["id"], set()).add(mint)

    def priors(self, mint: str, report: dict, holders=None) -> Priors:
        """Everything already on file about this coin's actors. The coin itself is always excluded,
        so re-looking at the same coin can never manufacture its own prior."""
        with self._lock:
            p = Priors(creator=report.get("creator"))
            if p.creator:
                p.creator_coins = sorted(
                    self.coins.get(m, m) for m in self.by_creator.get(p.creator, set()) if m != mint)
            launches = [t.get("mint") for t in (report.get("creatorTokens") or []) if t.get("mint")]
            p.creator_launches = len(launches)
            p.creator_launches_seen = sorted(
                self.coins[m] for m in launches if m != mint and m in self.coins)
            known = report.get("knownAccounts") or {}
            rows = ([(h.owner, h.known) for h in holders.holders] if holders is not None
                    else [(h.get("owner"), (known.get(h.get("owner")) or {}).get("type"))
                          for h in report.get("topHolders") or []])
            for owner, label in rows:
                if not _is_actor(owner, label):
                    continue
                others = sorted({self.coins.get(s.mint, s.mint)
                                 for s in self.by_wallet.get(owner, []) if s.mint != mint})
                if others:
                    p.repeat_wallets[owner] = others
            return p

    # ---------------------------------------------------------------- writing
    def record(self, pair, report: dict, safety, verdict: str, holders=None) -> dict:
        """One record per RugCheck report the Filter paid for.

        A coin that comes back is written again rather than deduplicated: the *change* is the
        signal. LIA's creator read 32.73% of supply and 81.39% eight minutes later through the
        same code path, and only two records can show that.
        """
        return self.record_raw(pair.mint, pair.symbol, report, verdict, safety=safety,
                               holders=holders)

    def record_raw(self, mint: str, symbol: str, report: dict, verdict: str,
                   safety=None, source: str = "live", holders=None) -> dict:
        """The record shape, usable without a Pair or a Safety -- which is what a backfill has."""
        tok = report.get("token") or {}
        supply = float(tok.get("supply") or 0)
        known = report.get("knownAccounts") or {}
        # A HolderTable if the Filter had one (so an RPC-sourced graph lands here too), otherwise
        # the report's own field, which is what a backfill has.
        if holders is not None and holders.present:
            rows = [{"owner": h.owner, "pct": h.pct, "insider": h.insider, "known": h.known}
                    for h in holders.holders]
            holders_source = holders.source
        else:
            rows = [{"owner": h.get("owner"), "pct": h.get("pct"),
                     "insider": bool(h.get("insider")),
                     "known": (known.get(h.get("owner")) or {}).get("type")}
                    for h in report.get("topHolders") or [] if h.get("owner")]
            holders_source = "rugcheck" if rows else (holders.source if holders is not None else "none")
        holders = rows
        nets = [{"id": n.get("id"), "size": n.get("size"), "type": n.get("type"),
                 "holding_pct": round(float(n.get("currentHolding") or 0) / supply * 100, 4) if supply else None}
                for n in (report.get("insiderNetworks") or [])]
        rec = {
            "ts": time.time(), "iso": datetime.now().isoformat(timespec="seconds"),
            "mint": mint, "symbol": symbol, "source": source,
            "creator": report.get("creator"),
            "creator_pct": round(safety.dev_pct, 4) if safety is not None and safety.dev_pct is not None else None,
            "creator_launches": [{"mint": t.get("mint"), "created_at": t.get("createdAt"),
                                  "mcap": t.get("marketCap")}
                                 for t in (report.get("creatorTokens") or [])],
            # RugCheck's own flag, kept because it is what R6 reads -- and it said False for LIA,
            # whose pool was empty eight minutes after the desk looked at it.
            "rugcheck_rugged": report.get("rugged"),
            "rugcheck_score": report.get("score_normalised"),
            "holders": holders, "holders_source": holders_source,
            # A look can carry no holder table at all -- RugCheck's field comes and goes
            # server-side. Recorded explicitly so such a record is never read as "this coin had
            # no concentrated holders".
            "holders_erased": not holders,
            "networks": nets, "graph_insiders": report.get("graph_insiders") or report.get("graphInsidersDetected"),
            "total_holders": report.get("totalHolders"),
            "top10_pct": safety.top10_pct if safety is not None else None,
            "bundle_pct": safety.bundle_pct if safety is not None else None,
            "lp_locked_pct": safety.lp_locked_pct if safety is not None else None,
            "verdict": verdict,
        }
        self.append(rec)
        return rec

    def append(self, rec: dict) -> None:
        with self._lock:
            with self.path.open("a") as f:
                f.write(json.dumps(rec, default=str) + "\n")
            self._index(rec)

    # ---------------------------------------------------------------- summary
    def summary(self) -> dict:
        with self._lock:
            repeat_creators = {c: sorted(self.coins.get(m, m) for m in ms)
                               for c, ms in self.by_creator.items() if len(ms) > 1}
            repeat_wallets = {w: sorted({s.mint for s in ss})
                              for w, ss in self.by_wallet.items()
                              if len({s.mint for s in ss}) > 1 and any(s.role == "holder" for s in ss)}
            return {"records": self.records, "coins": len(self.coins),
                    "wallets": len(self.by_wallet), "creators": len(self.by_creator),
                    "repeat_creators": repeat_creators, "repeat_wallets": repeat_wallets,
                    "recurring_network_ids": {i: sorted(ms) for i, ms in self.network_ids.items()
                                              if len(ms) > 1}}


def backfill_from_events(store: ActorStore, events: list[dict], rug, sleep_s: float = 0.35,
                         echo=print) -> dict:
    """Re-fetch what RugCheck still holds for every coin already in the event log.

    This recovers **creators and their launch history, not the wallet graph**. Measured on
    2026-11-07 across the 67 mints the desk had seen: 65 still returned a `creator`, and **zero**
    returned `topHolders` on that pass. So every record this writes carries `holders_erased: true`
    and the cross-coin wallet index starts from the live looks that happen to carry a table.
    """
    seen: dict[str, str] = {}
    verdict: dict[str, str] = {}
    looks: dict[str, int] = {}
    for e in events:
        m = e.get("mint")
        if not m:
            continue
        if e.get("symbol"):
            seen.setdefault(m, e["symbol"])
        if e.get("event") == "kill" and e.get("rule_name"):
            verdict[m] = e["rule_name"]
            looks[m] = looks.get(m, 0) + 1
        elif e.get("event") == "survived_filter":
            verdict[m] = "SURVIVED"
            looks[m] = looks.get(m, 0) + 1
    out = {"coins": len(seen), "recorded": 0, "no_creator": 0, "failed": 0, "holders_recovered": 0}
    for i, (mint, symbol) in enumerate(sorted(seen.items(), key=lambda kv: kv[1])):
        if mint in store.coins:
            continue                                  # already on file, live record wins
        try:
            report = rug.report(mint, max_age_s=0)
        except Exception as e:                        # noqa: BLE001 - a backfill never blocks
            out["failed"] += 1
            echo(f"  {symbol:12s} non recuperabile: {e}")
            continue
        rec = store.record_raw(mint, symbol, report, verdict.get(mint, "UNKNOWN"), source="backfill")
        rec_looks = looks.get(mint, 0)
        out["recorded"] += 1
        if not report.get("creator"):
            out["no_creator"] += 1
        if not rec["holders_erased"]:
            out["holders_recovered"] += 1
        echo(f"  {symbol:12s} creator={(report.get('creator') or '-')[:8]:8s} "
             f"launches={len(report.get('creatorTokens') or []):2d} "
             f"holders={'si' if not rec['holders_erased'] else 'erasi':5s} "
             f"verdetto={verdict.get(mint, '?')} ({rec_looks} look)")
        time.sleep(sleep_s)
    return out
