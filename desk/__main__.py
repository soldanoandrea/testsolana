"""CLI.

    python -m desk run [--hours 10]          # paper mode (default)
    python -m desk run --live                 # live; refused until paper logs are clean
    python -m desk recap [--mode paper]       # print/write the recap for the current session
    python -m desk status [--mode paper]
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys

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
    # recap/status only read files; they never touch a wallet
    from .agents.logger import Logger
    from .state import State
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


if __name__ == "__main__":
    sys.exit(main())
