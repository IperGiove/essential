"""Subdomain enumeration via Certificate Transparency + dangling-CNAME takeover check.

The enumeration queries crt.sh, NOT the target. Per discovered subdomain we resolve
the CNAME and, when it dangles (NXDOMAIN) or points at a known third-party service
whose "unclaimed" page matches, flag a possible takeover (one GET to the subdomain).
Your own headers/cookie are never forwarded to crt.sh or to third-party hosts."""
from __future__ import annotations

import asyncio
import dataclasses
import json
import time
from typing import Any, Optional
from urllib.parse import quote

import httpx

from ..utils import run_parallel
from ._http import ProbeOptions, Resp, Session, is_ip_or_local, registrable_domain, split_host_port
from .email import _dns
from .report import ProbeReport

CHECK = "subdomains"
CRT_URL = "https://crt.sh/?q=%25.{domain}&output=json"
LIST_CAP = 50
RESOLVE_CAP = 300

# (CNAME suffixes, body fingerprint, service)
FINGERPRINTS = [
    (("github.io",), "There isn't a GitHub Pages site here", "GitHub Pages"),
    (("s3.amazonaws.com", "amazonaws.com"), "NoSuchBucket", "AWS S3"),
    (("herokuapp.com", "herokudns.com", "herokuapp.com."), "No such app", "Heroku"),
    (("azurewebsites.net", "cloudapp.net", "cloudapp.azure.com", "trafficmanager.net",
      "blob.core.windows.net", "azureedge.net"), "404 Web Site not found", "Azure"),
    (("netlify.app", "netlify.com"), "Not Found - Request ID", "Netlify"),
    (("bitbucket.io",), "Repository not found", "Bitbucket"),
    (("ghost.io",), "The thing you were looking for is no longer here", "Ghost"),
    (("surge.sh",), "project not found", "Surge"),
    (("readme.io",), "Project doesnt exist", "Readme"),
    (("pantheonsite.io",), "The gods are wise", "Pantheon"),
    (("zendesk.com",), "Help Center Closed", "Zendesk"),
    (("myshopify.com",), "Sorry, this shop is currently unavailable", "Shopify"),
    (("fastly.net",), "Fastly error: unknown domain", "Fastly"),
]


CT_ATTEMPTS = 3
CT_BACKOFF = 1.5            # seconds before attempt 2; doubles before attempt 3
RETRY_STATUSES = (502, 503, 504)


async def _backoff(delay: float) -> None:
    await asyncio.sleep(delay)


async def _fetch_ct(s: Session, url: str) -> tuple[Optional[Resp], int]:
    """crt.sh is flaky (502s, timeouts): retry those, never a 200 or a 4xx.
    Bounded: CT_ATTEMPTS tries, each limited by the user's --timeout."""
    r: Optional[Resp] = None
    for attempt in range(1, CT_ATTEMPTS + 1):
        r = await s.fetch(url, max_bytes=8_000_000)
        if r is not None and r.status not in RETRY_STATUSES:
            return r, attempt
        if attempt < CT_ATTEMPTS:
            await _backoff(CT_BACKOFF * 2 ** (attempt - 1))
    return r, CT_ATTEMPTS


def parse_ct(body: str, domain: str) -> list[str]:
    """Names from crt.sh JSON, lowercased, wildcard-stripped, in-scope, deduped."""
    rows = json.loads(body)
    names: set[str] = set()
    for row in rows if isinstance(rows, list) else []:
        for n in str(row.get("name_value", "")).splitlines():
            n = n.strip().lower().lstrip("*.").rstrip(".")
            if n and (n == domain or n.endswith("." + domain)) and " " not in n and "@" not in n:
                names.add(n)
    return sorted(names)


async def _cname(resolver: Any, name: str) -> Optional[str]:
    dns = _dns()
    try:
        ans = await resolver.resolve(name, "CNAME")
    except Exception:  # noqa: BLE001 - NXDOMAIN/NoAnswer/timeouts: nothing to judge
        return None
    return next((str(r).rstrip(".").lower() for r in ans), None)


