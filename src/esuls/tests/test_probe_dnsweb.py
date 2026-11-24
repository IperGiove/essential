"""dns / subdomains / open relay / methods / directory listing / security.txt."""
import asyncio

import httpx
import pytest
from click.testing import CliRunner

from esuls.probe import ProbeOptions, check_dns
from esuls.probe.cli import app

dns_resolver = pytest.importorskip("dns.resolver")


class _R:
    """Fake rdata: str() is what the probe reads."""
    def __init__(self, text, **kw):
        self.text = text
        self.__dict__.update(kw)

    def __str__(self):
        return self.text


class FakeResolver:
    def __init__(self, records):
        self.records = records

    async def resolve(self, name, rdtype):
        v = self.records.get((name, rdtype), "NX")
        if v == "NX":
            raise dns_resolver.NXDOMAIN()
        if v == "NOANSWER":
            raise dns_resolver.NoAnswer()
        if isinstance(v, Exception):
            raise v
        return [x if isinstance(x, _R) else _R(x) for x in v]


def by_title(rep):
    return {f.title: f for f in rep.findings}


ZONE = {("example.it", "NS"): ["ns1.example.it.", "ns2.example.it."],
        ("ns1.example.it", "A"): ["192.0.2.1"], ("ns2.example.it", "A"): ["192.0.2.2"]}


async def test_dns_open_resolver_is_high_and_refusal_is_not():
    rec = lambda ip, t: "recursive" if ip == "192.0.2.1" else "refused"
    rep = await check_dns("example.it", resolver=FakeResolver(ZONE), recursion=rec,
                          axfr=lambda ip, d, t: ("refused", 0, []))
    hits = [f for f in rep.findings if f.title.startswith("Open DNS resolver")]
    assert len(hits) == 1 and hits[0].severity == "high" and "192.0.2.1" in hits[0].detail
    assert rep.exit_code == 1
    ok = await check_dns("example.it", resolver=FakeResolver(ZONE), recursion=lambda ip, t: "refused",
                         axfr=lambda ip, d, t: ("refused", 0, []))
    assert not any(f.severity == "high" for f in ok.findings)


async def test_dns_axfr_allowed_is_high_with_samples():
    axfr = lambda ip, d, t: ("ok", 42, ["example.it.", "www.example.it."]) if ip == "192.0.2.2" else ("refused", 0, [])
    rep = await check_dns("https://www.example.it/", resolver=FakeResolver(ZONE),
                          recursion=lambda ip, t: "refused", axfr=axfr)
    f = by_title(rep)["DNS zone transfer allowed"]
    assert f.severity == "high" and "42 record" in f.detail and "www.example.it." in f.detail
    assert "AXFR refused" in by_title(rep)


async def test_dns_dnssec_present_absent_and_failures_never_crash():
    base = dict(ZONE)
    kw = dict(recursion=lambda ip, t: "refused", axfr=lambda ip, d, t: ("error", 0, ["timeout"]))
    r = await check_dns("example.it", resolver=FakeResolver({**base, ("example.it", "DNSKEY"): ["257 3 13 AAAA"]}), **kw)
    assert by_title(r)["DNSSEC present"].severity == "info"
    r = await check_dns("example.it", resolver=FakeResolver(base), **kw)
    assert by_title(r)["No DNSSEC"].severity == "low"
    r = await check_dns("example.it", resolver=FakeResolver({**base, ("example.it", "DNSKEY"): dns_resolver.NoNameservers(),
                                                              ("example.it", "DS"): dns_resolver.NoNameservers()}), **kw)
    assert "DNSSEC lookup failed" in by_title(r) and not r.errors
    r = await check_dns("example.it", resolver=FakeResolver({}), **kw)       # NXDOMAIN everywhere
    assert by_title(r)["No NS records found"].severity == "info" and r.exit_code == 0


async def test_dns_raising_probe_is_recorded_as_info():
    def boom(*a):
        raise OSError("net down")
    rep = await check_dns("example.it", resolver=FakeResolver(ZONE), recursion=boom, axfr=boom)
    assert not rep.errors and "AXFR attempt failed" in by_title(rep) and "Recursion test failed" in by_title(rep)


async def test_dns_skips_ip_targets():
    rep = await check_dns("http://127.0.0.1:8000")
    assert rep.findings[0].title == "DNS check skipped"


