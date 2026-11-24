"""Security-header, cookie-flag and HTTPS-redirect checks."""
from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlsplit

import httpx

from ._http import ProbeOptions, Session
from .report import ProbeReport

CHECK = "headers"


async def check_headers(
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
        https = s.target.startswith("https://")
        r = await s.fetch("/")
        if r is not None:
            h = r.headers
            csp = h.get("content-security-policy", "")

            def add(sev, title, detail=""):
                report.add(CHECK, sev, url, title, detail)

            if https and "strict-transport-security" not in h:
                add("medium", "Missing Strict-Transport-Security (HSTS)",
                    "Browsers can be downgraded to plain HTTP.")
            if not csp:
                add("medium", "Missing Content-Security-Policy", "No CSP limits script/content sources (XSS mitigation).")
            if h.get("x-content-type-options", "").lower() != "nosniff":
                add("low", "Missing X-Content-Type-Options: nosniff", "MIME sniffing is not disabled.")
            if "x-frame-options" not in h and "frame-ancestors" not in csp.lower():
                add("medium", "Missing clickjacking protection",
                    "Neither X-Frame-Options nor CSP frame-ancestors is set.")
            if "referrer-policy" not in h:
                add("low", "Missing Referrer-Policy", "Full URLs may leak in the Referer header.")
            if "permissions-policy" not in h:
                add("low", "Missing Permissions-Policy", "Browser features are not explicitly restricted.")
            server = h.get("server", "")
            if re.search(r"\d+\.\d+", server) or ("/" in server and re.search(r"\d", server)):
                add("low", "Server header discloses version", f"Server: {server}")
            if "x-powered-by" in h:
                add("low", "X-Powered-By discloses technology", f"X-Powered-By: {h['x-powered-by']}")
            _check_cookies(report, url, h.get_list("set-cookie"), https)

        # http:// must redirect to https://
        if https:
            parts = urlsplit(s.target)
            host = parts.hostname or ""
            http_url = f"http://{host}{parts.path}/"
            r2 = await s.fetch(http_url)
            if r2 is None:
                report.add(CHECK, "info", http_url, "Plain HTTP not reachable",
                           "http:// variant did not answer (fine if port 80 is closed on purpose).")
            elif not (300 <= r2.status < 400 and r2.headers.get("location", "").lower().startswith("https://")):
                report.add(CHECK, "medium", http_url, "HTTP does not redirect to HTTPS",
                           f"http:// answered {r2.status} without redirecting to https://.")
        elif r is not None and not (300 <= r.status < 400 and r.headers.get("location", "").lower().startswith("https://")):
            report.add(CHECK, "medium", url, "Site served over plain HTTP without redirect to HTTPS",
                       f"Target is http:// and answered {r.status} without redirecting to https://.")
        s.finalize(report, CHECK)
    return report


def _check_cookies(report: ProbeReport, url: str, cookies: list[str], https: bool) -> None:
    missing: dict[str, list[str]] = {"Secure": [], "HttpOnly": [], "SameSite": []}
    for c in cookies:
        name = c.split("=", 1)[0].strip()
        attrs = [a.strip().lower() for a in c.split(";")[1:]]
        if https and "secure" not in attrs:
            missing["Secure"].append(name)
        if "httponly" not in attrs:
            missing["HttpOnly"].append(name)
        if not any(a.startswith("samesite=") for a in attrs):
            missing["SameSite"].append(name)
    sev = {"Secure": "medium", "HttpOnly": "medium", "SameSite": "low"}
    for flag, names in missing.items():
        if names:
            report.add(CHECK, sev[flag], url, f"Cookie(s) without {flag} flag",
                       "Cookies: " + ", ".join(names))
