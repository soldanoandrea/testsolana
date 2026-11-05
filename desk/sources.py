"""Data sources. Every method either returns real data or raises DataUnavailable."""
from __future__ import annotations

import time
from typing import Iterable, Optional

from .http import Http, DataUnavailable

SOL_MINT = "So11111111111111111111111111111111111111112"
DS = "https://api.dexscreener.com"
GT = "https://api.geckoterminal.com/api/v2"
RC = "https://api.rugcheck.xyz/v1"


class DexScreener:
    def __init__(self, http: Http):
        self.http = http

    def discover_mints(self) -> set[str]:
        """Recently listed / promoted Solana tokens from DexScreener feeds."""
        mints: set[str] = set()
        for path in ("/token-profiles/latest/v1", "/token-boosts/latest/v1", "/token-boosts/top/v1"):
            try:
                for t in self.http.get(DS + path) or []:
                    if t.get("chainId") == "solana" and t.get("tokenAddress"):
                        mints.add(t["tokenAddress"])
            except DataUnavailable:
                continue
        for q in ("pumpswap", "pump", "raydium"):
            try:
                for p in (self.http.get(f"{DS}/latest/dex/search", params={"q": q}) or {}).get("pairs") or []:
                    if p.get("chainId") == "solana":
                        mints.add(p["baseToken"]["address"])
            except (DataUnavailable, KeyError):
                continue
        mints.discard(SOL_MINT)
        return mints

    def pairs_for_mints(self, mints: Iterable[str]) -> list[dict]:
        mints = list(dict.fromkeys(mints))
        out: list[dict] = []
        for i in range(0, len(mints), 30):
            chunk = ",".join(mints[i:i + 30])
            out.extend(self.http.get(f"{DS}/tokens/v1/solana/{chunk}") or [])
        return out

    def prices(self, mints: Iterable[str]) -> dict[str, dict]:
        """Most liquid pair per mint -> {'price': float, 'pair': raw}"""
        best: dict[str, dict] = {}
        for p in self.pairs_for_mints(mints):
            m = p.get("baseToken", {}).get("address")
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
            if m and (m not in best or liq > float((best[m].get("liquidity") or {}).get("usd") or 0)):
                best[m] = p
        return {m: {"price": float(p.get("priceUsd") or 0), "pair": p} for m, p in best.items()}


class GeckoTerminal:
    def __init__(self, http: Http):
        self.http = http

    def discover_mints(self) -> set[str]:
        mints: set[str] = set()
        paths = ["/networks/solana/trending_pools?page=1", "/networks/solana/trending_pools?page=2"]
        for dex in ("pumpswap", "pump-fun", "raydium", "raydium-clmm", "raydium-launchlab"):
            paths.append(f"/networks/solana/dexes/{dex}/pools?sort=h24_volume_usd_desc&page=1")
        paths.append("/networks/solana/dexes/pumpswap/pools?sort=h24_tx_count_desc&page=1")
        for path in paths:
            try:
                for pool in (self.http.get(GT + path) or {}).get("data", []):
                    base = (pool.get("relationships", {}).get("base_token", {}).get("data") or {}).get("id", "")
                    if base.startswith("solana_"):
                        mints.add(base[len("solana_"):])
            except DataUnavailable:
                continue
        mints.discard(SOL_MINT)
        return mints

    def pool_h1_activity(self, pool_address: str) -> dict:
        d = self.http.get(f"{GT}/networks/solana/pools/{pool_address}")
        tx = d["data"]["attributes"]["transactions"]["h1"]
        return {"buys": int(tx.get("buys") or 0), "sells": int(tx.get("sells") or 0),
                "buyers": int(tx.get("buyers") or 0), "sellers": int(tx.get("sellers") or 0)}


class RugCheck:
    def __init__(self, http: Http):
        self.http = http
        self._cache: dict[str, tuple[float, dict]] = {}

    def report(self, mint: str, max_age_s: float = 60) -> dict:
        hit = self._cache.get(mint)
        if hit and time.time() - hit[0] < max_age_s:
            return hit[1]
        d = self.http.get(f"{RC}/tokens/{mint}/report")
        if not isinstance(d, dict) or "token" not in d:
            raise DataUnavailable(f"rugcheck: malformed report for {mint}")
        self._cache[mint] = (time.time(), d)
        return d

    def was_rugged(self, mint: str) -> bool:
        return bool(self.report(mint, max_age_s=3600).get("rugged"))


class Jupiter:
    """Second price source for the stop monitor: the same quote route live trades through."""

    def __init__(self, http: Http, base: str):
        self.http = http
        self.base = base

    def prices(self, mints: Iterable[str]) -> dict[str, dict]:
        mints = [m for m in dict.fromkeys(mints) if m]
        out: dict[str, dict] = {}
        for i in range(0, len(mints), 50):
            d = self.http.get(f"{self.base}/price/v3", params={"ids": ",".join(mints[i:i + 50])})
            for m, v in (d or {}).items():
                try:
                    px = float((v or {}).get("usdPrice") or 0)
                except (TypeError, ValueError):
                    continue
                if px > 0:
                    out[m] = {"price": px, "source": "jupiter"}
        return out


class SolanaRPC:
    def __init__(self, http: Http, url: str):
        self.http = http
        self.url = url

    def call(self, method: str, params: list):
        d = self.http.post(self.url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if "error" in d:
            raise DataUnavailable(f"rpc {method}: {d['error']}")
        return d["result"]

    def mint_info(self, mint: str) -> dict:
        r = self.call("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
        v = r.get("value")
        if not v:
            raise DataUnavailable(f"mint {mint} not found")
        return v["data"]["parsed"]["info"]

    def token_balance(self, owner: str, mint: str) -> float:
        r = self.call("getTokenAccountsByOwner", [owner, {"mint": mint}, {"encoding": "jsonParsed"}])
        total = 0.0
        for acc in r.get("value", []):
            total += float(acc["account"]["data"]["parsed"]["info"]["tokenAmount"].get("uiAmount") or 0)
        return total

    def sol_balance(self, owner: str) -> float:
        return self.call("getBalance", [owner])["value"] / 1e9


class Sources:
    def __init__(self, http: Http, rpc_url: str, jupiter_base: str = "https://lite-api.jup.ag"):
        self.dex = DexScreener(http)
        self.gt = GeckoTerminal(http)
        self.rug = RugCheck(http)
        self.jup = Jupiter(http, jupiter_base)
        self.rpc = SolanaRPC(http, rpc_url)
