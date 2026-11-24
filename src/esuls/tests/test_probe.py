"""Unit tests for esuls.probe checks, all against httpx.MockTransport."""
import httpx
import pytest

from esuls.probe import (
    ProbeOptions, ProbeReport, check_cors, check_exposed, check_headers,
    check_paths, check_ratelimit, run_full_probe,
)
from esuls.probe.paths import expand_words, load_wordlist

T = "https://site.test"


def client_for(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def titles(report):
    return [f.title for f in report.findings]


# ── report ────────────────────────────────────────────────────────────────

def test_report_exit_codes_and_sorting():
    r = ProbeReport("x")
    assert r.exit_code == 0
    r.add("c", "info", "u", "i")
    assert r.exit_code == 0
    r.add("c", "high", "u", "h")
    r.add("c", "low", "u", "l")
    assert [f.severity for f in r.sorted_findings()] == ["high", "low", "info"]
    assert r.exit_code == 1
    r.errors.append("boom")
    assert r.exit_code == 3
    d = r.to_dict()
    assert d["max_severity"] == "high" and "high" in r.render_text().lower()


def test_wordlist_builtin_and_ext_expansion():
    words = load_wordlist()
    assert 200 <= len(words) <= 500 and "admin" in words
    assert expand_words(["a.%EXT%", "b"], ["php", ".bak"]) == ["a.php", "a.bak", "b"]
    assert expand_words(["a.%EXT%", "b"]) == ["b"]


# ── paths ─────────────────────────────────────────────────────────────────

async def test_paths_soft404_catch_all_yields_nothing():
    def h(req):  # every path 200 with a body that echoes the path
        return httpx.Response(200, text=f"<html>Sorry, nothing at {req.url.path} here</html>")
    rep = await check_paths(T, client=client_for(h), wordlist=["admin", "api", "x.%EXT%"], extensions=["php"])
    assert rep.findings == [] and rep.errors == []


async def test_paths_soft404_redirect_catch_all():
    def h(req):
        return httpx.Response(302, headers={"location": f"/login?next={req.url.path}"})
    rep = await check_paths(T, client=client_for(h), wordlist=["admin", "api"])
    assert rep.findings == []


async def test_paths_finds_real_paths_and_401_403():
    def h(req):
        return {"/admin": httpx.Response(200, text="<html>" + "a" * 500 + "</html>"),
                "/secret": httpx.Response(403, text="forbidden"),
                "/basic": httpx.Response(401)}.get(req.url.path, httpx.Response(404, text="nf"))
    rep = await check_paths(T, client=client_for(h), wordlist=["admin", "secret", "basic", "nope"])
    got = {f.title.split()[1]: f.severity for f in rep.findings}
    assert got == {"/admin": "info", "/secret": "info", "/basic": "info"}
    assert any("access restricted" in t for t in titles(rep))
    assert rep.stats["paths"]["requests"] == 3 + 4  # 3 baseline probes + 4 words


async def test_paths_recursive():
    def h(req):
        if req.url.path in ("/app", "/app/"):
            return httpx.Response(200, text="app" * 50)
        if req.url.path == "/app/hidden":
            return httpx.Response(200, text="deep content" * 20)
        return httpx.Response(404, text="nf")
    rep = await check_paths(T, client=client_for(h), wordlist=["app", "hidden"], recursive=True, depth=2)
    assert any("/app/hidden" in t for t in titles(rep))


async def test_unreachable_target_is_an_error():
    def h(req):
        raise httpx.ConnectError("refused")
    rep = await check_paths(T, client=client_for(h), wordlist=["a"])
    assert rep.errors and rep.exit_code == 3


# ── exposed ───────────────────────────────────────────────────────────────

async def test_exposed_catch_all_200_has_no_false_positives():
    def h(req):
        return httpx.Response(200, text="<!doctype html><html><body>Welcome</body></html>")
    rep = await check_exposed(T, client=client_for(h))
    assert rep.findings == []


async def test_exposed_real_git_head_and_env_redacted():
    def h(req):
        if req.url.path == "/.git/HEAD":
            return httpx.Response(200, text="ref: refs/heads/main\n")
        if req.url.path == "/.env":
            return httpx.Response(200, text="DB_PASSWORD=hunter2hunter2\nSECRET_KEY=abc123\n")
        if req.url.path == "/.DS_Store":
            return httpx.Response(200, content=b"\x00\x00\x00\x01Bud1" + b"\x00" * 30)
        if req.url.path == "/backup.zip":
            return httpx.Response(200, content=b"PK\x03\x04" + b"0" * 20)
        return httpx.Response(404)
    rep = await check_exposed(T, client=client_for(h))
    by = {f.title: f for f in rep.findings}
    assert by["Exposed git repository (.git/HEAD)"].severity == "high"
    assert ".DS_Store" in " ".join(by) and "backup.zip" in " ".join(by)
    env = by["Exposed environment file (.env)"]
    assert "hunter2" not in (env.evidence + env.detail) and "DB_PASSWORD" in env.evidence
    assert "abc123" not in env.evidence


async def test_exposed_validators_reject_wrong_content():
    def h(req):
        if req.url.path == "/.git/HEAD":
            return httpx.Response(200, text="<html>ref: not really</html>")
        if req.url.path == "/.env":
            return httpx.Response(200, text="just one line")
        if req.url.path == "/phpinfo.php":
            return httpx.Response(200, text="nothing")
        return httpx.Response(404)
    assert (await check_exposed(T, client=client_for(h))).findings == []


async def test_exposed_phpinfo_and_debug_page():
    def h(req):
        if req.url.path == "/phpinfo.php":
            return httpx.Response(200, text="<h1>PHP Version 8.2</h1>")
        if req.url.path.startswith("/") and len(req.url.path) > 8 and req.url.path not in ("/phpinfo.php",):
            return httpx.Response(404, text="<p>You're seeing this error because you have <code>DEBUG = True</code> in your settings</p>")
        return httpx.Response(404)
    rep = await check_exposed(T, client=client_for(h))
    t = titles(rep)
    assert "Exposed phpinfo()" in t and "Framework debug mode enabled" in t


# ── headers ───────────────────────────────────────────────────────────────

async def test_headers_all_missing():
    def h(req):
        if req.url.scheme == "http":
            return httpx.Response(200, text="plain")
        return httpx.Response(200, text="ok", headers=[
            ("Server", "nginx/1.18.0"), ("X-Powered-By", "PHP/7.4"),
            ("Set-Cookie", "sid=1; Path=/")])
    rep = await check_headers(T, client=client_for(h))
    t = " | ".join(titles(rep))
    for needle in ("HSTS", "Content-Security-Policy", "nosniff", "clickjacking", "Referrer-Policy",
                   "Permissions-Policy", "Server header", "X-Powered-By", "without Secure",
                   "without HttpOnly", "without SameSite", "HTTP does not redirect"):
        assert needle in t, needle


async def test_headers_all_present_is_clean():
    def h(req):
        if req.url.scheme == "http":
            return httpx.Response(301, headers={"location": "https://site.test/"})
        return httpx.Response(200, text="ok", headers=[
            ("Strict-Transport-Security", "max-age=63072000"),
            ("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'"),
            ("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "no-referrer"),
            ("Permissions-Policy", "camera=()"), ("Server", "nginx"),
            ("Set-Cookie", "sid=1; Secure; HttpOnly; SameSite=Lax")])
    rep = await check_headers(T, client=client_for(h))
    assert rep.findings == [], titles(rep)


# ── ratelimit ─────────────────────────────────────────────────────────────

async def test_ratelimit_none():
    rep = await check_ratelimit(T, client=client_for(lambda r: httpx.Response(200)), requests=20)
    assert [f.severity for f in rep.findings] == ["medium"]
    assert rep.stats["ratelimit"]["requests"] == 20


async def test_ratelimit_triggers_after_k():
    n = {"i": 0}

    def h(req):
        n["i"] += 1
        return httpx.Response(429, headers={"Retry-After": "30"}) if n["i"] > 10 else httpx.Response(200)
    rep = await check_ratelimit(T, ProbeOptions(concurrency=1), client=client_for(h), requests=30)
    (f,) = rep.findings
    assert f.severity == "info" and "#11" in f.title and "Retry-After: 30" in f.detail


# ── cors ──────────────────────────────────────────────────────────────────

async def test_cors_reflection_with_credentials_is_high():
    def h(req):
        o = req.headers.get("origin")
        return httpx.Response(200, headers={"Access-Control-Allow-Origin": o,
                                            "Access-Control-Allow-Credentials": "true"})
    rep = await check_cors(T, client=client_for(h))
    assert {f.severity for f in rep.findings} == {"high"}
    assert any("'null'" in t for t in titles(rep))


async def test_cors_reflection_without_credentials_is_medium():
    h = lambda req: httpx.Response(200, headers={"Access-Control-Allow-Origin": req.headers["origin"]})
    rep = await check_cors(T, client=client_for(h))
    assert rep.findings and {f.severity for f in rep.findings} == {"medium"}


async def test_cors_wildcard_with_credentials_and_clean():
    h = lambda req: httpx.Response(200, headers={"Access-Control-Allow-Origin": "*",
                                                 "Access-Control-Allow-Credentials": "true"})
    rep = await check_cors(T, client=client_for(h))
    assert any("Wildcard" in t for t in titles(rep))
    clean = await check_cors(T, client=client_for(lambda r: httpx.Response(200)))
    assert clean.findings == []


# ── full ──────────────────────────────────────────────────────────────────

async def test_full_survives_a_failing_check(monkeypatch):
    import esuls.probe.full as full

    async def boom(*a, **k):
        raise RuntimeError("kaput")
    monkeypatch.setattr(full, "check_cors", boom)
    rep = await run_full_probe(T, client=client_for(lambda r: httpx.Response(404, text="nf")),
                               wordlist=["a"], ratelimit_requests=5)
    assert any(e.startswith("cors: RuntimeError") for e in rep.errors)
    assert {"headers", "exposed", "paths", "ratelimit"} <= set(rep.stats)
    assert rep.exit_code == 3


# ── ratelimit: 403/503 only count after a normal answer ───────────────────

@pytest.mark.parametrize("status", [403, 503])
async def test_ratelimit_always_blocked_is_not_limited(status):
    rep = await check_ratelimit(T, client=client_for(lambda r: httpx.Response(status)), requests=20)
    (f,) = rep.findings
    assert f.severity == "info" and "could not be assessed" in f.title
    assert "kicked in" not in f.title and "No rate limiting" not in f.title


async def test_ratelimit_200_then_403_counts_as_limited():
    n = {"i": 0}

    def h(req):
        n["i"] += 1
        return httpx.Response(403) if n["i"] > 8 else httpx.Response(200)
    rep = await check_ratelimit(T, ProbeOptions(concurrency=1), client=client_for(h), requests=20)
    (f,) = rep.findings
    assert "#9 (403)" in f.title


async def test_ratelimit_429_always_counts_even_first():
    rep = await check_ratelimit(T, client=client_for(lambda r: httpx.Response(429)), requests=5)
    assert "kicked in at request #1 (429)" in rep.findings[0].title
