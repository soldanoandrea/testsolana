"""CLI.

    python -m desk run [--hours 10]          # paper mode (default)
    python -m desk run --live                 # live; refused until paper logs are clean
    python -m desk recap [--mode paper]       # print/write the recap for the current session
    python -m desk status [--mode paper]
    python -m desk actors [--mode paper]      # who the desk has seen behind the coins
    python -m desk actors --backfill          # recover creators for coins already in the log
    python -m desk rpc [--mint MINT]          # which calls the configured endpoint answers
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
                    help="re-fetch RugCheck for coins already in the log (creators and launch "
                         "history; the holder table is only there when RugCheck feels like it)")
    ac.add_argument("--events", action="append", default=[],
                    help="extra events.jsonl to take coins from; repeatable")
    rp = sub.add_parser("rpc", help="check which calls the configured RPC endpoint answers")
    rp.add_argument("--mode", choices=["paper", "live"], default="paper")
    rp.add_argument("--mint", default="BdouwEr3mwXkT6S6EmHNCjn9oEtykRKCKiUSTRF9pump",
                    help="mint to probe with (default: a coin the desk has already looked at)")
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
    if a.cmd == "rpc":
        return _rpc(cfg, a)
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
        src = Sources(Http(), cfg.rpc_url(), cfg.jupiter_base)
        print(f"Backfill da {len(ev)} eventi. Recupera creator e storico lanci; la tabella holder "
              f"la restituisce o no RugCheck, in modo intermittente, quindi il grafo wallet si "
              f"popola solo sui look che ce l'hanno.")
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


def _rpc(cfg, a) -> int:
    """Say which endpoint is configured and which of the four calls it answers.

    Never prints the URL: a paid endpoint carries its credential inside it. Only the host, and
    only through the same redaction every log line goes through.
    """
    from .config import SecretUnavailable
    from .http import DataUnavailable, Http, redact

    print(f"Origine endpoint: {cfg.rpc_source()}")
    try:
        url = cfg.rpc_url()
    except SecretUnavailable as e:
        print(f"\nNon risolvibile: {e}", file=sys.stderr)
        print("\nIl riferimento c'e' ma non si legge, quindi il desk non parte: una ricaduta "
              "silenziosa sull'endpoint pubblico rimetterebbe R5 su un solo campo intermittente "
              "senza dirlo. Sblocca 1Password (e con piu' account esporta OP_ACCOUNT), oppure "
              "togli DESK_RPC_URL_REF.", file=sys.stderr)
        return 2
    print(f"Endpoint:         {redact(url)}")

    from .sources import SolanaRPC
    rpc = SolanaRPC(Http(), url)
    probes = [
        ("getHealth", lambda: rpc.call("getHealth", []), "il nodo risponde"),
        ("getAccountInfo", lambda: rpc.mint_info(a.mint), "R1: mint/freeze authority"),
        ("getMultipleAccounts", lambda: rpc.account_programs([a.mint]), "quali holder sono di un programma"),
        ("getTokenLargestAccounts", lambda: rpc.token_largest_accounts(a.mint), "R5: holder dalla catena"),
    ]
    ok = {}
    print()
    for name, call, why in probes:
        try:
            r = call()
            n = len(r) if isinstance(r, (list, dict)) else 1
            ok[name] = True
            print(f"  {name:25s} ok   ({n} elementi)   {why}")
        except (DataUnavailable, KeyError, TypeError) as e:
            ok[name] = False
            print(f"  {name:25s} NO   {e}")
    print()
    if ok.get("getTokenLargestAccounts"):
        print("Verdetto: holder dalla catena ATTIVI. RpcHolders risponde e RugCheck resta fallback.")
        return 0
    print("Verdetto: holder dalla catena NON attivi. getTokenLargestAccounts e' una chiamata "
          "indicizzata:\n  l'endpoint deve tenere l'indice spl-token, cosa che gli endpoint "
          "senza chiave rifiutano.\n  La catena usa RugCheck, con la sua intermittenza, e la "
          "classificazione degli owner\n  continua a funzionare perche' getMultipleAccounts non "
          "e' indicizzata.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
