"""Read-only service fingerprint for the `ports` check.

Per known service: connect, read the greeting, send ONE capability command, read
the reply, send the protocol's quit, close. No authentication and no
credentials, ever. Implicit-TLS ports (465/993/995) get a permissive TLS
connect that only records what answered.

With ``plaintext_auth`` (opt-in, more invasive) ONE extra non-credential step
is sent after TLS was seen advertised (``USER anonymous`` / ``AUTH LOGIN`` /
...) to learn whether the server DEMANDS TLS first. No password is ever sent.
"""
from __future__ import annotations

import asyncio
import re
import ssl
from dataclasses import dataclass, field
from typing import Callable, Optional

READ_CAP = 3.0           # seconds per read, so a silent port cannot stall the scan
MAX_BYTES = 16384

SERVICES = {21: "ftp", 22: "ssh", 25: "smtp", 587: "smtp", 110: "pop3", 143: "imap"}
IMPLICIT_TLS = {465: "smtps", 993: "imaps", 995: "pop3s"}


@dataclass
class BannerResult:
    banner: str = ""
    findings: list = field(default_factory=list)       # (severity, title, detail)


class _Conn:
    def __init__(self, r: asyncio.StreamReader, w: asyncio.StreamWriter, cap: float) -> None:
        self.r, self.w, self.cap, self.buf = r, w, cap, b""
        self.sent: list[bytes] = []

    async def read_until(self, done: Callable[[list[str]], bool]) -> list[str]:
        """Lines read until `done(lines)` or timeout/EOF/size cap (never raises)."""
        lines: list[str] = []
        total = 0
        while not done(lines) and total < MAX_BYTES:
            try:
                raw = await asyncio.wait_for(self.r.readline(), self.cap)
            except (asyncio.TimeoutError, TimeoutError, OSError, ValueError):
                break
            if not raw:
                break
            total += len(raw)
            lines.append(raw.decode("utf-8", "replace").rstrip("\r\n"))
        return lines

    async def send(self, line: str) -> None:
        data = (line + "\r\n").encode()
        self.sent.append(data)
        try:
            self.w.write(data)
            await asyncio.wait_for(self.w.drain(), self.cap)
        except (asyncio.TimeoutError, TimeoutError, OSError):
            pass

    async def close(self) -> None:
        try:
            self.w.close()
            await asyncio.wait_for(self.w.wait_closed(), 1.0)
        except Exception:  # noqa: BLE001
            pass


def _final_numeric(lines: list[str]) -> bool:          # FTP/SMTP: "250 done" ends, "250-more" continues
    return bool(lines) and re.match(r"^\d{3}( |$)", lines[-1]) is not None


def _first_line(lines: list[str]) -> bool:
    return bool(lines)


def _clip(s: str, n: int = 160) -> str:
    s = s.replace("\x00", "")
    return s if len(s) <= n else s[:n] + "..."


def _code(lines: list[str]) -> str:
    return lines[-1][:3] if lines else ""


def _tls_words(lines: list[str]) -> bool:
    return bool(re.search(r"tls|ssl|starttls|secure|encrypt|plaintext|privacy", " ".join(lines), re.I))


async def fingerprint(host: str, port: int, service: str, timeout: float, plaintext_auth: bool = False) -> BannerResult:
    cap = min(max(timeout, 0.1), READ_CAP)
    res = BannerResult()
    try:
        r, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except Exception as e:  # noqa: BLE001
        res.findings.append(("info", f"{service} banner not read", f"reconnect failed: {type(e).__name__}"))
        return res
    c = _Conn(r, w, cap)
    try:
        await _PROBES[service](c, res, port, plaintext_auth)
    except Exception as e:  # noqa: BLE001 - a weird service must never crash the scan
        res.findings.append(("info", f"{service.upper()} fingerprint incomplete", f"{type(e).__name__}: {e}"))
    finally:
        await c.close()
    return res


