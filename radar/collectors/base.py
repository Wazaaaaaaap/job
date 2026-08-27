"""Shared HTTP plumbing for API-family collectors."""
from __future__ import annotations

import json
import logging
import random
import time
from typing import Any

import requests

log = logging.getLogger("radar.http")

UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
]


class Http:
    def __init__(self, timeout: int = 30, retries: int = 3, delay: float = 1.0):
        self.s = requests.Session()
        self.timeout = timeout
        self.retries = retries
        self.delay = delay

    def _headers(self, extra: dict | None = None) -> dict:
        h = {
            "User-Agent": random.choice(UA_POOL),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
        }
        if extra:
            h.update(extra)
        return h

    def request(self, method: str, url: str, *, headers=None, json_body=None, params=None) -> Any:
        last = None
        for attempt in range(self.retries):
            try:
                r = self.s.request(method, url, headers=self._headers(headers),
                                   json=json_body, params=params, timeout=self.timeout)
                if r.status_code in (429, 502, 503):
                    time.sleep(self.delay * (2 ** attempt) + random.random())
                    last = f"HTTP {r.status_code}"
                    continue
                r.raise_for_status()
                ct = r.headers.get("content-type", "")
                if "json" in ct:
                    return r.json()
                try:
                    return json.loads(r.text)
                except Exception:
                    return r.text
            except Exception as e:  # noqa: BLE001
                last = f"{type(e).__name__}: {e}"
                time.sleep(self.delay * (2 ** attempt) + random.random())
        raise RuntimeError(f"request failed after {self.retries} attempts: {method} {url} -> {last}")

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def post(self, url, **kw):
        return self.request("POST", url, **kw)


class Collector:
    """Every collector implements .collect() -> list[Job] and never raises upward."""

    family = "generic"

    def __init__(self, cfg: dict, http: Http | None = None):
        self.cfg = cfg
        self.http = http or Http()
        self.name = cfg.get("name") or cfg.get("id") or self.family

    def collect(self):  # pragma: no cover - interface
        raise NotImplementedError

    def safe_collect(self) -> tuple[list, str | None]:
        try:
            jobs = list(self.collect())
            return jobs, None
        except Exception as e:  # noqa: BLE001
            log.warning("collector %s failed: %s", self.name, e)
            return [], f"{type(e).__name__}: {e}"
