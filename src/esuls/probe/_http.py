"""Shared HTTP plumbing for the probe checks.

Probing needs raw status codes, NO redirect following, NO retries on 4xx/5xx
and exact request headers. ``esuls.request_cli`` (AsyncRequest/make_request)
follows redirects and retries by design, so the probe talks to ``httpx``
directly (the ``http`` extra already provides it).
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlsplit

import httpx

DEFAULT_USER_AGENT = "esuls-probe/1 (self-assessment)"


@dataclass
class ProbeOptions:
    """Options shared by every check."""

    concurrency: int = 10
    timeout: float = 10.0
    delay: float = 0.0                       # seconds slept before each request
    headers: dict = field(default_factory=dict)
    cookie: Optional[str] = None
    user_agent: str = DEFAULT_USER_AGENT
    insecure: bool = False                   # skip TLS verification


@dataclass
class Resp:
    url: str
    status: int
    headers: httpx.Headers
    body: bytes
    size: int                                # Content-Length if sent, else len(body)

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


def normalize_target(target: str) -> str:
    target = target.strip()
    if "://" not in target:
        target = "https://" + target
    return target.rstrip("/")


class Session:
    """Counts requests, applies options, never raises on transport errors."""

    def __init__(self, target: str, options: Optional[ProbeOptions] = None,
                 client: Optional[httpx.AsyncClient] = None) -> None:
        self.target = normalize_target(target)
        self.options = options or ProbeOptions()
        self._client = client
        self._own = client is None
        self.requests = 0
        self.failures = 0
        self.errors: list[str] = []
        self._t0 = time.monotonic()

    async def __aenter__(self) -> "Session":
        if self._client is None:
            o = self.options
            self._client = httpx.AsyncClient(
                follow_redirects=False,
                verify=not o.insecure,
                timeout=o.timeout,
                limits=httpx.Limits(max_connections=max(o.concurrency, 1) * 2),
            )
        self._t0 = time.monotonic()
        return self

    async def __aexit__(self, *exc) -> None:
        if self._own and self._client is not None:
            await self._client.aclose()

    def url(self, path: str = "") -> str:
        if path.startswith(("http://", "https://")):
            return path
        return self.target + "/" + path.lstrip("/")

    def _headers(self, extra: Optional[dict]) -> dict:
        o = self.options
        h = {"User-Agent": o.user_agent, "Accept": "*/*"}
        h.update(o.headers)
        if o.cookie:
            h["Cookie"] = o.cookie
        if extra:
            h.update(extra)
        return h

    async def fetch(self, path: str = "", *, method: str = "GET",
                    headers: Optional[dict] = None, data: Optional[str | bytes] = None,
                    max_bytes: int = 262144) -> Optional[Resp]:
        """One request; returns None on transport failure (recorded in errors)."""
        assert self._client is not None, "use `async with Session(...)`"
        if self.options.delay > 0:
            await asyncio.sleep(self.options.delay)
        url = self.url(path)
        self.requests += 1
        try:
            async with self._client.stream(
                method, url, headers=self._headers(headers), content=data,
                timeout=self.options.timeout,
            ) as r:
                chunks, total = [], 0
                async for chunk in r.aiter_bytes():
                    chunks.append(chunk)
                    total += len(chunk)
                    if total >= max_bytes:
                        break
                body = b"".join(chunks)[:max_bytes]
                cl = r.headers.get("content-length")
                size = int(cl) if cl and cl.isdigit() else len(body)
                return Resp(url, r.status_code, r.headers, body, size)
        except Exception as e:  # noqa: BLE001 - probing must never abort on one URL
            self.failures += 1
            if len(self.errors) < 5:
                self.errors.append(f"{method} {url}: {type(e).__name__}: {e}")
            return None

    def finalize(self, report, check: str) -> None:
        """Record per-check stats; flag a fully unreachable target as an error."""
        report.stats[check] = {
            "requests": self.requests,
            "failures": self.failures,
            "duration_s": round(time.monotonic() - self._t0, 3),
        }
        if self.requests and self.failures == self.requests:
            report.errors.append(f"{check}: target unreachable ({self.errors[0] if self.errors else 'no response'})")


def host_of(url: str) -> str:
    return urlsplit(url).hostname or ""