def _raw_banner(res: BannerResult, svc: str, lines: list[str]) -> None:
    res.banner = _clip(" | ".join(lines))
    if res.banner:
        res.findings.append(("info", f"{svc} did not speak the expected protocol",
                             f"raw banner: {res.banner}"))
    else:
        # Nothing arrived at all: the port accepts a connection but sends no
        # greeting in time. Most often the service is filtered upstream (e.g. an
        # ISP blocking outbound 25) or greet-delayed, not a wrong protocol.
        res.findings.append(("info", f"No {svc} banner within the timeout",
                             "port connects but sent no greeting — likely filtered "
                             "upstream or greet-delayed; raise --timeout to retry"))


async def _ftp(c: _Conn, res: BannerResult, port: int, pa: bool) -> None:
    lines = await c.read_until(_final_numeric)
    res.banner = _clip(" | ".join(lines))
    if not lines or not lines[0].startswith("220"):
        return _raw_banner(res, "FTP", lines)
    await c.send("FEAT")
    feat = await c.read_until(_final_numeric)
    tls = any(re.search(r"\bAUTH (TLS|SSL)\b", ln, re.I) for ln in feat)
    if not tls:
        res.findings.append(("medium", "FTP offers plaintext login (no AUTH TLS advertised)",
                             f"banner: {res.banner}; FEAT: {_clip(' | '.join(feat))}"))
    else:
        res.findings.append(("info", "FTPS available (AUTH TLS); plaintext enforcement not checked"
                             if not pa else "FTPS available (AUTH TLS)", f"banner: {res.banner}"))
        if pa:
            await c.send("USER anonymous")
            rep = await c.read_until(_final_numeric)
            code = _code(rep)
            if code in ("331", "230"):
                res.findings.append(("high" if code == "230" else "medium",
                                     "FTP plaintext login accepted (TLS not enforced)",
                                     f"USER anonymous -> {_clip(' | '.join(rep))}; no password was sent"))
            else:
                res.findings.append(("info", "FTP plaintext login refused (TLS enforced)",
                                     f"USER anonymous -> {_clip(' | '.join(rep))}"))
    await c.send("QUIT")


async def _smtp(c: _Conn, res: BannerResult, port: int, pa: bool) -> None:
    lines = await c.read_until(_final_numeric)
    res.banner = _clip(" | ".join(lines))
    if not lines or not lines[0].startswith("220"):
        return _raw_banner(res, "SMTP", lines)
    parts = lines[0].split()
    hostname = parts[1] if len(parts) > 1 else "?"
    await c.send("EHLO probe.invalid")
    ehlo = await c.read_until(_final_numeric)
    starttls = any(re.match(r"^250[ -]STARTTLS\b", ln, re.I) for ln in ehlo)
    detail = f"banner: {res.banner}; server hostname: {hostname}"
    if not starttls:
        res.findings.append(("medium", "SMTP has no STARTTLS (plaintext only)", detail))
    else:
        res.findings.append(("info", "SMTP offers STARTTLS", detail))
        if pa:
            await c.send("AUTH LOGIN")
            rep = await c.read_until(_final_numeric)
            code = _code(rep)
            if code == "334":
                res.findings.append(("medium", "SMTP plaintext AUTH accepted (TLS not enforced)",
                                     "AUTH LOGIN answered 334 before STARTTLS; no credentials were sent"))
            elif code.startswith("5") and _tls_words(rep):
                res.findings.append(("info", "SMTP plaintext login refused (TLS enforced)",
                                     f"AUTH LOGIN -> {_clip(' | '.join(rep))}"))
            else:
                res.findings.append(("info", "SMTP plaintext AUTH not offered on this port",
                                     f"AUTH LOGIN -> {_clip(' | '.join(rep))}"))
    await c.send("QUIT")


