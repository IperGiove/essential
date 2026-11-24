"""ports / auth / tls / email checks (the OUTSIDE view of a VM)."""
import asyncio
import datetime as dt
import socket
import ssl
import threading

import httpx
import pytest
from click.testing import CliRunner

from esuls.probe import ProbeOptions, check_auth, check_email, check_ports, check_tls, run_full_probe
from esuls.probe.cli import app
from esuls.probe.ports import DEFAULT_PORTS, parse_ports
from esuls.probe._http import registrable_domain, split_host_port


def client_for(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ── helpers ───────────────────────────────────────────────────────────────

def test_host_helpers():
    assert split_host_port("https://h.example:8443/x") == ("h.example", 8443)
    assert split_host_port("h.example") == ("h.example", None)
    assert split_host_port("h.example:22") == ("h.example", 22)
    assert registrable_domain("www.a.example.it") == "example.it"
    assert registrable_domain("a.b.example.co.uk") == "example.co.uk"
    assert parse_ports("22,80,8000-8002") == [22, 80, 8000, 8001, 8002]
    with pytest.raises(ValueError):
        parse_ports("70000")
    assert 22 in DEFAULT_PORTS and 2019 in DEFAULT_PORTS and 35 <= len(DEFAULT_PORTS) <= 50


# ── ports ─────────────────────────────────────────────────────────────────

async def test_ports_open_closed_and_dangerous():
    async def handler(r, w):
        w.close()
    srv = await asyncio.start_server(handler, "127.0.0.1", 0)
    open_port = srv.sockets[0].getsockname()[1]
    closed = free_port()
    try:
        rep = await check_ports("127.0.0.1", ports=[open_port, closed])
    finally:
        srv.close()
    assert rep.stats["ports"]["open_ports"] == [open_port]
    assert rep.stats["ports"]["closed"] == 1
    assert [f.title for f in rep.findings] == [f"Open port {open_port}/tcp (unknown service)"]
    assert rep.findings[0].severity == "info" and rep.exit_code == 0


async def _fake_redis(reply: bytes):
    async def handler(r, w):
        await r.readline()
        w.write(reply)
        await w.drain()
        w.close()
    srv = await asyncio.start_server(handler, "127.0.0.1", 0)
    return srv, srv.sockets[0].getsockname()[1]


async def test_ports_redis_no_auth_is_high():
    srv, port = await _fake_redis(b"+PONG\r\n")
    try:
        rep = await check_ports("http://127.0.0.1", ports=[port], redis_ports=(port,))
    finally:
        srv.close()
    (f,) = rep.findings
    assert f.severity == "high" and "Redis reachable without auth" in f.title


async def test_ports_redis_requiring_auth_is_not_high():
    srv, port = await _fake_redis(b"-NOAUTH Authentication required.\r\n")
    try:
        rep = await check_ports("127.0.0.1", ports=[port], redis_ports=(port,))
    finally:
        srv.close()
    assert rep.findings[0].severity != "high"


async def test_ports_known_dangerous_port_is_medium(monkeypatch):
    from esuls.probe import ports as P
    srv, port = await _fake_redis(b"")
    monkeypatch.setitem(P.PORTS, port, ("mysql", True))
    try:
        rep = await check_ports("127.0.0.1", ports=[port], redis_ports=())
    finally:
        srv.close()
    assert rep.findings[0].severity == "medium" and "mysql" in rep.findings[0].title


async def test_ports_unresolvable_host_is_error():
    rep = await check_ports("nonexistent.invalid", ports=[80], options=ProbeOptions(timeout=3))
    assert rep.errors


# ── auth ──────────────────────────────────────────────────────────────────

def _lockout_app(k, status=429, headers=None):
    seen = {"n": 0}

    def h(req):
        seen["n"] += 1
        if seen["n"] > k:
            return httpx.Response(status, headers=headers or {})
        return httpx.Response(200, text="invalid credentials")
    return h, seen


async def test_auth_lockout_fires_and_stops():
    h, seen = _lockout_app(5, headers={"Retry-After": "60"})
    rep = await check_auth("https://site.test", client=client_for(h), max_attempts=25)
    (f,) = rep.findings
    assert f.severity == "info" and "attempt #6 (429)" in f.title and "Retry-After: 60" in f.detail
    assert seen["n"] == 6                                  # stopped immediately
    assert rep.exit_code == 0


async def test_auth_403_after_normal_counts_but_always_403_is_unassessable():
    h, seen = _lockout_app(3, status=403)
    rep = await check_auth("https://site.test", client=client_for(h))
    assert "#4 (403)" in rep.findings[0].title and seen["n"] == 4
    rep = await check_auth("https://site.test", client=client_for(lambda r: httpx.Response(403)), max_attempts=5)
    assert "could not be assessed" in rep.findings[0].title


async def test_auth_lockout_message_in_body():
    n = {"i": 0}

    def h(req):
        n["i"] += 1
        return httpx.Response(200, text="Account locked, try again later" if n["i"] > 3 else "bad password")
    rep = await check_auth("https://site.test", client=client_for(h))
    assert "attempt #4" in rep.findings[0].title and n["i"] == 4


async def test_auth_never_defends_is_medium_and_json_body():
    bodies = []

    def h(req):
        bodies.append((req.headers["content-type"], req.content))
        return httpx.Response(401)
    rep = await check_auth("https://site.test", client=client_for(h), max_attempts=7,
                           content_type="json", user_field="email")
    (f,) = rep.findings
    assert f.severity == "medium" and "after 7 attempts" in f.title and len(bodies) == 7
    assert bodies[0][0] == "application/json" and b'"email"' in bodies[0][1]
    assert rep.exit_code == 1


async def test_auth_loopback_without_xff_is_downgraded():
    h = lambda r: httpx.Response(401)
    rep = await check_auth("http://127.0.0.1:8000", client=client_for(h), max_attempts=4)
    assert rep.findings[0].severity == "info" and "X-Forwarded-For: 203.0.113.7" in rep.findings[0].detail
    rep = await check_auth("http://127.0.0.1:8000", ProbeOptions(headers={"X-Forwarded-For": "203.0.113.7"}),
                           client=client_for(h), max_attempts=4)
    assert rep.findings[0].severity == "medium"


# ── tls ───────────────────────────────────────────────────────────────────

crypto = pytest.importorskip("cryptography")


def make_cert(tmp_path, *, dns=("localhost",), ips=("127.0.0.1",), start_days=-10, end_days=365):
    import ipaddress
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, dns[0] if dns else "x")])
    now = dt.datetime.now(dt.timezone.utc)
    sans = [x509.DNSName(d) for d in dns] + [x509.IPAddress(ipaddress.ip_address(i)) for i in ips]
    b = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
         .serial_number(x509.random_serial_number())
         .not_valid_before(now + dt.timedelta(days=start_days)).not_valid_after(now + dt.timedelta(days=end_days)))
    if sans:
        b = b.add_extension(x509.SubjectAlternativeName(sans), critical=False)
    cert = b.sign(key, hashes.SHA256())
    cp, kp = tmp_path / "c.pem", tmp_path / "k.pem"
    cp.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    kp.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                     serialization.NoEncryption()))
    return str(cp), str(kp)


