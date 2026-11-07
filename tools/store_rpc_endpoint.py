#!/usr/bin/env python3
"""Put a paid Solana RPC endpoint in 1Password and point the desk at it.

Run this yourself, in your own terminal: it asks for the key with the echo off and nothing else in
this repo ever sees it.

The endpoint URL *is* a credential -- Helius keeps the key in the query string, Alchemy and
QuickNode in the path -- so this script is careful about the three places a secret leaks by
accident, none of which is the file it is supposed to end up in:

* **command arguments.** `op`'s own help warns that assignments "get logged in your command
  history, and can be visible to other processes on your machine". The URL is never an argument:
  it goes to `op item create` as a JSON template on stdin.
* **the shell.** No `printf "$SECRET"`, no here-string (bash writes those to a temp file), no
  environment variable. `getpass` reads straight from the terminal, the value stays in this
  process, and the process is short.
* **the screen and this script's own output.** Only `scheme://host/…` is ever printed, through the
  same `redact()` every log line in the desk goes through -- so the output is safe to paste back.

Usage:

    python3 tools/store_rpc_endpoint.py                       # Esamatic account, IT vault
    python3 tools/store_rpc_endpoint.py --vault 'Employee' --title 'Solana RPC'
    python3 tools/store_rpc_endpoint.py --replace              # archive an existing item first

It accepts either a bare Helius key or a full URL from any provider.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from desk.http import redact                      # noqa: E402

HELIUS = "https://mainnet.helius-rpc.com/?api-key={key}"
NOTE = ("Solana RPC endpoint for the memecoin desk (this repo). The whole URL is the credential.\n"
        "Read with: op read op://<vault>/<item>/credential\n"
        "The desk never stores it: DESK_RPC_URL_REF holds this reference and desk/config.py\n"
        "resolves it with `op read` into the process only. See README, "
        "'The RPC endpoint, and why it is never written down'.")


def endpoint_url(secret: str) -> str:
    """A bare provider key becomes a Helius URL; a full URL is taken as given.

    Raises ValueError rather than returning something unusable, because the caller is about to
    write the result into a password manager under a name that says it is an RPC endpoint.
    """
    secret = (secret or "").strip()
    if not secret:
        raise ValueError("niente inserito")
    url = secret if secret.startswith(("http://", "https://")) else HELIUS.format(key=secret)
    u = urlparse(url)
    if not u.hostname:
        raise ValueError("non sembra un URL valido")
    if u.scheme != "https":
        raise ValueError("un endpoint con credenziale nell'URL va su https")
    if " " in url:
        raise ValueError("la chiave contiene spazi")
    return url


def op(args: list[str], account: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    cmd = ["op", *args] + (["--account", account] if account else [])
    return subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=60)


def fail(msg: str) -> None:
    print(f"\n{msg}", file=sys.stderr)
    raise SystemExit(2)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--account", default=os.environ.get("DESK_OP_ACCOUNT", "esamaticsrl.1password.eu"),
                    help="1Password account shorthand (default: the Esamatic account, per policy)")
    ap.add_argument("--vault", default="Esamatic – Infrastruttura IT")
    ap.add_argument("--title", default="Solana RPC (Helius)")
    ap.add_argument("--field", default="credential")
    ap.add_argument("--replace", action="store_true", help="archive an item with this title first")
    a = ap.parse_args()

    if not sys.stdin.isatty():
        fail("Questo script va lanciato in un terminale: la chiave si digita, non si passa per pipe\n"
             "(una pipe vuol dire che il valore sta in un file o nella history).")

    print(f"Account 1Password : {a.account}")
    print(f"Vault             : {a.vault}")
    print(f"Item              : {a.title}")
    print("\nIncolla la chiave Helius (oppure l'URL completo di un altro provider).")
    print("Non viene mostrata, non finisce nella history, non viene scritta su file.")
    try:
        url = endpoint_url(getpass.getpass("Chiave o URL: "))
    except ValueError as e:
        fail(f"{e}.")
    u = urlparse(url)
    print(f"\nEndpoint da salvare: {redact(url)}")

    existing = op(["item", "get", a.title, "--vault", a.vault, "--fields", "label=title",
                   "--format", "json"], a.account)
    if existing.returncode == 0:
        if not a.replace:
            fail(f"Esiste già un item '{a.title}' in '{a.vault}'.\n"
                 f"Usa --replace per archiviarlo e crearne uno nuovo, o --title per un altro nome.")
        arch = op(["item", "delete", a.title, "--vault", a.vault, "--archive"], a.account)
        if arch.returncode != 0:
            fail(f"Non ho potuto archiviare l'item esistente: {arch.stderr.strip()}")
        print(f"Item precedente archiviato.")

    item = {
        "title": a.title,
        "category": "API_CREDENTIAL",
        "fields": [
            {"id": a.field, "type": "CONCEALED", "label": a.field, "value": url},
            {"id": "notesPlain", "type": "STRING", "purpose": "NOTES", "label": "notesPlain",
             "value": NOTE},
            {"id": "hostname", "type": "STRING", "label": "hostname", "value": u.hostname},
        ],
    }
    # The one call that carries the value: on stdin, not in argv.
    created = op(["item", "create", "-", "--vault", a.vault, "--format", "json"],
                 a.account, stdin=json.dumps(item))
    if created.returncode != 0:
        fail(f"`op item create` non è riuscito: {created.stderr.strip()}")
    try:                                   # op echoes the item back, concealed values included
        item_id = json.loads(created.stdout)["id"]
    except (ValueError, KeyError):
        item_id = a.title
    del created
    print(f"Creato: item {item_id} in '{a.vault}'")

    ref = f"op://{a.vault}/{a.title}/{a.field}"
    check = op(["read", "--no-newline", ref], a.account)
    if check.returncode != 0:
        fail(f"Scritto, ma non rileggibile con quel riferimento: {check.stderr.strip()}")
    read_back = check.stdout
    ok = read_back == url
    del check
    print(f"Rilettura        : {'identica' if ok else 'DIVERSA da quanto scritto'}")
    if not ok:
        fail("Il valore riletto non corrisponde: non configurare il desk su questo item.")

    print("\nAggiungi al tuo profilo di shell (sono riferimenti, non segreti):\n")
    print(f'  export DESK_RPC_URL_REF="{ref}"')
    print(f'  export DESK_OP_ACCOUNT="{a.account}"')
    print("\nPoi verifica con:\n\n  python -m desk rpc\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
