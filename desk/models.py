from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

from . import rules


@dataclass
class Pair:
    """A DexScreener pair snapshot. All numbers in USD unless stated."""
    pair_address: str
    mint: str
    symbol: str
    dex_id: str
    created_at_ms: int
    price_usd: float
    market_cap: float
    liquidity_usd: float
    volume_h1: float
    price_change_m5: float
    price_change_h1: float
    buys_h1: int
    sells_h1: int
    url: str = ""

    @property
    def age_min(self) -> float:
        return (time.time() * 1000 - self.created_at_ms) / 60000.0

    @classmethod
    def from_dexscreener(cls, p: dict) -> "Pair":
        def f(x, default=0.0):
            try:
                return float(x)
            except (TypeError, ValueError):
                return default
        tx = (p.get("txns") or {}).get("h1") or {}
        return cls(
            pair_address=p["pairAddress"],
            mint=p["baseToken"]["address"],
            symbol=p["baseToken"].get("symbol", "?"),
            dex_id=p.get("dexId", ""),
            created_at_ms=int(p.get("pairCreatedAt") or 0),
            price_usd=f(p.get("priceUsd")),
            market_cap=f(p.get("marketCap") or p.get("fdv")),
            liquidity_usd=f((p.get("liquidity") or {}).get("usd")),
            volume_h1=f((p.get("volume") or {}).get("h1")),
            price_change_m5=f((p.get("priceChange") or {}).get("m5")),
            price_change_h1=f((p.get("priceChange") or {}).get("h1")),
            buys_h1=int(tx.get("buys") or 0),
            sells_h1=int(tx.get("sells") or 0),
            url=p.get("url", ""),
        )

    def brief(self) -> dict:
        return {"mint": self.mint, "symbol": self.symbol, "pair": self.pair_address, "dex": self.dex_id,
                "age_min": round(self.age_min, 1), "mcap": round(self.market_cap), "liq": round(self.liquidity_usd),
                "vol_h1": round(self.volume_h1), "chg_h1": self.price_change_h1, "price": self.price_usd}


@dataclass
class Safety:
    """On-chain safety facts the Filter needs. None = unknown (Filter kills on unknown)."""
    mint_authority_revoked: Optional[bool] = None
    freeze_authority_revoked: Optional[bool] = None
    lp_locked_pct: Optional[float] = None
    top10_pct: Optional[float] = None
    dev_pct: Optional[float] = None
    dev_past_rugs: Optional[int] = None
    bundle_pct: Optional[float] = None
    holders: Optional[int] = None
    notes: list = field(default_factory=list)


@dataclass
class Verdict:
    pair: Pair
    alive: bool
    rule: Optional[int] = None
    rule_name: Optional[str] = None
    detail: str = ""
    safety: Optional[Safety] = None


@dataclass
class Score:
    pair: Pair
    total: float
    parts: dict
    inputs: dict


@dataclass
class Position:
    mint: str
    symbol: str
    pair_address: str
    score: float
    entry_price: float
    qty: float                 # token units
    cost_usd: float            # what we paid incl. fees
    stop_price: float
    opened_at: float
    peak_price: float
    trailing: bool = False
    sell_attempts: int = 0     # carried across ticks so the slippage escalation does not restart
    tx_entry: str = ""

    def stop_floor(self) -> float:
        """The lowest this position's stop may ever sit: the hard stop off the real entry."""
        return self.entry_price * (1 + rules.RISK_STOP_PCT / 100)

    def raise_stop(self, new_stop: float) -> bool:
        """Stops only move up. Any attempt to lower one is refused."""
        if new_stop <= self.stop_price:
            return False
        self.stop_price = new_stop
        return True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