def _udp_dns_server(recursive: bool):
    import socket
    import threading
    import dns.flags, dns.message, dns.rcode, dns.rrset
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(0.1)
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            try:
                data, addr = sock.recvfrom(2048)
            except OSError:
                continue
            q = dns.message.from_wire(data)
            r = dns.message.make_response(q)
            if recursive:
                r.flags |= dns.flags.RA
                r.answer.append(dns.rrset.from_text("example.com.", 60, "IN", "A", "93.184.216.34"))
            else:
                r.set_rcode(dns.rcode.REFUSED)
            sock.sendto(r.to_wire(), addr)
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return sock, stop, t


def test_real_recursion_probe_against_a_local_udp_dns_server():
    import dns.rcode  # noqa: F401
    from esuls.probe.dnscheck import axfr_probe, recursion_probe
    for recursive, want in ((True, "recursive"), (False, "refused")):
        sock, stop, t = _udp_dns_server(recursive)
        try:
            assert recursion_probe("127.0.0.1", 2, port=sock.getsockname()[1]) == want
        finally:
            stop.set()
            t.join(2)
            sock.close()
    # nothing listening: reported, never raised
    assert axfr_probe("127.0.0.1", "example.it", 0.5, port=1)[0] in ("error", "refused")


# ── subdomains ────────────────────────────────────────────────────────────

import json

from esuls.probe import check_subdomains

CT = [{"name_value": "example.it\nwww.example.it"}, {"name_value": "*.example.it\napp.example.it"},
      {"name_value": "old.example.it"}, {"name_value": "blog.example.it"}, {"name_value": "evil.other.com"}]


def ct_client(handler_extra=None, ct_status=200, ct_body=None):
    seen = []

    def h(req):
        seen.append(req)
        if req.url.host == "crt.sh":
            assert "%25.example.it" in str(req.url) or "%.example.it" in req.url.params["q"]
            return httpx.Response(ct_status, text=json.dumps(CT) if ct_body is None else ct_body)
        if handler_extra:
            return handler_extra(req)
        return httpx.Response(404)
    return httpx.AsyncClient(transport=httpx.MockTransport(h)), seen


async def test_subdomain_enumeration_dedupes_and_scopes():
    client, seen = ct_client()
    rep = await check_subdomains("https://www.example.it", client=client, resolver=FakeResolver({}))
    f = by_title(rep)["5 subdomain(s) in Certificate Transparency logs"]
    assert f.severity == "info" and "app.example.it" in f.detail and "other.com" not in f.detail
    assert [r.url.host for r in seen] == ["crt.sh"]            # the target itself is never queried


async def test_subdomain_list_is_capped():
    big = [{"name_value": f"s{i}.example.it"} for i in range(80)]
    client, _ = ct_client(ct_body=json.dumps(big))
    rep = await check_subdomains("example.it", client=client, resolver=FakeResolver({}), list_cap=50)
    f = rep.findings[0]
    assert f.title.startswith("80 subdomain") and "(+30 more)" in f.detail


async def test_crtsh_failure_is_info_not_error():
    for kw in ({"ct_status": 502}, {"ct_body": "<html>bad gateway"}):
        client, _ = ct_client(**kw)
        rep = await check_subdomains("example.it", client=client, resolver=FakeResolver({}))
        assert rep.findings[0].title == "CT lookup failed" and not rep.errors and rep.exit_code == 0


async def test_takeover_nxdomain_and_fingerprint_and_clean():
    res = FakeResolver({
        ("old.example.it", "CNAME"): ["gone.herokuapp.com."],        # target NXDOMAIN
        ("blog.example.it", "CNAME"): ["acme.github.io."],           # exists, unclaimed page
        ("blog.example.it", "A"): [], ("acme.github.io", "A"): ["185.199.108.153"],
        ("app.example.it", "CNAME"): ["live.github.io."], ("live.github.io", "A"): ["185.199.108.153"],
    })

    def pages(req):
        if req.url.host == "blog.example.it":
            return httpx.Response(404, text="<p>There isn't a GitHub Pages site here.</p>")
        return httpx.Response(200, text="my real site")
    client, seen = ct_client(pages)
    rep = await check_subdomains("example.it", client=client, resolver=res)
    hits = {f.url: f for f in rep.findings if f.title == "Possible subdomain takeover"}
    assert set(hits) == {"https://old.example.it", "https://blog.example.it"}
    assert all(f.severity == "high" for f in hits.values())
    assert "NXDOMAIN" in hits["https://old.example.it"].detail and "GitHub Pages" in hits["https://blog.example.it"].detail
    assert rep.exit_code == 1


