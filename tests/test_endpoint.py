"""The RPC endpoint: resolved from 1Password, and never written down anywhere.

A paid endpoint carries its credential *inside the URL* -- Helius in the query, Alchemy and
QuickNode in the path -- and this client's errors travel into events.jsonl and into kill details
on every 429. So two things are tested here: the URL is resolved into the process and not stored,
and no code path can emit it.
"""
import dataclasses
import subprocess
import time

import pytest
from desk.config import PUBLIC_RPC, Config, SecretUnavailable, read_secret
from desk.http import DataUnavailable, Http, redact

KEY = "sk-live-NEVER-IN-A-LOG-0123456789"
KEYED = {
    "helius (query)": f"https://mainnet.helius-rpc.com/?api-key={KEY}",
    "alchemy (path)": f"https://solana-mainnet.g.alchemy.com/v2/{KEY}",
    "quicknode (path)": f"https://x.solana-mainnet.quiknode.pro/{KEY}/",
    "userinfo": f"https://user:{KEY}@rpc.example.com/v1",
}


@pytest.mark.parametrize("where,url", KEYED.items(), ids=list(KEYED))
def test_redaction_keeps_the_host_and_nothing_that_can_hold_a_secret(where, url):
    out = redact(url)
    assert KEY not in out and "user" not in out
    assert out.startswith("https://") and out.endswith("/…")


def test_the_public_endpoint_survives_redaction_intact():
    """Redaction must not make ordinary errors useless: a URL with nothing to hide keeps its host."""
    assert redact(PUBLIC_RPC) == PUBLIC_RPC


@pytest.fixture
def no_backoff(monkeypatch):
    """The retry backoff is real seconds; this suite is about strings."""
    monkeypatch.setattr(time, "sleep", lambda s: None)


@pytest.mark.parametrize("url", list(KEYED.values()))
@pytest.mark.parametrize("status", [401, 429, 500])
def test_no_http_failure_can_leak_the_credential(url, status, no_backoff):
    class Response:
        status_code = status
        def json(self): return {}

    class Session:
        headers = {}
        def request(self, *a, **kw): return Response()

    http = Http(timeout=0.01, retries=1)
    http.s = Session()
    http.limiters = {}
    with pytest.raises(DataUnavailable) as e:
        http.post(url, json={})
    assert KEY not in str(e.value)


def test_a_transport_error_does_not_leak_it_either(no_backoff):
    """repr() of a requests exception embeds the full URL it was given."""
    import requests

    class Session:
        headers = {}
        def request(self, *a, **kw):
            raise requests.ConnectionError(f"failed to reach {KEYED['helius (query)']}")

    http = Http(timeout=0.01, retries=1)
    http.s = Session()
    http.limiters = {}
    with pytest.raises(DataUnavailable) as e:
        http.post(KEYED["helius (query)"], json={})
    assert KEY not in str(e.value)


# ------------------------------------------------------------------ resolution
def test_with_no_reference_the_env_url_is_used():
    cfg = dataclasses.replace(Config(), solana_rpc_url="https://rpc.example.com", solana_rpc_url_ref="")
    assert cfg.rpc_url() == "https://rpc.example.com"
    assert cfg.rpc_source() == "SOLANA_RPC_URL"


def test_the_default_says_the_rpc_provider_cannot_answer():
    cfg = dataclasses.replace(Config(), solana_rpc_url=PUBLIC_RPC, solana_rpc_url_ref="")
    assert "RpcHolders cannot answer" in cfg.rpc_source()


def test_a_reference_is_read_through_the_1password_cli(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout=f"https://mainnet.helius-rpc.com/?api-key={KEY}\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    cfg = dataclasses.replace(Config(), solana_rpc_url_ref="op://Esamatic/Helius/url")
    assert cfg.rpc_url().endswith(KEY)
    assert seen["cmd"] == ["op", "read", "--no-newline", "op://Esamatic/Helius/url"]


def test_the_source_line_names_the_reference_and_not_the_url(monkeypatch):
    """A startup line is printed and a reference is not a secret; the URL it resolves to is."""
    cfg = dataclasses.replace(Config(), solana_rpc_url_ref="op://Esamatic/Helius/url")
    assert cfg.rpc_source() == "1Password (op://Esamatic/Helius/url)"
    assert KEY not in cfg.rpc_source()


@pytest.mark.parametrize("ref", ["https://mainnet.helius-rpc.com/?api-key=x", "/run/secrets/rpc", "plain"])
def test_only_an_op_reference_is_accepted(ref):
    """Anything else is a URL or a path, i.e. the secret itself or a file holding it."""
    with pytest.raises(SecretUnavailable, match="op://"):
        dataclasses.replace(Config(), solana_rpc_url_ref=ref).rpc_url()


def test_a_reference_that_cannot_be_read_raises_instead_of_falling_back(monkeypatch):
    """Falling back to the public endpoint quietly would put R5 back on one intermittent field
    with nothing in the log to say why."""
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 1, stdout="", stderr="[ERROR] could not read secret: item not found\n"))
    cfg = dataclasses.replace(Config(), solana_rpc_url_ref="op://Esamatic/Helius/url")
    with pytest.raises(SecretUnavailable, match="item not found"):
        cfg.rpc_url()


def test_a_missing_op_cli_says_what_to_do_instead(monkeypatch):
    def boom(cmd, **kw):
        raise FileNotFoundError("op")

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(SecretUnavailable, match="SOLANA_RPC_URL"):
        read_secret("op://Esamatic/Helius/url")


def test_an_empty_secret_is_not_a_url(monkeypatch):
    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="  \n", stderr=""))
    with pytest.raises(SecretUnavailable, match="returned nothing"):
        read_secret("op://Esamatic/Helius/url")


# ------------------------------------------------------------------ the helper that stores it
def test_a_bare_key_becomes_a_helius_url_whose_redaction_hides_it():
    from tools.store_rpc_endpoint import endpoint_url

    url = endpoint_url(KEY)
    assert url == f"https://mainnet.helius-rpc.com/?api-key={KEY}"
    assert KEY not in redact(url)


def test_a_full_url_from_another_provider_is_taken_as_given():
    from tools.store_rpc_endpoint import endpoint_url

    assert endpoint_url(KEYED["alchemy (path)"]) == KEYED["alchemy (path)"]


@pytest.mark.parametrize("bad,why", [
    ("", "niente"),
    ("   ", "niente"),
    ("http://mainnet.helius-rpc.com/?api-key=x", "https"),
    ("https:///no-host", "URL"),
    ("key with spaces", "spazi"),
])
def test_what_the_helper_refuses_to_store(bad, why):
    from tools.store_rpc_endpoint import endpoint_url

    with pytest.raises(ValueError, match=why):
        endpoint_url(bad)
