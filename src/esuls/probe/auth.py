"""Does the login lockout fire? Send wrong credentials until a defense reacts, then STOP."""
from __future__ import annotations

import json
import re
import uuid
from typing import Optional
from urllib.parse import urlencode

import httpx

from ._http import ProbeOptions, Session
from .ratelimit import FORWARDING_HEADERS, is_loopback_host
from ._http import host_of
from .report import ProbeReport

CHECK = "auth"
LOCKOUT_RE = re.compile(r"too many|locked|lockout|try again (later|in)|rate.?limit|temporarily|blocked|banned",
                        re.I)


async def check_auth(
    target: str,
    options: Optional[ProbeOptions] = None,
    *,
    client: Optional[httpx.AsyncClient] = None,
    path: str = "/login",
    method: str = "POST",
    user_field: str = "username",
    pass_field: str = "password",
    username: str = "esuls-probe@invalid.example",
    content_type: str = "form",
    max_attempts: int = 25,
) -> ProbeReport:
    """Serial wrong-credential attempts; stops at the first defensive response.

    Defense = 429 (always), 403/503/423 or a lockout message after an earlier
    normal answer. Never point this at systems you do not own: it can trigger
    real bans (fail2ban, WAF) against your IP."""
    options = options or ProbeOptions()
    report = ProbeReport(target=target)
    async with Session(target, options, client) as s:
        report.target = s.target
        url = s.url(path)
        first_body = ""
        seen_normal = False
        statuses: list[int] = []
        fired = None
        for i in range(1, max_attempts + 1):
            fields = {user_field: username, pass_field: f"wrong-{uuid.uuid4().hex[:10]}"}
            if content_type == "json":
                body, ct = json.dumps(fields), "application/json"
            else:
                body, ct = urlencode(fields), "application/x-www-form-urlencoded"
            r = await s.fetch(path, method=method.upper(), data=body,
                              headers={"Content-Type": ct}, max_bytes=8192)
            if r is None:
                continue
            statuses.append(r.status)
            text = r.text + " " + r.headers.get("location", "")
            if i == 1 or not first_body:
                first_body = text
            lock_msg = bool(LOCKOUT_RE.search(text)) and not LOCKOUT_RE.search(first_body) and seen_normal
            if r.status == 429 or (seen_normal and (r.status in (403, 503, 423) or lock_msg)):
                fired = (len(statuses), r)
                break
            if r.status not in (403, 429, 503, 404, 405):
                seen_normal = True
        n = len(statuses)
        if fired:
            k, r = fired
            ra = r.headers.get("retry-after")
            report.add(CHECK, "info", url, f"Login lockout fired at attempt #{k} ({r.status})",
                       f"Stopped immediately after the defense responded. Statuses so far: {statuses}"
                       + (f"; Retry-After: {ra}" if ra else ""))
        elif n:
            dist = ", ".join(sorted({str(x) for x in statuses}))
            if not seen_normal:
                report.add(CHECK, "info", url,
                           f"Login path answers {dist} to every attempt; lockout could not be assessed",
                           f"{method.upper()} {path} x{n}: the request is refused or the path does not exist. "
                           "Check --path, --method, --user-field/--pass-field and --content-type.")
            elif is_loopback_host(host_of(s.target)) and not (
                    FORWARDING_HEADERS & {k.lower() for k in options.headers}):
                report.add(CHECK, "info", url, f"No login lockout observed on a loopback target ({n} attempts)",
                           f"Statuses {statuses}. The target is loopback and the server may exempt local "
                           "traffic from rate limiting on purpose. Re-run with "
                           "-H 'X-Forwarded-For: 203.0.113.7' to simulate a remote client.")
            else:
                report.add(CHECK, "medium", url, f"No login lockout / rate limit after {n} attempts",
                           f"{method.upper()} {path}: wrong credentials were answered normally every time "
                           f"(statuses {sorted(set(statuses))}).")
        s.finalize(report, CHECK)
    return report
