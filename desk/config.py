"""Operational settings only (intervals, endpoints, paths, paper fill model).

Trading thresholds live in desk/rules.py and cannot be set from here.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Config:
    mode: str = "paper"                       # "paper" | "live"
    data_dir: Path = Path(_env("DESK_DATA_DIR", "data"))

    # loop timing
    scan_interval_s: int = int(_env("DESK_SCAN_INTERVAL_S", "60"))
    risk_interval_s: int = int(_env("DESK_RISK_INTERVAL_S", "10"))
    holder_window_s: int = int(_env("DESK_HOLDER_WINDOW_S", "300"))  # Analyst watches holders this long before scoring
    recap_hour_local: int = int(_env("DESK_RECAP_HOUR", "8"))

    # endpoints
    solana_rpc_url: str = _env("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
    jupiter_base: str = _env("JUPITER_BASE", "https://lite-api.jup.ag")

    # paper
    paper_start_balance_usd: float = float(_env("DESK_PAPER_BALANCE_USD", "1000"))
    paper_slippage_pct: float = float(_env("DESK_PAPER_SLIPPAGE_PCT", "1.5"))   # per side, against us
    paper_fee_pct: float = float(_env("DESK_PAPER_FEE_PCT", "0.3"))             # per side (DEX/LP fee)
    paper_network_fee_usd: float = float(_env("DESK_PAPER_NETWORK_FEE_USD", "0.05"))

    # live
    live_slippage_bps: int = int(_env("DESK_LIVE_SLIPPAGE_BPS", "300"))
    # NOTE: the live gate's thresholds are in rules.py on purpose. They used to be here, which
    # meant DESK_LIVE_MIN_PAPER_TRADES=0 opened live mode with no paper record at all.

    # smart wallets: one base58 address per line, '#' comments allowed
    smart_wallets_file: Path = Path(_env("DESK_SMART_WALLETS", "smart_wallets.txt"))
    max_smart_wallet_checks: int = 40

    def path(self, name: str) -> Path:
        d = self.data_dir / self.mode
        d.mkdir(parents=True, exist_ok=True)
        return d / name

    def smart_wallets(self) -> list[str]:
        if not self.smart_wallets_file.exists():
            return []
        out = []
        for line in self.smart_wallets_file.read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                out.append(line)
        return out[: self.max_smart_wallet_checks]
