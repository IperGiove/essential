"""Certificate and protocol posture (stdlib ssl + socket, run in a thread)."""
from __future__ import annotations

import asyncio
import os
import socket
import ssl
import tempfile
import warnings
import time
from datetime import datetime, timezone
from typing import Optional

from ._http import ProbeOptions, record_stats, split_host_port
from .report import ProbeReport

CHECK = "tls"
_LOCAL_REFUSAL = ("no protocols available", "no ciphers available", "unsupported protocol version")


def _handshake(host: str, port: int, timeout: float, ctx: ssl.SSLContext) -> dict:
    with socket.create_connection((host, port), timeout=timeout) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as tls:
            return {"protocol": tls.version(), "cipher": (tls.cipher() or ("?",))[0],
                    "der": tls.getpeercert(binary_form=True), "dict": tls.getpeercert()}


def _decode_der(der: bytes) -> dict:
    """Parse a DER certificate with the stdlib (CPython's own test helper)."""
    pem = ssl.DER_cert_to_PEM_cert(der)
    fd, path = tempfile.mkstemp(suffix=".pem")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(pem)
        return ssl._ssl._test_decode_cert(path)  # type: ignore[attr-defined]
    finally:
        os.unlink(path)


def _flat(rdns) -> dict:
    return {k: v for rdn in rdns for (k, v) in rdn}


def hostname_matches(host: str, cert: dict) -> bool:
    host = host.lower()
    sans = cert.get("subjectAltName", ())
    names = [v.lower() for k, v in sans if k in ("DNS", "IP Address")]
    if not names:                                    # legacy: CN only
        cn = _flat(cert.get("subject", ())).get("commonName")
        names = [cn.lower()] if cn else []
    for n in names:
        if n == host:
            return True
        if n.startswith("*.") and host.count(".") >= 1 and host.split(".", 1)[1] == n[2:]:
            return True
    return False


def _gather(host: str, port: int, timeout: float, insecure_only: bool = False) -> dict:
    out: dict = {"verify_error": None, "error": None}
    ctx = ssl.create_default_context()
    try:
        _handshake(host, port, timeout, ctx)
    except ssl.SSLCertVerificationError as e:
        out["verify_error"] = e.verify_message or str(e)
    except (ssl.SSLError, OSError) as e:
        out["error"] = f"{type(e).__name__}: {e}"
        return out
    nctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    nctx.check_hostname = False
    nctx.verify_mode = ssl.CERT_NONE
    try:
        hs = _handshake(host, port, timeout, nctx)
    except (ssl.SSLError, OSError) as e:
        out["error"] = f"{type(e).__name__}: {e}"
        return out
    out.update(protocol=hs["protocol"], cipher=hs["cipher"], cert=hs["dict"] or _decode_der(hs["der"]))
    # Old protocols
    old = {}
    for name, attr in (("TLSv1", "TLSv1"), ("TLSv1.1", "TLSv1_1")):
        with warnings.catch_warnings():       # these enum members are deprecated
            warnings.simplefilter("ignore", DeprecationWarning)
            old[name] = _try_old(host, port, timeout, getattr(ssl.TLSVersion, attr))
    out["old"] = old
    return out


def _try_old(host: str, port: int, timeout: float, version) -> str:
    """'accepted' | 'rejected' | 'untestable: <why>'"""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return _try_old_inner(host, port, timeout, version)


def _try_old_inner(host: str, port: int, timeout: float, version) -> str:
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        try:
            ctx.set_ciphers("ALL:@SECLEVEL=0")
        except ssl.SSLError:
            pass
        ctx.minimum_version = version
        ctx.maximum_version = version
    except (ValueError, ssl.SSLError, AttributeError) as e:
        return f"untestable: local OpenSSL lacks {version.name} ({e})"
    try:
        _handshake(host, port, timeout, ctx)
        return "accepted"
    except ssl.SSLError as e:
        if any(m in str(e).lower() for m in _LOCAL_REFUSAL):
            return f"untestable: local OpenSSL lacks {version.name} ({e})"
        return "rejected"
    except OSError as e:
        return f"untestable: {type(e).__name__}: {e}"


async def check_tls(
    target: str,
    options: Optional[ProbeOptions] = None,
    *,
    port: Optional[int] = None,
    now: Optional[float] = None,
) -> ProbeReport:
    options = options or ProbeOptions()
    host, url_port = split_host_port(target, 443)
    port = port or url_port or 443
    report = ProbeReport(target=f"{host}:{port}")
    if not host:
        report.errors.append(f"{CHECK}: no host in target {target!r}")
        return report
    t0 = time.monotonic()
    where = f"{host}:{port}"
    info = await asyncio.to_thread(_gather, host, port, options.timeout)
    record_stats(report, CHECK, 4 if "old" in info else 2, 1 if info.get("error") else 0, t0)
    if info.get("error"):
        report.errors.append(f"{CHECK}: {where}: {info['error']}")
        return report
    cert = info["cert"]
    subj, issuer = _flat(cert.get("subject", ())), _flat(cert.get("issuer", ()))
    sans = [v for _, v in cert.get("subjectAltName", ())]
    not_before, not_after = cert.get("notBefore"), cert.get("notAfter")
    report.add(CHECK, "info", where, f"{info['protocol']} / {info['cipher']}",
               f"CN={subj.get('commonName')}; SANs={', '.join(sans) or '-'}; "
               f"issuer={issuer.get('organizationName') or issuer.get('commonName')}; "
               f"valid {not_before} -> {not_after}")
    ve = info["verify_error"]
    if ve and not any(w in ve.lower() for w in ("has expired", "hostname mismatch", "doesn't match")):
        kind = "self-signed" if "self-signed" in ve.lower() or "self signed" in ve.lower() else "not trusted"
        report.add(CHECK, "medium", where, f"Certificate is {kind} (verification failed)", ve)
    if not_after:
        left = (ssl.cert_time_to_seconds(not_after) - (now if now is not None else time.time())) / 86400
        if left < 0:
            report.add(CHECK, "high", where, "Certificate has expired", f"notAfter {not_after} ({-left:.0f} days ago)")
        elif left < 15:
            report.add(CHECK, "medium", where, f"Certificate expires in {left:.0f} days", f"notAfter {not_after}")
        elif left < 30:
            report.add(CHECK, "low", where, f"Certificate expires in {left:.0f} days", f"notAfter {not_after}")
    if not hostname_matches(host, cert):
        report.add(CHECK, "high", where, f"Hostname {host} not covered by the certificate",
                   f"CN={subj.get('commonName')}; SANs={', '.join(sans) or '-'}")
    old = info["old"]
    acc = [n for n, v in old.items() if v == "accepted"]
    if acc:
        report.add(CHECK, "medium", where, f"TLS {'/'.join('1.0' if n == 'TLSv1' else '1.1' for n in acc)} accepted",
                   "Deprecated protocol versions are enabled; allow TLS 1.2+ only.")
    for n, v in old.items():
        if v.startswith("untestable"):
            report.add(CHECK, "info", where, f"{n}: could not test", v[len("untestable: "):])
    return report
