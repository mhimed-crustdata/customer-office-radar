"""Tiny Crustdata API client (standard library only).

Docs: https://docs.crustdata.com
Every request sends `authorization: Bearer <key>` and `x-api-version: 2025-11-01`.
The key is read from the CRUSTDATA_API_KEY environment variable and never logged.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Iterator, List, Optional

BASE_URL = "https://api.crustdata.com"
API_VERSION = "2025-11-01"


class CrustdataError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"Crustdata API error {status}: {message}")
        self.status = status


def load_env(path: str = ".env") -> None:
    """Load KEY=VALUE lines from a .env file without overriding real env vars."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key.startswith("export "):
                key = key[len("export "):].strip()
            value = value.strip()
            if value[:1] in ('"', "'") and value[1:].find(value[0]) >= 0:
                value = value[1:1 + value[1:].find(value[0])]
            elif value.startswith("#"):
                value = ""
            else:
                value = value.split(" #", 1)[0].split("\t#", 1)[0].strip()
            os.environ.setdefault(key, value)


Transport = Callable[[str, Dict[str, Any], Dict[str, str], float], Any]


def _urllib_transport(url: str, body: Dict[str, Any], headers: Dict[str, str], timeout: float) -> Any:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as err:
        detail = err.read().decode("utf-8", "replace")[:500]
        raise CrustdataError(err.code, detail) from None


class Crustdata:
    """Minimal client. Pass `transport` in tests to avoid the network."""

    def __init__(self, api_key: Optional[str] = None, base_url: str = BASE_URL,
                 timeout: float = 90.0, transport: Optional[Transport] = None, max_retries: int = 3):
        self.api_key = api_key or os.environ.get("CRUSTDATA_API_KEY", "")
        if not self.api_key and transport is None:
            raise SystemExit("CRUSTDATA_API_KEY is not set. Copy .env.example to .env and add your key.")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.transport = transport or _urllib_transport
        self.max_retries = max_retries

    def post(self, path: str, body: Dict[str, Any]) -> Any:
        headers = {
            "authorization": f"Bearer {self.api_key}",
            "content-type": "application/json",
            "x-api-version": API_VERSION,
        }
        for attempt in range(self.max_retries + 1):
            try:
                return self.transport(self.base_url + path, body, headers, self.timeout)
            except CrustdataError as err:
                retryable = err.status == 429 or err.status >= 500
                if not retryable or attempt == self.max_retries:
                    raise
                time.sleep(2 ** attempt * 2)
        raise AssertionError("unreachable")

    def paginate(self, path: str, body: Dict[str, Any], items_key: str,
                 max_results: int, page_size: int = 100) -> Iterator[Dict[str, Any]]:
        """Yield results across cursor pages until `max_results` or the last page."""
        sent = 0
        cursor = None
        while sent < max_results:
            page = dict(body, limit=min(page_size, max_results - sent))
            if cursor:
                page["cursor"] = cursor
            data = self.post(path, page)
            items: List[Dict[str, Any]] = data.get(items_key) or []
            for item in items:
                yield item
                sent += 1
            cursor = data.get("next_cursor")
            if not cursor or not items:
                break

    # Endpoint helpers (all real, documented endpoints)
    def company_search(self, filters: Dict[str, Any], fields: List[str], max_results: int = 100):
        return self.paginate("/company/search", {"filters": filters, "fields": fields}, "companies", max_results)

    def job_search(self, filters: Dict[str, Any], fields: List[str], max_results: int = 200,
                   sorts: Optional[List[Dict[str, str]]] = None):
        body: Dict[str, Any] = {"filters": filters, "fields": fields}
        if sorts:
            body["sorts"] = sorts
        return self.paginate("/job/search", body, "job_listings", max_results)

    def watch_company(self, body: Dict[str, Any]) -> Any:
        """Create an entity watch on companies (POST /watch/company)."""
        return self.post("/watch/company", body)


def normalize_domain(url_or_domain: Optional[str]) -> str:
    """'https://www.Stripe.com/about?x=1' -> 'stripe.com'."""
    if not url_or_domain:
        return ""
    d = url_or_domain.strip().lower()
    for prefix in ("https://", "http://"):
        if d.startswith(prefix):
            d = d[len(prefix):]
    d = d.split("/")[0].split("?")[0].split("#")[0]
    if d.startswith("www."):
        d = d[4:]
    return d


def day(value: Optional[str]) -> str:
    """'2026-08-01T00:00:00+00:00' -> '2026-08-01' ('' when missing)."""
    return (value or "")[:10]