async def test_subdomains_never_forward_user_headers_and_skip_ip():
    client, seen = ct_client()
    await check_subdomains("example.it", ProbeOptions(headers={"Authorization": "secret"}, cookie="sid=1"),
                           client=client, resolver=FakeResolver({}))
    assert all("authorization" not in r.headers and "cookie" not in r.headers for r in seen)
    rep = await check_subdomains("127.0.0.1")
    assert rep.findings[0].title == "Subdomain check skipped"


def test_cli_dns_and_subdomains_help():
    r = CliRunner()
    assert "crt.sh" in r.invoke(app, ["subdomains", "--help"]).output
    assert r.invoke(app, ["dns", "--help"]).exit_code == 0


# ── email: SPF mechanisms + open relay ────────────────────────────────────

from esuls.probe import check_email
from esuls.tests.test_probe_banner import Fake
from esuls.tests.test_probe_fleet import FakeResolver as MailResolver, _Mx, _Txt


@pytest.fixture
def smtp():
    made = []

    def make(replies):
        base = {"EHLO": b"250-mx.example.it\r\n250 SIZE 100\r\n", "MAIL FROM": b"250 sender ok\r\n", "RSET": b"250 ok\r\n"}
        s = Fake(b"220 mx.example.it ESMTP\r\n", {**base, **replies})
        made.append(s)
        return s
    yield make
    for s in made:
        s.close()


def mail_records(mx_host="127.0.0.1"):
    return MailResolver({("example.it", "MX"): [_Mx(10, mx_host)],
                         ("example.it", "TXT"): [_Txt("v=spf1 include:_spf.arubabusiness.it ip4:192.0.2.0/24 ip6:2001:db8::/32 -all")]})


async def test_email_lists_spf_mechanisms():
    rep = await check_email("example.it", resolver=mail_records())
    f = by_title(rep)["SPF authorised senders"]
    assert f.severity == "info" and "include: _spf.arubabusiness.it" in f.detail
    assert "ip4: 192.0.2.0/24" in f.detail and "ip6: 2001:db8::/32" in f.detail


async def test_open_relay_accepts_is_high_and_never_sends_data(smtp):
    srv = smtp({"RCPT TO": b"250 recipient ok\r\n"})
    rep = await check_email("example.it", resolver=mail_records(), check_open_relay=True, mx_port=srv.port,
                            options=ProbeOptions(timeout=3))
    f = by_title(rep)["Open mail relay"]
    assert f.severity == "high" and rep.exit_code == 1 and "no mail was sent" in f.detail
    srv.wait_connections(1)
    got = b"".join(srv.received).decode()
    assert not any(ln.upper().startswith("DATA") for ln in got.splitlines())
    assert got.splitlines() == ["EHLO probe.invalid", "MAIL FROM:<probe@invalid.example>",
                                "RCPT TO:<relay-test@example.com>", "RSET", "QUIT"]


async def test_open_relay_rejecting_server_is_not_high(smtp):
    srv = smtp({"RCPT TO": b"554 5.7.1 Relay access denied\r\n"})
    rep = await check_email("example.it", resolver=mail_records(), check_open_relay=True, mx_port=srv.port,
                            options=ProbeOptions(timeout=3))
    assert by_title(rep)["Mail relay refused"].severity == "info"
    assert not any(f.severity == "high" for f in rep.findings)


async def test_open_relay_is_opt_in_and_unreachable_25_is_info(smtp):
    srv = smtp({"RCPT TO": b"250 ok\r\n"})
    rep = await check_email("example.it", resolver=mail_records())
    assert not any("relay" in f.title.lower() for f in rep.findings) and srv.received == []
    import socket
    s = socket.socket(); s.bind(("127.0.0.1", 0)); dead = s.getsockname()[1]; s.close()
    rep = await check_email("example.it", resolver=mail_records(), check_open_relay=True, mx_port=dead,
                            options=ProbeOptions(timeout=2))
    f = [f for f in rep.findings if "unreachable from here" in f.title]
    assert f and f[0].severity == "info" and not rep.errors


def test_cli_email_relay_flags():
    out = CliRunner().invoke(app, ["email", "--help"]).output
    assert "--check-open-relay" in out and "--mx-port" in out and "never" in out.lower()
    assert "--check-open-relay" not in CliRunner().invoke(app, ["full", "--help"]).output


# ── methods / directory listing / security.txt ────────────────────────────

from esuls.probe import check_exposed, check_methods, check_paths


