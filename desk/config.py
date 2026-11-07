"""Operational settings only (intervals, endpoints, paths, paper fill model).

Trading thresholds live in desk/rules.py and cannot be set from here.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

PUBLIC_RPC = "https://api.mainnet-beta.solana.com"


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


class SecretUnavailable(Exception):
    """A configured secret reference could not be resolved. Never falls back quietly."""


def read_secret(ref: str) -> str:
    """Resolve a 1Password secret reference (`op://Vault/Item/field`) with the `op` CLI.

    The reference is not a secret, so it can live in a shell profile or a launchd plist; the value
    it points at is read into this process and nowhere else. That is the only way a paid RPC
    endpoint can be configured here, because its credential travels *inside the URL* and would
    otherwise end up in a file, a saved environment variable or a log line.

    Raises rather than falling back. A reference that cannot be read is a broken configuration,
    and the quiet version -- dropping to the public endpoint -- is exactly the failure the holder
    provider chain was built to make visible: R5 would go back to resting on one intermittent
    field, with nothing in the log saying why.
    """
    if not ref.startswith("op://"):
        raise SecretUnavailable(f"a secret reference must start with op:// (got {ref[:8]!r}…)")
    try:
        r = subprocess.run(["op", "read", "--no-newline", ref],
                           capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        raise SecretUnavailable("1Password CLI `op` not found: install it, or unset the reference "
                                "and export SOLANA_RPC_URL for one command instead") from None
    except subprocess.TimeoutExpired:
        raise SecretUnavailable("`op read` timed out (is 1Password unlocked?)") from None
    if r.returncode != 0:
        # op's stderr names the item and the account, never the value
        why = (r.stderr or "").strip().splitlines()
        raise SecretUnavailable(f"`op read {ref}` failed: {why[-1] if why else f'exit {r.returncode}'}")
    value = r.stdout.strip()
    if not value:
        raise SecretUnavailable(f"`op read {ref}` returned nothing")
    return value


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
    solana_rpc_url: str = _env("SOLANA_RPC_URL", PUBLIC_RPC)
    # A 1Password reference for the RPC endpoint, e.g. op://Private/Helius/url. Preferred over
    # SOLANA_RPC_URL for a paid endpoint, whose credential is part of the URL: the reference can
    # be stored, the URL cannot. See read_secret().
    solana_rpc_url_ref: str = _env("DESK_RPC_URL_REF", "")
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

    def rpc_url(self) -> str:
        """The endpoint to use, resolved once per process. Raises if a reference cannot be read."""
        if self.solana_rpc_url_ref:
            return read_secret(self.solana_rpc_url_ref)
        return self.solana_rpc_url

    def rpc_source(self) -> str:
        """Where the endpoint came from, for a startup line that must not contain the URL."""
        if self.solana_rpc_url_ref:
            return f"1Password ({self.solana_rpc_url_ref})"
        if self.solana_rpc_url != PUBLIC_RPC:
            return "SOLANA_RPC_URL"
        return "public endpoint (no key: RpcHolders cannot answer)"

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