class TlsServer:
    def __init__(self, certfile, keyfile, old=False):
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        if old:
            self.ctx.set_ciphers("ALL:@SECLEVEL=0")
            self.ctx.minimum_version = ssl.TLSVersion.TLSv1
        else:
            self.ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        self.ctx.load_cert_chain(certfile, keyfile)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self._stop = False
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while not self._stop:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            try:
                with self.ctx.wrap_socket(c, server_side=True):
                    pass
            except (ssl.SSLError, OSError):
                c.close()

    def close(self):
        self._stop = True
        self.sock.close()


@pytest.fixture
def tls_server(tmp_path):
    made = []

    def make(**kw):
        old = kw.pop("old", False)
        s = TlsServer(*make_cert(tmp_path, **kw), old=old)
        made.append(s)
        return s
    yield make
    for s in made:
        s.close()


async def test_tls_self_signed_cert_parsed_and_flagged(tls_server):
    srv = tls_server()
    rep = await check_tls("127.0.0.1", port=srv.port)
    t = [f.title for f in rep.findings]
    assert any(x.startswith("TLSv1.") for x in t)                     # protocol / cipher info
    assert "Certificate is self-signed (verification failed)" in t
    assert not any("not covered" in x or "expired" in x for x in t)   # SAN has 127.0.0.1
    info = rep.findings[0]
    assert "CN=localhost" in info.detail and "127.0.0.1" in info.detail
    assert rep.exit_code == 1 and not rep.errors


