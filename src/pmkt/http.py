"""HTTP client with retry/backoff, per-host throttle and v2 cursor pagination."""
from __future__ import annotations

import logging
import random
import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)

RETRY_STATUS = {429, 500, 502, 503, 504}


class Http:
    def __init__(self, timeout_s: float = 30, max_retries: int = 5, min_interval_s: float = 0.12):
        self.client = httpx.Client(timeout=timeout_s, headers={"User-Agent": "pmkt-research/0.1"})
        self.max_retries = max_retries
        self.min_interval_s = min_interval_s
        self._last: dict[str, float] = {}

    def _throttle(self, url: str) -> None:
        host = urlparse(url).netloc
        wait = self._last.get(host, 0) + self.min_interval_s - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last[host] = time.monotonic()

    def request(self, method: str, url: str, **kw: Any) -> Any:
        for attempt in range(self.max_retries + 1):
            self._throttle(url)
            try:
                r = self.client.request(method, url, **kw)
                if r.status_code in RETRY_STATUS:
                    raise httpx.HTTPStatusError(f"retryable {r.status_code}", request=r.request, response=r)
                r.raise_for_status()
                return r.json()
            except (httpx.TransportError, httpx.HTTPStatusError) as e:
                status = getattr(getattr(e, "response", None), "status_code", None)
                if status is not None and status not in RETRY_STATUS:
                    raise
                if attempt == self.max_retries:
                    raise
                ra = getattr(getattr(e, "response", None), "headers", {}).get("retry-after")
                delay = float(ra) if ra else min(30.0, 0.5 * 2**attempt) * (0.5 + random.random())
                log.warning("retry %s %s in %.1fs (%s)", method, url, delay, e)
                time.sleep(delay)
        raise RuntimeError("unreachable")

    def get(self, url: str, params: dict | None = None) -> Any:
        return self.request("GET", url, params=params)

    def post(self, url: str, json: Any) -> Any:
        return self.request("POST", url, json=json)

    def paginate_v2(self, url: str, params: dict, max_pages: int = 1000) -> Iterator[dict]:
        """Data API v2: payload in `data`, follow `pagination.next_cursor` until null."""
        params = dict(params)
        for _ in range(max_pages):
            body = self.get(url, params)
            data = body.get("data") or []
            yield from data
            cur = (body.get("pagination") or {}).get("next_cursor")
            if not cur:
                return
            params["cursor"] = cur
        log.warning("paginate_v2 hit max_pages=%d for %s", max_pages, url)
