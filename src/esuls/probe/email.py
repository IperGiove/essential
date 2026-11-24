"""SPF / DMARC / DKIM / MX posture from DNS (needs dnspython, imported lazily)."""
from __future__ import annotations

import re
import time
from typing import Any, Optional, Sequence

from ._http import ProbeOptions, is_ip_or_local, record_stats, registrable_domain, split_host_port
from .report import ProbeReport

CHECK = "email"


def _dns():
    try:
        import dns.asyncresolver
        import dns.exception
        import dns.resolver
    except ModuleNotFoundError as e:
        raise ModuleNotFoundError(
            "esuls-probe email requires the optional 'probe' feature (dnspython) — install it "
            "with `pip install 'esuls[probe]'`.") from e
    return dns


async def _query(resolver: Any, name: str, rdtype: str) -> list:
    """Records as strings (TXT) or (pref, host) tuples (MX); [] when absent."""
    dns = _dns()
    try:
        ans = await resolver.resolve(name, rdtype)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return []
    out = []
    for r in ans:
        if rdtype == "MX":
            out.append((r.preference, str(r.exchange).rstrip(".")))
        else:
            out.append(b"".join(r.strings).decode("utf-8", "replace"))
    return out


async def check_email(
    target: str,
    options: Optional[ProbeOptions] = None,
    *,
    selectors: Sequence[str] = (),
    resolver: Any = None,
) -> ProbeReport:
    options = options or ProbeOptions()
    host, _ = split_host_port(target)
    report = ProbeReport(target=host or target)
    if not host or is_ip_or_local(host):
        report.add(CHECK, "info", host or target, "Email DNS check skipped",
                   "The target has no registrable domain (IP address or localhost).")
        return report
    dns = _dns()
    if resolver is None:
        resolver = dns.asyncresolver.Resolver()
        resolver.lifetime = options.timeout
    domain = registrable_domain(host)
    report.target = domain
    t0, nq, nfail = time.monotonic(), 0, 0

    async def q(name, rdtype):
        nonlocal nq, nfail
        nq += 1
        try:
            return await _query(resolver, name, rdtype)
        except Exception as e:  # noqa: BLE001 - timeouts, SERVFAIL, no nameservers
            nfail += 1
            report.errors.append(f"{CHECK}: {rdtype} {name}: {type(e).__name__}: {e}")
            return None

    mx = await q(domain, "MX")
    if mx is not None:
        if mx:
            report.add(CHECK, "info", domain, "MX records",
                       ", ".join(f"{h} (pref {p})" for p, h in sorted(mx)))
        else:
            report.add(CHECK, "info", domain, "No MX records", "The domain does not receive mail (or uses A fallback).")
    txt = await q(domain, "TXT")
    if txt is not None:
        spf = [t for t in txt if t.lower().startswith("v=spf1")]
        if not spf:
            report.add(CHECK, "medium", domain, "SPF record missing", "Anyone can send mail claiming to be this domain.")
        else:
            rec = spf[0]
            m = re.findall(r"(?:^|\s)([+\-~?]?)all(?:\s|$)", rec)
            q_ = m[-1] if m else None
            if q_ in ("+", ""):
                report.add(CHECK, "high", domain, "SPF allows everyone (+all)", rec)
            elif q_ == "?":
                report.add(CHECK, "low", domain, "SPF is neutral (?all)", rec)
            elif q_ in ("~", "-"):
                report.add(CHECK, "info", domain, f"SPF policy {q_}all", rec)
            else:
                report.add(CHECK, "info", domain, "SPF without an 'all' mechanism", rec)
            if len(spf) > 1:
                report.add(CHECK, "medium", domain, "Multiple SPF records", "RFC 7208 allows exactly one; receivers return permerror.")
    dm = await q(f"_dmarc.{domain}", "TXT")
    if dm is not None:
        rec = next((t for t in dm if t.lower().startswith("v=dmarc1")), None)
        if rec is None:
            report.add(CHECK, "medium", domain, "DMARC record missing", f"No TXT v=DMARC1 at _dmarc.{domain}.")
        else:
            pm = re.search(r"(?:^|;)\s*p\s*=\s*(\w+)", rec, re.I)
            pol = pm.group(1).lower() if pm else None
            if pol in ("quarantine", "reject"):
                report.add(CHECK, "info", domain, f"DMARC policy p={pol}", rec)
            else:
                report.add(CHECK, "low", domain, f"DMARC policy p={pol or 'missing'} (monitoring only)", rec)
    if not selectors:
        report.add(CHECK, "info", domain, "DKIM not checked",
                   "DKIM needs a selector; pass --selector NAME (repeatable). Selectors are not guessed.")
    for sel in selectors:
        recs = await q(f"{sel}._domainkey.{domain}", "TXT")
        if recs is None:
            continue
        ok = any("v=dkim1" in r.lower() or re.search(r"(^|;)\s*p=", r) for r in recs)
        report.add(CHECK, "info", domain, f"DKIM selector '{sel}' {'found' if ok else 'absent'}",
                   f"{sel}._domainkey.{domain}")
    record_stats(report, CHECK, nq, nfail, t0)
    return report