async def test_tls_hostname_not_in_san_is_high(tls_server):
    srv = tls_server(dns=("other.example",), ips=())
    rep = await check_tls("127.0.0.1", port=srv.port)
    f = next(f for f in rep.findings if "not covered" in f.title)
    assert f.severity == "high"


async def test_tls_expired_and_expiring(tls_server):
    exp = await check_tls("127.0.0.1", port=tls_server(start_days=-400, end_days=-5).port)
    assert any(f.title == "Certificate has expired" and f.severity == "high" for f in exp.findings)
    soon = await check_tls("127.0.0.1", port=tls_server(end_days=10).port)
    assert any("expires in" in f.title and f.severity == "medium" for f in soon.findings)
    month = await check_tls("127.0.0.1", port=tls_server(end_days=25).port)
    assert any("expires in" in f.title and f.severity == "low" for f in month.findings)


async def test_tls_connection_refused_is_error():
    rep = await check_tls("127.0.0.1", port=free_port(), options=ProbeOptions(timeout=2))
    assert rep.errors and rep.exit_code == 3


async def test_tls_modern_server_never_reports_old_protocols(tls_server):
    srv = tls_server()
    rep = await check_tls("127.0.0.1", port=srv.port)
    assert not any("accepted" in f.title for f in rep.findings)
    # either rejected silently or reported as untestable, never a crash
    assert all(f.severity == "info" for f in rep.findings if "could not test" in f.title)


def test_tls_old_protocol_acceptance_when_openssl_allows(tls_server):
    from esuls.probe.tls import _try_old
    try:
        srv = tls_server(old=True)
    except (ValueError, ssl.SSLError):
        pytest.skip("local OpenSSL cannot serve TLS 1.0")
    res = _try_old("127.0.0.1", srv.port, 3, ssl.TLSVersion.TLSv1)
    if res.startswith("untestable"):
        pytest.skip(res)
    assert res == "accepted"


# ── email ─────────────────────────────────────────────────────────────────

dns_resolver = pytest.importorskip("dns.resolver")


class _Txt:
    def __init__(self, s):
        self.strings = [s.encode()]


class _Mx:
    def __init__(self, pref, host):
        self.preference, self.exchange = pref, host + "."


class FakeResolver:
    def __init__(self, records):
        self.records = records

    async def resolve(self, name, rdtype):
        if (name, rdtype) not in self.records:
            raise dns_resolver.NXDOMAIN()
        return self.records[(name, rdtype)]


def titles(rep):
    return {f.title: f for f in rep.findings}


async def test_email_good_posture():
    res = FakeResolver({
        ("example.it", "MX"): [_Mx(10, "mx1.example.it"), _Mx(20, "mx2.example.it")],
        ("example.it", "TXT"): [_Txt("v=spf1 include:_spf.x.com -all")],
        ("_dmarc.example.it", "TXT"): [_Txt("v=DMARC1; p=reject; rua=mailto:a@example.it")],
        ("s1._domainkey.example.it", "TXT"): [_Txt("v=DKIM1; k=rsa; p=MIIB")],
    })
    rep = await check_email("https://www.example.it/x", selectors=["s1", "s2"], resolver=res)
    t = titles(rep)
    assert "mx1.example.it (pref 10)" in t["MX records"].detail
    assert "SPF policy -all" in t and "DMARC policy p=reject" in t
    assert "DKIM selector 's1' found" in t and "DKIM selector 's2' absent" in t
    assert rep.exit_code == 0 and rep.target == "example.it"


async def test_email_missing_and_weak():
    res = FakeResolver({("example.it", "TXT"): [_Txt("v=spf1 +all")],
                        ("_dmarc.example.it", "TXT"): [_Txt("v=DMARC1; p=none")]})
    rep = await check_email("example.it", resolver=res)
    t = titles(rep)
    assert t["SPF allows everyone (+all)"].severity == "high"
    assert t["DMARC policy p=none (monitoring only)"].severity == "low"
    assert t["No MX records"].severity == "info"
    assert "DKIM not checked" in t                       # no selector: skipped, not guessed
    empty = await check_email("example.it", resolver=FakeResolver({}))
    t = titles(empty)
    assert t["SPF record missing"].severity == "medium" and t["DMARC record missing"].severity == "medium"


