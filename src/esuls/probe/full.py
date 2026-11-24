"""Run every check and merge the results into one report."""
from __future__ import annotations

from typing import Iterable, Optional, Sequence

import httpx

from ._http import ProbeOptions, normalize_target
from .cors import check_cors
from .exposed import check_exposed
from .headers import check_headers
from .paths import check_paths
from .ratelimit import check_ratelimit
from .report import ProbeReport

ALL_CHECKS = ("headers", "exposed", "cors", "paths", "ratelimit")


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
    }
    for name in checks:
        try:
            report.merge(await runners[name]())
        except Exception as e:  # noqa: BLE001 - isolate each check
            report.errors.append(f"{name}: {type(e).__name__}: {e}")
    return report
