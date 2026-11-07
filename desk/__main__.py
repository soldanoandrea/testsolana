"""CLI.

    python -m desk run [--hours 10]          # paper mode (default)
    python -m desk run --live                 # live; refused until paper logs are clean
    python -m desk recap [--mode paper]       # print/write the recap for the current session
    python -m desk status [--mode paper]
    python -m desk actors [--mode paper]      # who the desk has seen behind the coins
    python -m desk actors --backfill          # recover creators for coins already in the log
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

from .config import Config
from .desk import Desk, LiveGateError


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="desk")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--live", action="store_true", help="trade real money (gated)")
    r.add_argument("--hours", type=float, default=None, help="stop after N hours (default: until killed)")
    for name in ("recap", "status"):
        p = sub.add_parser(name)
        p.add_argument("--mode", choices=["paper", "live"], default="paper")
    ac = sub.add_parser("actors")
    ac.add_argument("--mode", choices=["paper", "live"], default="paper")
    ac.add_argument("--backfill", action="store_true",
                    help="re-fetch RugCheck for coins already in the log (creators only: "
                         "the holder table is erased once a pool is gone)")
    ac.add_argument("--events", action="append", default=[],
                    help="extra events.jsonl to take coins from; repeatable")
    a = ap.parse_args(argv)

    mode = "live" if getattr(a, "live", False) else getattr(a, "mode", "paper")
    cfg = dataclasses.replace(Config(), mode=mode)
    if a.cmd == "run":
        try:
            desk = Desk(cfg)
        except LiveGateError as e:
            print(e, file=sys.stderr)
            return 2
        desk.run(hours=a.hours)
        return 0
    # recap/status/actors only read files; they never touch a wallet
    from .agents.logger import Logger
    from .state import State
    if a.cmd == "actors":
        return _actors(cfg, a)
    st = State.load(cfg.path("state.json"), cfg.paper_start_balance_usd)
    log = Logger(cfg.path("events.jsonl"), echo=False)
    if a.cmd == "status":
        print(json.dumps({"cash": st.cash, "start_balance": st.start_balance,
                          "open": [p.to_dict() for p in st.positions.values()],
                          "traded": len(st.traded), "dead": len(st.dead)}, indent=1))
    else:
        marked = sum(p.cost_usd for p in st.positions.values())  # at cost; run-time recap marks to market
        print(log.recap(st.session_start, st.cash + marked, st.start_balance, [p.to_dict() for p in st.positions.values()]))
    return 0


def _actors(cfg, a) -> int:
    """Report the actor store, and optionally backfill it from the event log."""
    import json as _json
    from .actors import ActorStore, backfill_from_events
    from .agents.logger import Logger

    store = ActorStore(cfg.path("actors.jsonl"))
    if a.backfill:
        from .http import Http
        from .sources import Sources
        ev = Logger(cfg.path("events.jsonl"), echo=False).events()
        for extra in a.events:
            ev += Logger(Path(extra), echo=False).events()
        src = Sources(Http(), cfg.solana_rpc_url, cfg.jupiter_base)
        print(f"Backfill da {len(ev)} eventi. Recupera creator e storico lanci; la tabella holder "
              f"viene erasa da RugCheck quando il pool si svuota, quindi il grafo wallet parte da ora.")
        res = backfill_from_events(store, ev, src.rug)
        print("\n" + _json.dumps(res, indent=1))
    s = store.summary()
    print(f"\nStore: {s['records']} record su {s['coins']} coin — "
          f"{s['creators']} creator, {s['wallets']} wallet indicizzati")
    if s["repeat_creators"]:
        print(f"\nCreator visti su piu' di una coin ({len(s['repeat_creators'])}):")
        for c, coins in sorted(s["repeat_creators"].items(), key=lambda kv: -len(kv[1])):
            print(f"  {c}  ->  {', '.join(coins)}")
    else:
        print("\nNessun creator ripetuto ancora.")
    if s["repeat_wallets"]:
        print(f"\nWallet holder visti su piu' di una coin ({len(s['repeat_wallets'])}):")
        for w, coins in sorted(s["repeat_wallets"].items(), key=lambda kv: -len(kv[1]))[:20]:
            print(f"  {w}  ->  {len(coins)} coin")
    if s["recurring_network_ids"]:
        print(f"\nInsider network id ricorrenti: {s['recurring_network_ids']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
