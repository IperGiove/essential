"""Does the target ever push back on a burst of requests?"""
from __future__ import annotations

import ipaddress
from collections import Counter
from typing import Optional

import httpx

from ..utils import run_parallel
from ._http import ProbeOptions, Resp, Session, host_of
from .report import ProbeReport

CHECK = "ratelimit"
LIMIT_STATUSES = (429, 403, 503)
FORWARDING_HEADERS = {"x-forwarded-for", "forwarded", "x-real-ip"}


def is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost" or host.lower().endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


async def check_ratelimit(
    target: str,
    options: Optional[ProbeOptions] = None,
    *,
    client: Optional[httpx.AsyncClient] = None,
    path: str = "/",
    method: str = "GET",
    data: Optional[str] = None,
    requests: int = 60,
) -> ProbeReport:
    options = options or ProbeOptions()
    report = ProbeReport(target=target)
    async with Session(target, options, client) as s:
        report.target = s.target
        url = s.url(path)
        results = await run_parallel(
            *[s.fetch(path, method=method.upper(), data=data, max_bytes=4096) for _ in range(requests)],
            limit=max(options.concurrency, 1),
        )
        resps = [r if isinstance(r, Resp) else None for r in results]
        dist = Counter(r.status for r in resps if r is not None)
        got = sum(dist.values())
        if got:
            summary = ", ".join(f"{k}: {v}" for k, v in sorted(dist.items()))
            # 429 always means limiting. 403/503 only count after the path
            # answered normally (status below 400) earlier in the burst.
            first = None
            seen_normal = False
            for i, r in enumerate(resps):
                if r is None:
                    continue
                if r.status == 429 or (r.status in (403, 503) and seen_normal):
                    first = i
                    break
                if r.status < 400:
                    seen_normal = True
            if first is None and not seen_normal:
                # Every answer was a 4xx/5xx other than a limiter: the request
                # itself is being refused (blocked path, wrong --data, ...).
                st = ", ".join(str(k) for k in sorted(dist))
                what = f"Path answers {st} to every request" if len(dist) == 1 else f"Path answers only error statuses ({st})"
                report.add(CHECK, "info", url, f"{what}; rate limiting could not be assessed",
                           f"{method.upper()} {path} x{requests}: status distribution {{{summary}}}. "
                           "No request was answered normally, so a limiter cannot be told apart. "
                           "Check --path, --method, --data and headers (a client error such as 422 "
                           "usually means the request is malformed).")
            elif first is None:
                loopback = is_loopback_host(host_of(s.target)) and not (
                    FORWARDING_HEADERS & {k.lower() for k in options.headers})
                detail = (f"{method.upper()} {path} x{requests} at concurrency {options.concurrency}; "
                          f"status distribution {{{summary}}}. Nothing returned 429/403/503.")
                if loopback:
                    report.add(CHECK, "info", url, f"No rate limiting observed on a loopback target ({got} requests)",
                               detail + " The target is loopback and the server may exempt local traffic "
                               "from rate limiting on purpose. Re-run with "
                               "-H 'X-Forwarded-For: 203.0.113.7' to simulate a remote client.")
                else:
                    report.add(CHECK, "medium", url, f"No rate limiting observed ({got} requests)", detail)
            else:
                st = resps[first].status
                ra = resps[first].headers.get("retry-after")
                report.add(CHECK, "info", url, f"Rate limiting kicked in at request #{first + 1} ({st})",
                           f"Status distribution {{{summary}}}"
                           + (f"; Retry-After: {ra}" if ra else "; no Retry-After header"))
        s.finalize(report, CHECK)
    return report