async def test_email_skip_on_ip_and_dns_failure_recorded():
    rep = await check_email("http://127.0.0.1:8000")
    assert rep.findings[0].title == "Email DNS check skipped" and rep.exit_code == 0

    class Boom:
        async def resolve(self, *a):
            raise dns_resolver.NoNameservers()
    rep = await check_email("example.it", resolver=Boom())
    assert rep.errors and rep.exit_code == 3


# ── wiring ────────────────────────────────────────────────────────────────

async def test_full_skips_tls_on_plain_http_and_email_on_ip():
    h = lambda r: httpx.Response(404, text="nf")
    rep = await run_full_probe("http://127.0.0.1:%d" % free_port(), client=client_for(h), wordlist=["a"],
                               ratelimit_requests=3, ports=[free_port()], auth_max_attempts=2,
                               options=ProbeOptions(timeout=2))
    skipped = {f.check for f in rep.findings if f.title.endswith("skipped")}
    assert {"tls", "email"} <= skipped
    assert not any(e.startswith(("tls", "email")) for e in rep.errors)


def test_cli_new_subcommands_exist_and_validate():
    r = CliRunner()
    for cmd in ("ports", "auth", "tls", "email"):
        assert r.invoke(app, [cmd, "--help"]).exit_code == 0
    assert "fail2ban" in r.invoke(app, ["auth", "--help"]).output
    assert r.invoke(app, ["ports", "127.0.0.1", "--ports", "abc"]).exit_code == 2


def test_cli_ports_json_exit_codes():
    import json
    port = free_port()
    res = CliRunner().invoke(app, ["ports", "127.0.0.1", "--ports", str(port), "--json"])
    assert res.exit_code == 0 and json.loads(res.output)["stats"]["ports"]["closed"] == 1


# ── full: one simulated client IP per check ───────────────────────────────

def _bucket_app(limit=40):
    """Per-XFF request budget: a shared client identity gets banned (429) quickly."""
    seen, by_ip = [], {}

    def h(req):
        ip = req.headers.get("x-forwarded-for", "-")
        by_ip[ip] = by_ip.get(ip, 0) + 1
        seen.append((req.url.path, ip))
        if by_ip[ip] > limit:
            return httpx.Response(429)
        return httpx.Response(200, text="ok") if req.url.path in ("/", "/login") else httpx.Response(404, text="nf")
    return h, seen


async def test_full_runs_each_check_under_a_distinct_xff_and_no_false_lockout():
    h, seen = _bucket_app(limit=40)
    rep = await run_full_probe("http://127.0.0.1:%d" % free_port(), client=client_for(h),
                               wordlist=[f"w{i}" for i in range(80)], ratelimit_requests=20,
                               auth_max_attempts=5, ports=[free_port()],
                               options=ProbeOptions(timeout=2),
                               checks=("headers", "exposed", "paths", "ratelimit", "auth"))
    ips = {ip for _, ip in seen}
    assert len(ips) >= 5 and all(ip.startswith("203.0.113.") for ip in ips)
    # paths burned its own bucket; ratelimit/auth must not inherit that ban
    titles = [f.title for f in rep.findings]
    assert not any("kicked in at request #1 " in t or "attempt #1 " in t for t in titles), titles


async def test_full_user_xff_is_base_for_first_check_only():
    h, seen = _bucket_app(limit=1000)
    await run_full_probe("http://127.0.0.1:1", client=client_for(h), wordlist=["a"], ratelimit_requests=2,
                         auth_max_attempts=2, options=ProbeOptions(timeout=2, headers={"x-forwarded-for": "198.51.100.7", "X-Keep": "1"}),
                         checks=("headers", "cors", "ratelimit"))
    order = []
    for _, ip in seen:
        if ip not in order:
            order.append(ip)
    assert order[0] == "198.51.100.7" and all(ip.startswith("198.51.100.") for ip in order) and len(order) == 3
