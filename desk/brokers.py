"""Order routing. PaperBroker simulates fills pessimistically; LiveBroker swaps through Jupiter."""
from __future__ import annotations

import base64
import time
from dataclasses import dataclass

from .http import DataUnavailable
from .sources import SOL_MINT


def sell_slippage_bps(base_bps: int, attempt: int) -> int:
    """Each failed exit doubles the allowance so exits get out, capped at 8x the base."""
    return base_bps * 2 ** min(max(attempt, 0), 3)


@dataclass
class Fill:
    price: float      # effective USD per token
    qty: float        # tokens
    usd: float        # cost (buy) or proceeds (sell), fees included
    tx: str = ""


class PaperBroker:
    live = False

    def __init__(self, cfg, state):
        self.cfg = cfg
        self.state = state  # state.cash is the paper balance

    def balance_usd(self) -> float:
        return self.state.cash

    def held_qty(self, mint: str) -> float:
        """Paper never holds anything the state file does not already know about."""
        return 0.0

    def expected_buy_price(self, mint: str, usd: float, ref_price: float) -> float:
        return ref_price * (1 + self.cfg.paper_slippage_pct / 100)

    def buy(self, mint: str, usd: float, ref_price: float) -> Fill:
        price = self.expected_buy_price(mint, usd, ref_price)
        spend = usd - usd * self.cfg.paper_fee_pct / 100 - self.cfg.paper_network_fee_usd
        qty = spend / price
        self.state.cash -= usd
        return Fill(price=usd / qty, qty=qty, usd=usd, tx="paper")

    def sell(self, mint: str, qty: float, ref_price: float, attempt: int = 0) -> Fill:
        price = ref_price * (1 - self.cfg.paper_slippage_pct / 100)
        gross = qty * price
        proceeds = gross - gross * self.cfg.paper_fee_pct / 100 - self.cfg.paper_network_fee_usd
        proceeds = max(proceeds, 0.0)
        self.state.cash += proceeds
        return Fill(price=proceeds / qty if qty else 0, qty=qty, usd=proceeds, tx="paper")


class LiveBroker:
    """Real swaps via Jupiter. Requires `solders` and SOLANA_PRIVATE_KEY (base58). Keeps a SOL fee reserve."""
    live = True
    SOL_RESERVE = 0.05

    def __init__(self, cfg, http, rpc, private_key_b58: str):
        from solders.keypair import Keypair  # imported lazily so paper mode never needs it
        self.cfg, self.http, self.rpc = cfg, http, rpc
        self.kp = Keypair.from_base58_string(private_key_b58)
        self.owner = str(self.kp.pubkey())

    # --- helpers
    def sol_price(self) -> float:
        d = self.http.get(f"{self.cfg.jupiter_base}/price/v3?ids={SOL_MINT}")
        return float(d[SOL_MINT]["usdPrice"])

    def _decimals(self, mint: str) -> int:
        return int(self.rpc.mint_info(mint)["decimals"])

    def _quote(self, inp: str, out: str, amount: int, slippage_bps: int | None = None) -> dict:
        return self.http.get(f"{self.cfg.jupiter_base}/swap/v1/quote", params={
            "inputMint": inp, "outputMint": out, "amount": str(amount),
            "slippageBps": str(slippage_bps or self.cfg.live_slippage_bps)})

    def _swap(self, quote: dict) -> str:
        from solders.transaction import VersionedTransaction
        body = {"quoteResponse": quote, "userPublicKey": self.owner, "wrapAndUnwrapSol": True,
                "dynamicComputeUnitLimit": True, "prioritizationFeeLamports": "auto"}
        tx_b64 = self.http.post(f"{self.cfg.jupiter_base}/swap/v1/swap", json=body)["swapTransaction"]
        raw = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
        signed = VersionedTransaction(raw.message, [self.kp])
        sig = self.rpc.call("sendTransaction", [base64.b64encode(bytes(signed)).decode(),
                                                {"encoding": "base64", "skipPreflight": False, "maxRetries": 3}])
        for _ in range(60):
            st = self.rpc.call("getSignatureStatuses", [[sig]])["value"][0]
            if st and st.get("err"):
                raise DataUnavailable(f"swap failed on-chain: {st['err']}")
            if st and st.get("confirmationStatus") in ("confirmed", "finalized"):
                return sig
            time.sleep(2)
        raise DataUnavailable(f"swap not confirmed: {sig}")

    # --- broker API
    def balance_usd(self) -> float:
        sol = max(self.rpc.sol_balance(self.owner) - self.SOL_RESERVE, 0.0)
        return sol * self.sol_price()

    def held_qty(self, mint: str) -> float:
        return self.rpc.token_balance(self.owner, mint)

    def expected_buy_price(self, mint: str, usd: float, ref_price: float) -> float:
        lamports = int(usd / self.sol_price() * 1e9)
        q = self._quote(SOL_MINT, mint, lamports)
        out = int(q["otherAmountThreshold"]) / 10 ** self._decimals(mint)   # worst-case fill within slippage
        return usd / out

    def buy(self, mint: str, usd: float, ref_price: float) -> Fill:
        sol_px = self.sol_price()
        q = self._quote(SOL_MINT, mint, int(usd / sol_px * 1e9))
        before = self.rpc.token_balance(self.owner, mint)
        sig = self._swap(q)
        time.sleep(2)
        qty = self.rpc.token_balance(self.owner, mint) - before
        if qty <= 0:
            raise DataUnavailable(f"buy confirmed but no tokens received: {sig}")
        return Fill(price=usd / qty, qty=qty, usd=usd, tx=sig)

    def sell(self, mint: str, qty: float, ref_price: float, attempt: int = 0) -> Fill:
        """Exits must get out: each retry widens slippage (300 -> 600 -> 1200 bps ...)."""
        dec = self._decimals(mint)
        held = self.rpc.token_balance(self.owner, mint)
        amount = int(min(qty, held) * 10 ** dec)
        q = self._quote(mint, SOL_MINT, amount, sell_slippage_bps(self.cfg.live_slippage_bps, attempt))
        sol_before = self.rpc.sol_balance(self.owner)
        sig = self._swap(q)
        time.sleep(2)
        got_sol = self.rpc.sol_balance(self.owner) - sol_before
        usd = max(got_sol, 0) * self.sol_price()
        return Fill(price=usd / qty if qty else 0, qty=qty, usd=usd, tx=sig)
