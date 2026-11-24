"""Run every check and merge the results into one report."""
from __future__ import annotations

import dataclasses
import ipaddress
from typing import Iterable, Optional, Sequence

import httpx

from ._http import ProbeOptions, normalize_target
from .auth import check_auth
from .email import check_email
from .ports import check_ports
from .tls import check_tls
from .cors import check_cors
from .dnscheck import check_dns
from .exposed import check_exposed
from .headers import check_headers
from .methods import check_methods
from .paths import check_paths
from .ratelimit import check_ratelimit
from .report import ProbeReport

# auth goes last: it deliberately triggers lockouts/bans against the source IP.
ALL_CHECKS = ("headers", "exposed", "cors", "methods", "paths", "ratelimit", "ports", "tls", "email", "dns", "auth")


HTTP_CHECKS = ("headers", "exposed", "cors", "methods", "paths", "ratelimit", "auth")
_XFF_BASE = "203.0.113"          # TEST-NET-3 documentation range
_XFF_FIRST_HOST = 11


def _xff_for_checks(options: ProbeOptions, names: Sequence[str]) -> dict[str, str]:
    """One distinct simulated client IP per HTTP check.

    Policy: if the user passed their own X-Forwarded-For (an IPv4), it is used
    verbatim for the FIRST HTTP check and the others vary the last octet inside
    that /24; otherwise the 203.0.113.0/24 range is used (.11, .12, ...)."""
    base, first = _XFF_BASE, None
    for k, v in options.headers.items():
        if k.lower() == "x-forwarded-for":
            try:
                ip = ipaddress.ip_address(v.split(",")[0].strip())
            except ValueError:
                break
            if ip.version == 4:
                base, first = str(ip).rsplit(".", 1)[0], str(ip)
            break
    out: dict[str, str] = {}
    for i, name in enumerate(n for n in names if n in HTTP_CHECKS):
        out[name] = first if (i == 0 and first) else f"{base}.{_XFF_FIRST_HOST + i}"
    return out


def _with_xff(options: ProbeOptions, ip: str) -> ProbeOptions:
    headers = {k: v for k, v in options.headers.items() if k.lower() != "x-forwarded-for"}
    headers["X-Forwarded-For"] = ip
    return dataclasses.replace(options, headers=headers)


def _skipped(target: str, check: str, why: str) -> ProbeReport:
    r = ProbeReport(target=target)
    r.add(check, "info", target, f"{check} check skipped", why)
    return r


async def run_full_probe(
    target: str,
    options: Optional[ProbeOptions] = None,
    *,
    client: Optional[httpx.AsyncClient] = None,
    wordlist: Optional[Iterable[str]] = None,
    extensions: Sequence[str] = (),
    recursive: bool = False,
    depth: int = 2,
    ratelimit_path: str = "/",
    ratelimit_method: str = "GET",
    ratelimit_data: Optional[str] = None,
    ratelimit_requests: int = 60,
    ports: Optional[Sequence[int]] = None,
    banner: bool = True,
    tls_port: Optional[int] = None,
    selectors: Sequence[str] = (),
    auth_path: str = "/login",
    auth_method: str = "POST",
    auth_user_field: str = "username",
    auth_pass_field: str = "password",
    auth_username: str = "esuls-probe@invalid.example",
    auth_content_type: str = "form",
    auth_max_attempts: int = 25,
    checks: Sequence[str] = ALL_CHECKS,
) -> ProbeReport:
    """A check that raises is recorded as an error; the others still run."""
    options = options or ProbeOptions()
    report = ProbeReport(target=normalize_target(target))
    runners = {
        "headers": lambda o: check_headers(target, o, client=client),
        "exposed": lambda o: check_exposed(target, o, client=client),
        "cors": lambda o: check_cors(target, o, client=client),
        "methods": lambda o: check_methods(target, o, client=client),
        "paths": lambda o: check_paths(target, o, client=client, wordlist=wordlist,
                                     extensions=extensions, recursive=recursive, depth=depth),
        "ratelimit": lambda o: check_ratelimit(target, o, client=client, path=ratelimit_path,
                                             method=ratelimit_method, data=ratelimit_data,
                                             requests=ratelimit_requests),
        "ports": lambda o: check_ports(target, o, ports=ports, banner=banner),
        "tls": lambda o: (_skipped(target, "tls", "target is plain http://; no TLS to assess")
                        if target.strip().lower().startswith("http://") and not tls_port
                        else check_tls(target, o, port=tls_port)),
        "dns": lambda o: check_dns(target, o),
        "email": lambda o: check_email(target, o, selectors=selectors),
        "auth": lambda o: check_auth(target, o, client=client, path=auth_path, method=auth_method,
                                   user_field=auth_user_field, pass_field=auth_pass_field,
                                   username=auth_username, content_type=auth_content_type,
                                   max_attempts=auth_max_attempts),
    }
    xffs = _xff_for_checks(options, checks)
    for name in checks:
        try:
            res = runners[name](_with_xff(options, xffs[name]) if name in xffs else options)
            report.merge(await res if hasattr(res, "__await__") else res)
        except Exception as e:  # noqa: BLE001 - isolate each check
            report.errors.append(f"{name}: {type(e).__name__}: {e}")
    return report