def mclient(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_methods_dangerous_advertised_is_medium_and_no_write_sent():
    methods = []

    def h(req):
        methods.append(req.method)
        if req.method == "OPTIONS":
            return httpx.Response(200, headers={"Allow": "GET, HEAD, PUT, DELETE, OPTIONS",
                                                "Access-Control-Allow-Methods": "GET, PATCH"})
        return httpx.Response(405)
    rep = await check_methods("https://site.test", client=mclient(h))
    t = by_title(rep)
    assert t["Dangerous HTTP method advertised: PUT"].severity == "medium"
    assert "Dangerous HTTP method advertised: PATCH" in t and "Dangerous HTTP method advertised: DELETE" in t
    assert "not confirmed writable" in t["Dangerous HTTP method advertised: PUT"].detail
    assert set(methods) == {"OPTIONS", "TRACE"} and rep.exit_code == 1       # no PUT/DELETE/PATCH ever sent


async def test_methods_safe_allow_and_trace_echo():
    safe = await check_methods("https://site.test", client=mclient(
        lambda r: httpx.Response(200, headers={"Allow": "GET, HEAD, OPTIONS"}) if r.method == "OPTIONS" else httpx.Response(405)))
    assert not any(f.severity != "info" for f in safe.findings) and safe.exit_code == 0

    def echo(req):
        if req.method == "TRACE":
            return httpx.Response(200, text="TRACE / HTTP/1.1\r\n" + "\r\n".join(f"{k}: {v}" for k, v in req.headers.items()))
        return httpx.Response(200)
    rep = await check_methods("https://site.test", client=mclient(echo))
    assert by_title(rep)["TRACE enabled (XST)"].severity == "medium"
    notecho = await check_methods("https://site.test", client=mclient(lambda r: httpx.Response(200, text="hello")))
    assert "TRACE enabled (XST)" not in by_title(notecho)


async def test_paths_directory_listing_is_low():
    def h(req):
        if req.url.path == "/backup/":
            return httpx.Response(200, text="<html><head><title>Index of /backup/</title></head><body><h1>Index of /backup/</h1>" + "x" * 300)
        if req.url.path == "/admin":
            return httpx.Response(200, text="<html>login form " + "y" * 300)
        return httpx.Response(404, text="nf")
    rep = await check_paths("https://site.test", client=mclient(h), wordlist=["backup/", "admin"])
    t = by_title(rep)
    assert t["Directory listing enabled at /backup/"].severity == "low"
    assert "Directory listing enabled at /admin" not in t and rep.exit_code == 1


async def test_paths_autoindex_catch_all_is_still_dropped_by_baseline():
    h = lambda r: httpx.Response(200, text="<title>Index of /</title>" + "z" * 100)
    rep = await check_paths("https://site.test", client=mclient(h), wordlist=["backup/"])
    assert rep.findings == []


def test_autoindex_signatures():
    from esuls.probe.paths import is_autoindex
    assert is_autoindex(b"<html><head><title>Index of /x</title>")
    assert is_autoindex(b"<title>Directory listing for /</title>")
    assert is_autoindex(b'<a href="?sort=namedirfirst">Name</a>')
    assert not is_autoindex(b"<html><title>My site</title>")


async def test_security_txt_present_info_and_absent_silent():
    def h(req):
        if req.url.path == "/.well-known/security.txt":
            return httpx.Response(200, text="Contact: mailto:security@example.it\nExpires: 2030-01-01T00:00:00Z\n")
        return httpx.Response(404)
    rep = await check_exposed("https://site.test", client=mclient(h))
    f = by_title(rep)["security.txt present (good practice)"]
    assert f.severity == "info" and rep.exit_code == 0
    none = await check_exposed("https://site.test", client=mclient(lambda r: httpx.Response(404)))
    assert none.findings == []
    html = await check_exposed("https://site.test", client=mclient(lambda r: httpx.Response(200, text="<html>Contact: x</html>")))
    assert html.findings == []


async def test_full_wiring_runs_new_checks_and_skips_domain_checks_on_loopback():
    from esuls.probe import run_full_probe
    rep = await run_full_probe("http://127.0.0.1:9", client=mclient(lambda r: httpx.Response(404, text="nf")),
                               wordlist=["a"], ratelimit_requests=2, auth_max_attempts=2, ports=[9],
                               options=ProbeOptions(timeout=1))
    assert "methods" in rep.stats
    skipped = {f.check for f in rep.findings if "skipped" in f.title}
    assert {"dns", "subdomains", "email"} <= skipped
    assert not any(f.check == "email" and "relay" in f.title.lower() for f in rep.findings)
