"""Small HTTP helper shared by stages. Generic User-Agent; no personal details."""
from __future__ import annotations

import time

import httpx

USER_AGENT = "Mozilla/5.0 (compatible; content-network/0.1)"


class FetchError(RuntimeError):
    pass


class _Retryable(Exception):
    pass


def fetch(url: str, timeout: float = 20.0, retries: int = 2) -> tuple[bytes, str]:
    """GET a URL and return (body, final_url). Retries timeouts and 5xx, not 4xx."""
    last = ""
    for attempt in range(retries + 1):
        try:
            with httpx.Client(
                timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT}
            ) as client:
                r = client.get(url)
            if r.status_code >= 500:
                raise _Retryable(f"HTTP {r.status_code}")
            if r.status_code >= 400:
                raise FetchError(f"{url}: HTTP {r.status_code}")
            return r.content, str(r.url)
        except (_Retryable, httpx.HTTPError) as e:
            last = f"{type(e).__name__}: {e}"
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))
    raise FetchError(f"{url}: {last}")
