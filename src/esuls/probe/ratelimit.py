"""Does the target ever push back on a burst of requests?"""
from __future__ import annotations

from collections import Counter
from typing import Optional

import httpx

from ..utils import run_parallel
from ._http import ProbeOptions, Resp, Session
from .report import ProbeReport

CHECK = "ratelimit"
LIMIT_STATUSES = (429, 403, 503)


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
            # answered normally (not 403/503/429) earlier in the burst.
            first = None
            seen_normal = False
            for i, r in enumerate(resps):
                if r is None:
                    continue
                if r.status == 429 or (r.status in (403, 503) and seen_normal):
                    first = i
                    break
                if r.status not in LIMIT_STATUSES:
                    seen_normal = True
            only_blocked = not seen_normal and first is None and set(dist) <= {403, 503}
            if only_blocked:
                st = ", ".join(str(k) for k in sorted(dist))
                report.add(CHECK, "info", url, f"Path answers {st} to every request; rate limiting could not be assessed",
                           f"{method.upper()} {path} x{requests}: status distribution {{{summary}}}. "
                           "The path is blocked/erroring from the first request, so a limiter cannot be told apart. "
                           "Try another --path.")
            elif first is None:
                report.add(CHECK, "medium", url, f"No rate limiting observed ({got} requests)",
                           f"{method.upper()} {path} x{requests} at concurrency {options.concurrency}; "
                           f"status distribution {{{summary}}}. Nothing returned 429/403/503.")
            else:
                st = resps[first].status
                ra = resps[first].headers.get("retry-after")
                report.add(CHECK, "info", url, f"Rate limiting kicked in at request #{first + 1} ({st})",
                           f"Status distribution {{{summary}}}"
                           + (f"; Retry-After: {ra}" if ra else "; no Retry-After header"))
        s.finalize(report, CHECK)
    return report
