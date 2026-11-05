"""Small HTTP client: timeouts, retry with backoff, per-host rate limiting."""
from __future__ import annotations

import threading
import time
from urllib.parse import urlparse

import requests


class DataUnavailable(Exception):
    """Raised when a data source cannot answer. Callers treat this as fail-closed."""


class RateLimiter:
    def __init__(self, per_minute: int):
        self.interval = 60.0 / per_minute
        self.lock = threading.Lock()
        self.next_ok = 0.0

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            if now < self.next_ok:
                time.sleep(self.next_ok - now)
                now = time.monotonic()
            self.next_ok = now + self.interval


# conservative per-host limits (requests/minute)
HOST_LIMITS = {
    "api.dexscreener.com": 200,
    "api.geckoterminal.com": 25,
    "api.rugcheck.xyz": 60,
    "api.mainnet-beta.solana.com": 80,
    "lite-api.jup.ag": 50,
}


class Http:
    def __init__(self, timeout: float = 15.0, retries: int = 3):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "memecoin-desk/0.1"
        self.timeout = timeout
        self.retries = retries
        self.limiters: dict[str, RateLimiter] = {}
        self.lock = threading.Lock()

    def _limiter(self, url: str) -> RateLimiter:
        host = urlparse(url).hostname or ""
        with self.lock:
            if host not in self.limiters:
                self.limiters[host] = RateLimiter(HOST_LIMITS.get(host, 60))
            return self.limiters[host]

    def request(self, method: str, url: str, **kw):
        last = None
        for attempt in range(self.retries):
            self._limiter(url).wait()
            try:
                r = self.s.request(method, url, timeout=self.timeout, **kw)
                if r.status_code == 429 or r.status_code >= 500:
                    last = f"HTTP {r.status_code}"
                    time.sleep(2 ** attempt * 2)
                    continue
                if r.status_code >= 400:
                    raise DataUnavailable(f"{url}: HTTP {r.status_code}")
                return r.json()
            except (requests.RequestException, ValueError) as e:
                last = repr(e)
                time.sleep(2 ** attempt)
        raise DataUnavailable(f"{url}: {last}")

    def get(self, url: str, **kw):
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw):
        return self.request("POST", url, **kw)
