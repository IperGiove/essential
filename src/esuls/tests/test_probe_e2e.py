"""End-to-end: `esuls-probe full --json` against a deliberately weak local site."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from click.testing import CliRunner

from esuls.probe.cli import app


class Weak(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *a):
        pass

    def _send(self, code, body=b"", extra=()):
        self.send_response(code)
        origin = self.headers.get("Origin")
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Credentials", "true")
        self.send_header("Server", "WeakServer/1.0")
        for k, v in extra:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/.git/HEAD":
            return self._send(200, b"ref: refs/heads/main\n")
        if self.path == "/":
            return self._send(200, b"<html>weak</html>", [("Set-Cookie", "sid=1")])
        if self.path == "/admin":
            return self._send(200, b"<html>" + b"admin panel " * 40 + b"</html>")
        self._send(404, b"not found")

    do_POST = do_GET

    def do_OPTIONS(self):
        self._send(204)


@pytest.fixture
def weak_site():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Weak)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def test_full_cli_against_weak_site(weak_site):
    res = CliRunner().invoke(app, ["full", weak_site, "--json", "-n", "20"])
    assert res.exit_code == 1, res.output
    data = json.loads(res.output)
    assert data["errors"] == []
    titles = [f["title"] for f in data["findings"]]
    checks = {f["check"] for f in data["findings"]}
    assert "Exposed git repository (.git/HEAD)" in titles
    assert "Missing Content-Security-Policy" in titles
    assert any("Arbitrary Origin is reflected" in t for t in titles)
    assert any(t.startswith("Found /admin") for t in titles)
    assert any("No rate limiting" in t for t in titles)
    assert {"exposed", "headers", "cors", "paths", "ratelimit"} <= checks
    assert data["max_severity"] == "high"


def test_cli_unreachable_exits_3():
    res = CliRunner().invoke(app, ["headers", "http://127.0.0.1:1", "--timeout", "2"])
    assert res.exit_code == 3


def test_cli_output_file_and_text(weak_site, tmp_path):
    out = tmp_path / "r.txt"
    res = CliRunner().invoke(app, ["exposed", weak_site, "-o", str(out), "-H", "X-Test: 1"])
    assert res.exit_code == 1
    assert ".git/HEAD" in out.read_text()
