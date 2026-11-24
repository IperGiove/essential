"""Run every check and merge the results into one report."""
from __future__ import annotations

from typing import Iterable, Optional, Sequence

import httpx

from ._http import ProbeOptions, normalize_target
from .auth import check_auth
from .email import check_email
from .ports import check_ports
from .tls import check_tls
from .cors import check_cors
from .exposed import check_exposed
from .headers import check_headers
from .paths import check_paths
from .ratelimit import check_ratelimit
from .report import ProbeReport

# auth goes last: it deliberately triggers lockouts/bans against the source IP.
ALL_CHECKS = ("headers", "exposed", "cors", "paths", "ratelimit", "ports", "tls", "email", "auth")


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
        "headers": lambda: check_headers(target, options, client=client),
        "exposed": lambda: check_exposed(target, options, client=client),
        "cors": lambda: check_cors(target, options, client=client),
        "paths": lambda: check_paths(target, options, client=client, wordlist=wordlist,
                                     extensions=extensions, recursive=recursive, depth=depth),
        "ratelimit": lambda: check_ratelimit(target, options, client=client, path=ratelimit_path,
                                             method=ratelimit_method, data=ratelimit_data,
                                             requests=ratelimit_requests),
        "ports": lambda: check_ports(target, options, ports=ports),
        "tls": lambda: (_skipped(target, "tls", "target is plain http://; no TLS to assess")
                        if target.strip().lower().startswith("http://") and not tls_port
                        else check_tls(target, options, port=tls_port)),
        "email": lambda: check_email(target, options, selectors=selectors),
        "auth": lambda: check_auth(target, options, client=client, path=auth_path, method=auth_method,
                                   user_field=auth_user_field, pass_field=auth_pass_field,
                                   username=auth_username, content_type=auth_content_type,
                                   max_attempts=auth_max_attempts),
    }
    for name in checks:
        try:
            res = runners[name]()
            report.merge(await res if hasattr(res, "__await__") else res)
        except Exception as e:  # noqa: BLE001 - isolate each check
            report.errors.append(f"{name}: {type(e).__name__}: {e}")
    return report
