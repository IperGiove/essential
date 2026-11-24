"""Advertised HTTP methods (OPTIONS) and TRACE. Never sends a write method."""
from __future__ import annotations

import uuid
from typing import Optional

import httpx

from ._http import ProbeOptions, Session
from .report import ProbeReport

CHECK = "methods"
DANGEROUS = ("PUT", "DELETE", "CONNECT", "PATCH", "TRACE")


async def check_methods(
    target: str,
    options: Optional[ProbeOptions] = None,
    *,
    client: Optional[httpx.AsyncClient] = None,
) -> ProbeReport:
    options = options or ProbeOptions()
    report = ProbeReport(target=target)
    async with Session(target, options, client) as s:
        report.target = s.target
        url = s.url("/")
        r = await s.fetch("/", method="OPTIONS", max_bytes=4096)
        if r is not None:
            adv: list[str] = []
            for h in ("allow", "access-control-allow-methods"):
                for part in r.headers.get(h, "").split(","):
                    m = part.strip().upper()
                    if m and m not in adv:
                        adv.append(m)
            if adv:
                report.add(CHECK, "info", url, f"Advertised methods: {', '.join(adv)}",
                           f"OPTIONS / answered {r.status}")
            for m in DANGEROUS:
                if m in adv:
                    report.add(CHECK, "medium", url, f"Dangerous HTTP method advertised: {m}",
                               "Advertised in Allow / Access-Control-Allow-Methods, not confirmed writable "
                               "(no write request was sent).")
        token = uuid.uuid4().hex
        t = await s.fetch("/", method="TRACE", headers={"X-Probe-Trace": token}, max_bytes=8192)
        if t is not None and t.status == 200 and token in t.text:
            report.add(CHECK, "medium", url, "TRACE enabled (XST)",
                       "TRACE echoed the request including its headers back to the client "
                       "(cross-site tracing can expose cookies/credentials headers).")
        s.finalize(report, CHECK)
    return report