async def _pop3(c: _Conn, res: BannerResult, port: int, pa: bool) -> None:
    lines = await c.read_until(_first_line)
    res.banner = _clip(" | ".join(lines))
    if not lines or not lines[0].startswith("+OK"):
        return _raw_banner(res, "POP3", lines)
    await c.send("CAPA")
    capa = await c.read_until(lambda ls: bool(ls) and (ls[-1] == "." or ls[0].startswith("-ERR")))
    if not any(ln.strip().upper() == "STLS" for ln in capa):
        res.findings.append(("medium", "POP3 has no STLS (plaintext only)",
                             f"banner: {res.banner}; CAPA: {_clip(' | '.join(capa))}"))
    else:
        res.findings.append(("info", "POP3 offers STLS", f"banner: {res.banner}"))
        if pa:
            await c.send("USER probe")
            rep = await c.read_until(_first_line)
            if rep and rep[0].startswith("+OK"):
                res.findings.append(("medium", "POP3 plaintext login accepted (TLS not enforced)",
                                     "USER accepted before STLS; no password was sent"))
            else:
                res.findings.append(("info", "POP3 plaintext login refused (TLS enforced)",
                                     f"USER -> {_clip(' | '.join(rep))}"))
    await c.send("QUIT")


async def _imap(c: _Conn, res: BannerResult, port: int, pa: bool) -> None:
    lines = await c.read_until(_first_line)
    res.banner = _clip(" | ".join(lines))
    if not lines or not lines[0].startswith("* OK"):
        return _raw_banner(res, "IMAP", lines)
    await c.send("a CAPABILITY")
    caps = await c.read_until(lambda ls: any(ln.startswith("a ") for ln in ls))
    text = " ".join(caps).upper()
    if "STARTTLS" not in text.split():
        res.findings.append(("medium", "IMAP has no STARTTLS (plaintext only)",
                             f"banner: {res.banner}; CAPABILITY: {_clip(' | '.join(caps))}"))
    else:
        res.findings.append(("info", "IMAP offers STARTTLS", f"banner: {res.banner}"))
        if pa:
            if "LOGINDISABLED" in text.split():
                res.findings.append(("info", "IMAP plaintext login refused (TLS enforced)",
                                     "CAPABILITY advertises LOGINDISABLED before STARTTLS"))
            else:
                await c.send("b AUTHENTICATE PLAIN")
                rep = await c.read_until(lambda ls: bool(ls))
                if rep and rep[0].startswith("+"):
                    res.findings.append(("medium", "IMAP plaintext AUTH accepted (TLS not enforced)",
                                         "AUTHENTICATE PLAIN got a continuation before STARTTLS; no credentials were sent"))
                else:
                    res.findings.append(("info", "IMAP plaintext login refused (TLS enforced)",
                                         f"AUTHENTICATE -> {_clip(' | '.join(rep))}"))
    await c.send("a LOGOUT")


async def _ssh(c: _Conn, res: BannerResult, port: int, pa: bool) -> None:
    lines = await c.read_until(lambda ls: bool(ls) and ls[-1].startswith("SSH-"))
    ssh = next((ln for ln in lines if ln.startswith("SSH-")), None)
    res.banner = _clip(ssh or " | ".join(lines))
    if not ssh:
        return _raw_banner(res, "SSH", lines)
    res.findings.append(("info", f"SSH version: {ssh}", "banner read only; nothing was sent"))


_PROBES = {"ftp": _ftp, "smtp": _smtp, "pop3": _pop3, "imap": _imap, "ssh": _ssh}


async def implicit_tls(host: str, port: int, name: str, timeout: float) -> BannerResult:
    """Permissive TLS connect on an implicit-TLS port: record what answered."""
    res = BannerResult()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        r, w = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=ctx, server_hostname=host,
                                    ssl_handshake_timeout=timeout), timeout)
    except Exception as e:  # noqa: BLE001
        res.findings.append(("info", f"{name} on {port}/tcp: TLS handshake failed", f"{type(e).__name__}: {e}"))
        return res
    c = _Conn(r, w, min(max(timeout, 0.1), READ_CAP))
    try:
        proto = w.get_extra_info("ssl_object").version()
        lines = await c.read_until(_first_line)       # greeting is optional
        res.banner = _clip(" | ".join(lines))
        res.findings.append(("info", f"{name} on {port}/tcp speaks implicit TLS ({proto})",
                             f"TLS service answered; greeting: {res.banner or '(none within timeout)'}"))
    finally:
        await c.close()
    return res