async def _exists(resolver: Any, name: str) -> Optional[bool]:
    """True/False for resolves/NXDOMAIN, None when undecidable (timeout, SERVFAIL)."""
    dns = _dns()
    for rd in ("A", "AAAA"):
        try:
            await resolver.resolve(name, rd)
            return True
        except dns.resolver.NoAnswer:
            return True                     # the name exists, just no such record type
        except dns.resolver.NXDOMAIN:
            return False
        except Exception:  # noqa: BLE001
            return None
    return None


async def check_subdomains(
    target: str,
    options: Optional[ProbeOptions] = None,
    *,
    client: Optional[httpx.AsyncClient] = None,
    resolver: Any = None,
    list_cap: int = LIST_CAP,
) -> ProbeReport:
    options = options or ProbeOptions()
    host, _ = split_host_port(target)
    report = ProbeReport(target=host or target)
    if not host or is_ip_or_local(host):
        report.add(CHECK, "info", host or target, "Subdomain check skipped",
                   "The target has no registrable domain (IP address or localhost).")
        return report
    dns = _dns()
    if resolver is None:
        resolver = dns.asyncresolver.Resolver()
        resolver.lifetime = options.timeout
    domain = registrable_domain(host)
    report.target = domain
    # third-party requests: never forward the user's headers/cookie
    clean = dataclasses.replace(options, headers={}, cookie=None)
    async with Session("https://crt.sh", clean, client) as s:
        r, attempts = await _fetch_ct(s, CRT_URL.format(domain=quote(domain)))
        names: list[str] = []
        if r is None or r.status != 200:
            report.add(CHECK, "info", "https://crt.sh", "CT lookup failed",
                       f"crt.sh answered {r.status if r else 'no response'} after {attempts} attempt(s); subdomains could "
                       "not be enumerated (crt.sh is often slow or rate-limited, retry later).")
        else:
            try:
                names = parse_ct(r.text, domain)
            except (ValueError, AttributeError):
                report.add(CHECK, "info", "https://crt.sh", "CT lookup failed", "crt.sh returned unparseable JSON.")
        if names:
            shown = names[:list_cap]
            report.add(CHECK, "info", domain, f"{len(names)} subdomain(s) in Certificate Transparency logs",
                       ", ".join(shown) + (f" ... (+{len(names) - len(shown)} more)" if len(names) > len(shown) else ""))
        subs = [n for n in names if n != domain][:RESOLVE_CAP]
        cnames = await run_parallel(*[_cname(resolver, n) for n in subs], limit=max(options.concurrency, 1))
        for sub, cn in zip(subs, cnames):
            if not isinstance(cn, str) or cn == sub:
                continue
            exists = await _exists(resolver, cn)
            if exists is False:
                report.add(CHECK, "high", f"https://{sub}", "Possible subdomain takeover",
                           f"{sub} is a CNAME to {cn}, which does not exist (NXDOMAIN): anyone who can register "
                           "that name controls this subdomain.")
                continue
            fp = next((f for f in FINGERPRINTS if any(cn == x or cn.endswith("." + x) for x in f[0])), None)
            if fp is None:
                continue
            resp: Optional[Resp] = await s.fetch(f"http://{sub}/", max_bytes=65536)
            if resp is not None and 300 <= resp.status < 400 and resp.headers.get("location", "").startswith("https://"):
                resp = await s.fetch(resp.headers["location"], max_bytes=65536)
            if resp is not None and fp[1].lower() in resp.text.lower():
                report.add(CHECK, "high", f"https://{sub}", "Possible subdomain takeover",
                           f"{sub} is a CNAME to {cn} ({fp[2]}) and the page says '{fp[1]}': the service "
                           "account/resource is unclaimed.")
        s.finalize(report, CHECK)
    return report
