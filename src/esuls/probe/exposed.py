"""Curated sensitive-file checks, each guarded by a content validator.

A catch-all site that answers 200 to everything must NOT produce findings, so
a path is only reported when the body actually looks like the real artifact.
Evidence never contains secret values: key names, magic bytes and sizes only.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import Callable, Optional

import httpx

from ..utils import run_parallel
from ._http import ProbeOptions, Resp, Session
from .report import ProbeReport

CHECK = "exposed"

Validator = Callable[[Resp], Optional[str]]   # returns redacted evidence, or None


def _is_html(r: Resp) -> bool:
    head = r.body[:512].lstrip().lower()
    return head.startswith((b"<!doctype", b"<html", b"<head", b"<body"))


def _v_git_head(r: Resp) -> Optional[str]:
    m = re.match(r"(ref: refs/\S+|[0-9a-f]{40})\s*$", r.text.strip())
    return "HEAD points to " + ("a ref" if m and m.group(1).startswith("ref") else "a commit") if m else None


def _v_git_config(r: Resp) -> Optional[str]:
    t = r.text
    if "[core]" in t and ("repositoryformatversion" in t or "[remote" in t or "[branch" in t):
        return "git config sections: " + ", ".join(re.findall(r"^\[([^\]\s]+)", t, re.M)[:6])
    return None


def _v_env(r: Resp) -> Optional[str]:
    if _is_html(r):
        return None
    keys = re.findall(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=", r.text, re.M)
    if len(keys) >= 2:
        return f"{len(keys)} KEY=VALUE lines (keys: {', '.join(keys[:8])}; values redacted)"
    return None


def _magic(magic: bytes, label: str) -> Validator:
    def v(r: Resp) -> Optional[str]:
        return f"{label} (magic bytes {magic[:4].hex()}), {r.size} bytes" if r.body.startswith(magic) else None
    return v


def _v_svn_entries(r: Resp) -> Optional[str]:
    t = r.text
    if not _is_html(r) and re.match(r"\d+\s*\n", t) and re.search(r"^(dir|file)\s*$", t, re.M):
        return "svn entries listing"
    return None


def _v_htpasswd(r: Resp) -> Optional[str]:
    if _is_html(r):
        return None
    lines = [ln for ln in r.text.splitlines() if re.match(
        r"^[^:\s]+:(\$apr1\$|\{SHA\}|\$2[aby]\$|\$[156]\$|[./A-Za-z0-9]{13}$)", ln)]
    return f"{len(lines)} credential line(s) (hashes redacted)" if lines else None


def _v_sql(r: Resp) -> Optional[str]:
    if _is_html(r):
        return None
    if re.search(r"CREATE\s+TABLE|INSERT\s+INTO", r.text[:262144], re.I):
        return f"SQL dump statements present, {r.size} bytes"
    return None


def _contains(*needles: str, label: str, html_ok: bool = True) -> Validator:
    def v(r: Resp) -> Optional[str]:
        t = r.text
        return label if all(n in t for n in needles) else None
    return v


def _v_wpconfig(r: Resp) -> Optional[str]:
    t = r.text
    if "<?php" in t and re.search(r"DB_(NAME|PASSWORD|USER)", t):
        return "wp-config source with DB_* constants (values redacted)"
    return None


def _v_compose(r: Resp) -> Optional[str]:
    t = r.text
    if not _is_html(r) and re.search(r"^services:\s*$", t, re.M) and re.search(r"^\s+(image|build):", t, re.M):
        return "docker-compose services definition"
    return None


def _v_privkey(r: Resp) -> Optional[str]:
    m = re.search(r"-----BEGIN ([A-Z ]*)PRIVATE KEY-----", r.text)
    return f"private key header: BEGIN {m.group(1)}PRIVATE KEY (body redacted)" if m else None


def _v_actuator_env(r: Resp) -> Optional[str]:
    try:
        d = json.loads(r.text)
    except ValueError:
        return None
    if isinstance(d, dict) and ("propertySources" in d or "activeProfiles" in d):
        return "Spring actuator environment JSON (property values redacted)"
    return None


def _v_sftp(r: Resp) -> Optional[str]:
    try:
        d = json.loads(r.text)
    except ValueError:
        return None
    if isinstance(d, dict) and "host" in d and any(k in d for k in ("username", "password", "privateKeyPath")):
        return "sftp config keys: " + ", ".join(sorted(d)[:8]) + " (values redacted)"
    return None


def _v_npmrc(r: Resp) -> Optional[str]:
    return "npm auth token line (redacted)" if re.search(r"_authToken\s*=", r.text) and not _is_html(r) else None


def _v_aws(r: Resp) -> Optional[str]:
    return "AWS credentials keys (redacted)" if re.search(r"aws_access_key_id\s*=", r.text, re.I) else None


def _v_security_txt(r: Resp) -> Optional[str]:
    if _is_html(r) or r.size > 65536:
        return None
    m = re.search(r"^Contact:\s*(\S+)", r.text, re.M | re.I)
    return "Contact field present" if m else None


@dataclass(frozen=True)
class Exposure:
    path: str
    title: str
    severity: str
    validator: Validator


_ARCHIVES = ["backup", "www", "site", "website", "public_html", "db", "database", "dump", "htdocs", "web"]

EXPOSURES: list[Exposure] = [
    Exposure(".git/HEAD", "Exposed git repository (.git/HEAD)", "high", _v_git_head),
    Exposure(".git/config", "Exposed git config (.git/config)", "high", _v_git_config),
    Exposure(".git/index", "Exposed git index (.git/index)", "high", _magic(b"DIRC", "git index")),
    Exposure(".env", "Exposed environment file (.env)", "high", _v_env),
    Exposure(".env.local", "Exposed environment file (.env.local)", "high", _v_env),
    Exposure(".env.production", "Exposed environment file (.env.production)", "high", _v_env),
    Exposure(".env.bak", "Exposed environment backup (.env.bak)", "high", _v_env),
    Exposure(".DS_Store", "Exposed macOS directory metadata (.DS_Store)", "low",
             _magic(b"\x00\x00\x00\x01Bud1", ".DS_Store")),
    Exposure(".svn/entries", "Exposed Subversion metadata (.svn/entries)", "medium", _v_svn_entries),
    Exposure(".svn/wc.db", "Exposed Subversion database (.svn/wc.db)", "medium",
             _magic(b"SQLite format 3", "SQLite database")),
    Exposure(".htpasswd", "Exposed password file (.htpasswd)", "high", _v_htpasswd),
    Exposure(".npmrc", "Exposed npm config (.npmrc)", "high", _v_npmrc),
    Exposure(".aws/credentials", "Exposed AWS credentials", "high", _v_aws),
    *[Exposure(n, f"Exposed SQL dump ({n})", "high", _v_sql)
      for n in ("dump.sql", "backup.sql", "db.sql", "database.sql", "data.sql", "mysql.sql")],
    *[Exposure(f"{n}.zip", f"Exposed backup archive ({n}.zip)", "high", _magic(b"PK\x03\x04", "zip archive"))
      for n in _ARCHIVES],
    *[Exposure(f"{n}.tar.gz", f"Exposed backup archive ({n}.tar.gz)", "high", _magic(b"\x1f\x8b", "gzip archive"))
      for n in _ARCHIVES[:4]],
    Exposure("backup.gz", "Exposed backup archive (backup.gz)", "high", _magic(b"\x1f\x8b", "gzip archive")),
    Exposure("phpinfo.php", "Exposed phpinfo()", "medium", _contains("PHP Version", label="phpinfo output")),
    Exposure("info.php", "Exposed phpinfo()", "medium", _contains("PHP Version", label="phpinfo output")),
    Exposure("server-status", "Apache server-status exposed", "medium",
             _contains("Apache Server Status", label="Apache server-status page")),
    Exposure("server-info", "Apache server-info exposed", "medium",
             _contains("Apache Server Information", label="Apache server-info page")),
    *[Exposure(n, f"Exposed WordPress config backup ({n})", "high", _v_wpconfig)
      for n in ("wp-config.php.bak", "wp-config.php~", "wp-config.php.old", "wp-config.php.save",
                "wp-config.php.orig", "wp-config.bak")],
    Exposure("docker-compose.yml", "Exposed docker-compose.yml", "medium", _v_compose),
    Exposure("docker-compose.yaml", "Exposed docker-compose.yaml", "medium", _v_compose),
    Exposure("id_rsa", "Exposed SSH private key (id_rsa)", "high", _v_privkey),
    Exposure(".ssh/id_rsa", "Exposed SSH private key (.ssh/id_rsa)", "high", _v_privkey),
    Exposure("actuator/env", "Spring Boot actuator /env exposed", "high", _v_actuator_env),
    Exposure(".well-known/security.txt", "security.txt present (good practice)", "info", _v_security_txt),
    Exposure("security.txt", "security.txt present (good practice)", "info", _v_security_txt),
    Exposure(".vscode/sftp.json", "Exposed VS Code SFTP config", "high", _v_sftp),
]


def _v_debug_page(r: Resp) -> Optional[str]:
    t = r.text
    if "You're seeing this error because you have" in t and "DEBUG = True" in t:
        return "Django debug page (DEBUG = True) on a random 404"
    if "Werkzeug Debugger" in t or "The debugger caught an exception" in t:
        return "Flask/Werkzeug interactive debugger on a random 404"
    if "Traceback (most recent call last)" in t and "DEBUG" in t:
        return "Python traceback page on a random 404"
    return None


async def check_exposed(
    target: str,
    options: Optional[ProbeOptions] = None,
    *,
    client: Optional[httpx.AsyncClient] = None,
) -> ProbeReport:
    options = options or ProbeOptions()
    report = ProbeReport(target=target)
    async with Session(target, options, client) as s:
        report.target = s.target
        results = await run_parallel(
            *[s.fetch(e.path, max_bytes=65536) for e in EXPOSURES],
            limit=max(options.concurrency, 1),
        )
        for e, r in zip(EXPOSURES, results):
            if not isinstance(r, Resp) or r.status != 200:
                continue
            ev = e.validator(r)
            if ev:
                report.add(CHECK, e.severity, r.url, e.title,
                           f"HTTP 200, {r.size} bytes, content validated", ev)
        # Debug pages: provoke a 404 with a random path (also try a malformed one).
        for p in (uuid.uuid4().hex, f"{uuid.uuid4().hex[:8]}/%"):
            r = await s.fetch(p, max_bytes=65536)
            if r is not None:
                ev = _v_debug_page(r)
                if ev:
                    report.add(CHECK, "high", r.url, "Framework debug mode enabled",
                               f"HTTP {r.status}; tracebacks and settings leak to visitors", ev)
                    break
        s.finalize(report, CHECK)
    return report
