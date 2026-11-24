"""CORS misconfiguration checks (reflected origin, null, wildcard + credentials)."""
from __future__ import annotations

import uuid
from typing import Optional

import httpx

from ._http import ProbeOptions, Resp, Session
from .report import ProbeReport

CHECK = "cors"


def _eval(report: ProbeReport, kind: str, origin: str, r: Resp) -> None:
    acao = r.headers.get("access-control-allow-origin")
    creds = r.headers.get("access-control-allow-credentials", "").lower() == "true"
    if acao is None:
        return
    ev = f"Origin: {origin} -> ACAO: {acao}; ACAC: {'true' if creds else 'absent/false'}"
    if acao == "*" and creds:
        report.add(CHECK, "medium", r.url, f"Wildcard origin with credentials ({kind})",
                   "ACAO '*' together with ACAC true is invalid and signals a broken CORS policy.", ev)
    elif origin == "null" and acao == "null":
        report.add(CHECK, "high" if creds else "medium", r.url, f"Origin 'null' is allowed ({kind})",
                   "Sandboxed iframes / local files can read responses"
                   + (" with credentials." if creds else "."), ev)
    elif origin != "null" and acao == origin:
        report.add(CHECK, "high" if creds else "medium", r.url, f"Arbitrary Origin is reflected ({kind})",
                   "Any website can read responses"
                   + (" using the visitor's credentials." if creds else " (no credentials)."), ev)


async def check_cors(
    target: str,
    options: Optional[ProbeOptions] = None,
    *,
    client: Optional[httpx.AsyncClient] = None,
    path: str = "/",
) -> ProbeReport:
    options = options or ProbeOptions()
    report = ProbeReport(target=target)
    evil = f"https://{uuid.uuid4().hex[:10]}.attacker.example"
    async with Session(target, options, client) as s:
        report.target = s.target
        r = await s.fetch(path, headers={"Origin": evil}, max_bytes=4096)
        if r:
            _eval(report, "GET", evil, r)
        r = await s.fetch(path, headers={"Origin": "null"}, max_bytes=4096)
        if r:
            _eval(report, "GET", "null", r)
        r = await s.fetch(path, method="OPTIONS", max_bytes=4096, headers={
            "Origin": evil, "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization"})
        if r:
            _eval(report, "preflight", evil, r)
        s.finalize(report, CHECK)
    return report
