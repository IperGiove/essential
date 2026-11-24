"""Authoritative-DNS posture: open resolver, zone transfer (AXFR), DNSSEC.

Outside view, read-only: one recursive query and one AXFR attempt per
authoritative nameserver. Needs dnspython (imported lazily, like `email`)."""
from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Optional

from ..utils import run_parallel
from ._http import ProbeOptions, is_ip_or_local, record_stats, registrable_domain, split_host_port
from .email import _dns
from .report import ProbeReport

CHECK = "dns"
PROBE_NAME = "example.com"          # an external name this zone is not authoritative for
AXFR_RECORD_CAP = 5000


def recursion_probe(ip: str, timeout: float, port: int = 53) -> str:
    """'recursive' | 'refused' | 'no-answer' | 'error: ...' for one RD=1 query."""
    dns = _dns()
    import dns.flags, dns.message, dns.query, dns.rcode
    q = dns.message.make_query(PROBE_NAME, "A")           # RD flag set by default
    try:
        try:
            resp = dns.query.udp(q, ip, timeout=timeout, port=port)
        except dns.message.Truncated:
            resp = dns.query.tcp(q, ip, timeout=timeout, port=port)
        except dns.exception.Timeout:
            resp = dns.query.tcp(q, ip, timeout=timeout, port=port)   # UDP filtered/lost: TCP fallback
    except Exception as e:  # noqa: BLE001
        return f"error: {type(e).__name__}"
    if resp.rcode() == dns.rcode.REFUSED:
        return "refused"
    if resp.rcode() == dns.rcode.NOERROR and resp.answer and (resp.flags & dns.flags.RA) \
            and not (resp.flags & dns.flags.AA):
        return "recursive"
    return "no-answer"


def axfr_probe(ip: str, domain: str, timeout: float, port: int = 53) -> tuple[str, int, list[str]]:
    """('ok', n_records, sample_names) | ('refused', 0, []) | ('error', 0, [why])."""
    dns = _dns()
    import dns.query
    count, names = 0, []
    try:
        for msg in dns.query.xfr(ip, domain, port=port, timeout=timeout, lifetime=timeout * 2):
            for rrset in msg.answer:
                count += len(rrset)
                n = str(rrset.name)
                if n not in names and len(names) < 5:
                    names.append(n)
            if count >= AXFR_RECORD_CAP:
                break
    except Exception as e:  # noqa: BLE001 - TransferError(REFUSED), timeouts, resets
        if count:
            return "ok", count, names
        return ("refused" if "REFUSED" in str(e).upper() or "TransferError" in type(e).__name__
                else "error"), 0, [f"{type(e).__name__}: {e}"]
    return ("ok", count, names) if count else ("refused", 0, [])


async def _resolve(resolver: Any, name: str, rdtype: str) -> Optional[list[str]]:
    """[] when absent, None when the lookup itself failed."""
    dns = _dns()
    try:
        ans = await resolver.resolve(name, rdtype)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return []
    except Exception:  # noqa: BLE001 - SERVFAIL, timeout, no nameservers
        return None
    return [str(r).rstrip(".") for r in ans]


async def check_dns(
    target: str,
    options: Optional[ProbeOptions] = None,
    *,
    resolver: Any = None,
    recursion: Optional[Callable[[str, float], str]] = None,
    axfr: Optional[Callable[[str, str, float], tuple]] = None,
) -> ProbeReport:
    options = options or ProbeOptions()
    host, _ = split_host_port(target)
    report = ProbeReport(target=host or target)
    if not host or is_ip_or_local(host):
        report.add(CHECK, "info", host or target, "DNS check skipped",
                   "The target has no registrable domain (IP address or localhost).")
        return report
    dns = _dns()
    if resolver is None:
        resolver = dns.asyncresolver.Resolver()
        resolver.lifetime = options.timeout
    recursion = recursion or recursion_probe
    axfr = axfr or axfr_probe
    domain = registrable_domain(host)
    report.target = domain
    t0 = time.monotonic()
    ns_names = await _resolve(resolver, domain, "NS")
    if not ns_names:
        report.add(CHECK, "info", domain, "No NS records found" if ns_names == [] else "NS lookup failed",
                   "Authoritative nameservers could not be determined; open-resolver and AXFR tests skipped.")
        record_stats(report, CHECK, 1, 0, t0)
        return report
    servers: dict[str, str] = {}                    # ip -> label
    for ns in ns_names:
        for ip in await _resolve(resolver, ns, "A") or []:
            servers.setdefault(ip, ns)
    host_ips: list[str] = []
    if host != domain:                              # the target host itself, if it answers on 53
        host_ips = [ip for ip in await _resolve(resolver, host, "A") or [] if ip not in servers]
    report.add(CHECK, "info", domain, f"Authoritative nameservers: {', '.join(sorted(ns_names))}",
               "; ".join(f"{label} = {ip}" for ip, label in servers.items()) or "no A records for the nameservers")
    t = options.timeout
    all_ips = list(servers) + host_ips
    rec = await run_parallel(*[asyncio.to_thread(recursion, ip, t) for ip in all_ips],
                             limit=max(options.concurrency, 1))
    for ip, state in zip(all_ips, rec):
        label = servers.get(ip, host)
        if state == "recursive":
            report.add(CHECK, "high", f"{ip}:53", "Open DNS resolver (amplification risk)",
                       f"{label} ({ip}) answers recursive queries for {PROBE_NAME}, a zone it is not "
                       "authoritative for; restrict recursion to your own clients.")
        elif isinstance(state, BaseException):
            report.add(CHECK, "info", f"{ip}:53", "Recursion test failed", f"{type(state).__name__}: {state}")
    xf = await run_parallel(*[asyncio.to_thread(axfr, ip, domain, t) for ip in servers],
                            limit=max(options.concurrency, 1))
    for ip, res in zip(servers, xf):
        if isinstance(res, BaseException):
            report.add(CHECK, "info", f"{ip}:53", "AXFR attempt failed", f"{type(res).__name__}: {res}")
            continue
        status, count, samples = res
        if status == "ok":
            report.add(CHECK, "high", f"{ip}:53", "DNS zone transfer allowed",
                       f"{servers[ip]} ({ip}) handed out {count} record(s) via AXFR; "
                       f"sample names: {', '.join(samples[:5])}")
        else:
            report.add(CHECK, "info", f"{ip}:53", f"AXFR {status}", f"{servers[ip]} ({ip})")
    dnskey = await _resolve(resolver, domain, "DNSKEY")
    ds = await _resolve(resolver, domain, "DS")
    if dnskey or ds:
        report.add(CHECK, "info", domain, "DNSSEC present",
                   f"DNSKEY: {len(dnskey or [])}, DS: {len(ds or [])}")
    elif dnskey is None and ds is None:
        report.add(CHECK, "info", domain, "DNSSEC lookup failed", "DNSKEY/DS queries errored (SERVFAIL/timeout).")
    else:
        report.add(CHECK, "low", domain, "No DNSSEC", "No DNSKEY/DS records: answers cannot be validated.")
    record_stats(report, CHECK, 3 + len(servers) * 2 + len(host_ips), 0, t0)
    return report
