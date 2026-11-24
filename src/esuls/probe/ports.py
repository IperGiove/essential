"""TCP connect scan (stdlib asyncio): what does the internet actually reach?

A completed connection is "open", a refusal "closed", a timeout "filtered".
Sockets are closed immediately and nothing is sent, with one exception: on the
Redis port a single ``PING`` tells an open-to-the-world instance from one that
demands AUTH. No commands are ever run.
"""
from __future__ import annotations

import asyncio
import socket
import time
from typing import Optional, Sequence

from ..utils import run_parallel
from . import banner as _banner
from ._http import ProbeOptions, record_stats, split_host_port
from .report import ProbeReport

CHECK = "ports"

# port -> (service guess, dangerous when internet-exposed)
PORTS: dict[int, tuple[str, bool]] = {
    21: ("ftp", False), 22: ("ssh", False), 23: ("telnet", True), 25: ("smtp", False),
    53: ("dns", False), 80: ("http", False), 110: ("pop3", False), 111: ("rpcbind", False),
    135: ("msrpc", True), 139: ("netbios", True), 143: ("imap", False), 443: ("https", False),
    445: ("smb", True), 465: ("smtps", False), 587: ("smtp submission", False),
    993: ("imaps", False), 995: ("pop3s", False), 1433: ("mssql", True), 1521: ("oracle", True),
    2019: ("caddy admin API", True), 2375: ("docker API (plain)", True), 3000: ("dev server", False),
    3306: ("mysql", True), 3389: ("rdp", True), 5432: ("postgresql", True),
    5601: ("kibana", False), 5672: ("rabbitmq amqp", True), 5900: ("vnc", True),
    6379: ("redis", True), 8000: ("http-alt", False), 8080: ("http-proxy/alt", False),
    8443: ("https-alt", False), 8888: ("http-alt", False), 9090: ("prometheus", True),
    9100: ("node_exporter", False), 9200: ("elasticsearch", True), 11211: ("memcached", True),
    15672: ("rabbitmq management", True), 27017: ("mongodb", True),
}
DEFAULT_PORTS = sorted(PORTS)


def parse_ports(spec: str) -> list[int]:
    """'22,80,8000-8010' -> sorted unique ports. Raises ValueError on junk."""
    out: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        lo, _, hi = part.partition("-")
        a, b = int(lo), int(hi or lo)
        if not (1 <= a <= b <= 65535):
            raise ValueError(f"invalid port range {part!r}")
        out.update(range(a, b + 1))
    if not out:
        raise ValueError("no ports given")
    return sorted(out)


async def _connect(host: str, port: int, timeout: float, delay: float) -> str:
    if delay > 0:
        await asyncio.sleep(delay)
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except ConnectionRefusedError:
        return "closed"
    except (asyncio.TimeoutError, TimeoutError):
        return "filtered"
    except socket.gaierror as e:
        return f"error:{e}"
    except OSError:                      # unreachable / reset: not answering
        return "filtered"
    w.close()
    try:
        await w.wait_closed()
    except Exception:  # noqa: BLE001
        pass
    return "open"


async def _redis_auth_state(host: str, port: int, timeout: float) -> str:
    """'noauth' | 'auth' | 'unknown' from one PING."""
    try:
        r, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except Exception:  # noqa: BLE001
        return "unknown"
    try:
        w.write(b"PING\r\n")
        await w.drain()
        data = await asyncio.wait_for(r.read(64), timeout)
    except Exception:  # noqa: BLE001
        return "unknown"
    finally:
        w.close()
    if data.startswith(b"+PONG"):
        return "noauth"
    if data.startswith(b"-NOAUTH") or b"auth" in data.lower():
        return "auth"
    return "unknown"


async def check_ports(
    target: str,
    options: Optional[ProbeOptions] = None,
    *,
    ports: Optional[Sequence[int]] = None,
    redis_ports: Sequence[int] = (6379,),
    banner: bool = True,
    plaintext_auth: bool = False,
    service_map: Optional[dict] = None,
) -> ProbeReport:
    """banner: fingerprint known service ports (read-only). plaintext_auth: opt-in
    extra step asking whether TLS is enforced before login (never sends credentials)."""
    options = options or ProbeOptions()
    host, url_port = split_host_port(target)
    report = ProbeReport(target=host or target)
    if not host:
        report.errors.append(f"{CHECK}: no host in target {target!r}")
        return report
    plist = list(ports) if ports is not None else list(DEFAULT_PORTS)
    if ports is None and url_port and url_port not in plist:
        plist.append(url_port)               # a port named in the URL is always scanned
    t0 = time.monotonic()
    results = await run_parallel(*[_connect(host, p, options.timeout, options.delay) for p in plist],
                                 limit=max(options.concurrency, 1))
    states = {p: (r if isinstance(r, str) else "filtered") for p, r in zip(plist, results)}
    errs = [v for v in states.values() if v.startswith("error:")]
    if len(errs) == len(plist):
        report.errors.append(f"{CHECK}: cannot resolve {host} ({errs[0][6:]})")
    open_ports = [p for p, v in states.items() if v == "open"]
    smap = dict(_banner.SERVICES if service_map is None else service_map)
    fps: dict = {}
    if banner:
        todo = [p for p in open_ports if p in smap or p in _banner.IMPLICIT_TLS]

        async def fp(p):
            if p in smap:
                return await _banner.fingerprint(host, p, smap[p], options.timeout, plaintext_auth)
            return await _banner.implicit_tls(host, p, _banner.IMPLICIT_TLS[p], options.timeout)
        for p, r in zip(todo, await run_parallel(*[fp(p) for p in todo], limit=max(options.concurrency, 1))):
            if isinstance(r, _banner.BannerResult):
                fps[p] = r
    for p in open_ports:
        svc, dangerous = PORTS.get(p, ("unknown service", False))
        sev, title, detail = ("medium" if dangerous else "info"), f"Open port {p}/tcp ({svc})", ""
        if dangerous:
            detail = f"{svc} is dangerous when reachable from the internet; bind it to localhost/VPN or firewall it."
        if p in redis_ports:
            state = await _redis_auth_state(host, p, options.timeout)
            if state == "noauth":
                sev, title = "high", f"Redis reachable without auth on {p}/tcp"
                detail = "PING answered +PONG without authentication: anyone can read/write the datastore."
            elif state == "auth":
                detail = "Redis demands AUTH (PING refused); still should not face the internet."
        if p in fps and fps[p].banner:
            detail = (detail + " " if detail else "") + f"banner: {fps[p].banner}"
        report.add(CHECK, sev, f"{host}:{p}", title, detail)
        if p in fps:
            for fsev, ftitle, fdetail in fps[p].findings:
                report.add(CHECK, fsev, f"{host}:{p}", ftitle, fdetail)
    counts = {k: sum(1 for v in states.values() if v == k) for k in ("open", "closed", "filtered")}
    record_stats(report, CHECK, len(plist) + sum(1 for p in open_ports if p in redis_ports), 0, t0)
    report.stats[CHECK].update(counts, open_ports=open_ports)
    return report
