"""Service fingerprinting in `ports`: fake services in threads, read-only by default."""
import socket
import ssl
import threading

import pytest

from esuls.probe import ProbeOptions, check_ports
from esuls.probe.cli import app
from click.testing import CliRunner

OPTS = ProbeOptions(timeout=3)


class Fake:
    """Scripted line-oriented server. replies: {command-prefix(upper): bytes}. Records all received bytes."""

    def __init__(self, greeting: bytes, replies: dict, ctx: ssl.SSLContext | None = None, silent=False):
        self.greeting, self.replies, self.ctx, self.silent = greeting, replies, ctx, silent
        self.received: list[bytes] = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self._stop = False
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while not self._stop:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c):
        try:
            c.settimeout(5)
            if self.ctx:
                c = self.ctx.wrap_socket(c, server_side=True)
            if self.silent:
                threading.Event().wait(6)
                return
            c.sendall(self.greeting)
            f = c.makefile("rb")
            while True:
                line = f.readline()
                if not line:
                    return
                self.received.append(line)
                cmd = line.decode("latin-1").strip().upper()
                if cmd.endswith("QUIT") or cmd.endswith("LOGOUT"):
                    c.sendall(b"221 bye\r\n")
                    return
                for prefix, rep in self.replies.items():
                    if cmd.startswith(prefix):
                        c.sendall(rep)
                        break
                else:
                    c.sendall(b"500 unknown\r\n")
        except (OSError, ssl.SSLError):
            pass
        finally:
            try:
                c.close()
            except OSError:
                pass

    def close(self):
        self._stop = True
        self.sock.close()


@pytest.fixture
def fake():
    made = []

    def make(*a, **k):
        s = Fake(*a, **k)
        made.append(s)
        return s
    yield make
    for s in made:
        s.close()


def by_title(rep):
    return {f.title: f for f in rep.findings}


def sent(srv):
    return b"".join(srv.received).decode()


FTP_BANNER = b"220 ProFTPD 1.3.8 Server (example) [10.0.0.1]\r\n"
FEAT_TLS = b"211-Features:\r\n AUTH TLS\r\n PBSZ\r\n211 End\r\n"
FEAT_PLAIN = b"211-Features:\r\n MDTM\r\n SIZE\r\n211 End\r\n"


async def test_ftp_without_auth_tls_is_medium_and_banner_captured(fake):
    srv = fake(FTP_BANNER, {"FEAT": FEAT_PLAIN})
    rep = await check_ports("127.0.0.1", OPTS, ports=[srv.port], service_map={srv.port: "ftp"})
    t = by_title(rep)
    f = t["FTP offers plaintext login (no AUTH TLS advertised)"]
    assert f.severity == "medium" and "ProFTPD 1.3.8" in f.detail
    assert "ProFTPD" in t[f"Open port {srv.port}/tcp (unknown service)"].detail
    assert sent(srv) == "FEAT\r\nQUIT\r\n"             # nothing but capability + quit
    assert rep.exit_code == 1


async def test_ftp_with_auth_tls_is_info(fake):
    srv = fake(FTP_BANNER, {"FEAT": FEAT_TLS})
    rep = await check_ports("127.0.0.1", OPTS, ports=[srv.port], service_map={srv.port: "ftp"})
    f = by_title(rep)["FTPS available (AUTH TLS); plaintext enforcement not checked"]
    assert f.severity == "info" and "USER" not in sent(srv) and rep.exit_code == 0


async def test_smtp_starttls_missing_and_present(fake):
    no = fake(b"220 mail.example.org ESMTP Postfix\r\n", {"EHLO": b"250-mail.example.org\r\n250 SIZE 100\r\n"})
    rep = await check_ports("127.0.0.1", OPTS, ports=[no.port], service_map={no.port: "smtp"})
    f = by_title(rep)["SMTP has no STARTTLS (plaintext only)"]
    assert f.severity == "medium" and "mail.example.org" in f.detail and "Postfix" in f.detail
    yes = fake(b"220 mail.example.org ESMTP\r\n", {"EHLO": b"250-mail.example.org\r\n250-STARTTLS\r\n250 8BITMIME\r\n"})
    rep = await check_ports("127.0.0.1", OPTS, ports=[yes.port], service_map={yes.port: "smtp"})
    assert by_title(rep)["SMTP offers STARTTLS"].severity == "info"
    assert sent(yes) == "EHLO probe.invalid\r\nQUIT\r\n" and "AUTH" not in sent(yes)


async def test_pop3_stls(fake):
    no = fake(b"+OK Dovecot ready.\r\n", {"CAPA": b"+OK\r\nUSER\r\nUIDL\r\n.\r\n"})
    rep = await check_ports("127.0.0.1", OPTS, ports=[no.port], service_map={no.port: "pop3"})
    assert by_title(rep)["POP3 has no STLS (plaintext only)"].severity == "medium"
    yes = fake(b"+OK Dovecot ready.\r\n", {"CAPA": b"+OK\r\nUSER\r\nSTLS\r\n.\r\n"})
    rep = await check_ports("127.0.0.1", OPTS, ports=[yes.port], service_map={yes.port: "pop3"})
    assert by_title(rep)["POP3 offers STLS"].severity == "info" and "USER" not in sent(yes)


async def test_imap_starttls(fake):
    no = fake(b"* OK IMAP4rev1 ready\r\n", {"A CAPABILITY": b"* CAPABILITY IMAP4rev1 IDLE\r\na OK done\r\n"})
    rep = await check_ports("127.0.0.1", OPTS, ports=[no.port], service_map={no.port: "imap"})
    assert by_title(rep)["IMAP has no STARTTLS (plaintext only)"].severity == "medium"
    yes = fake(b"* OK IMAP4rev1 ready\r\n", {"A CAPABILITY": b"* CAPABILITY IMAP4rev1 STARTTLS\r\na OK done\r\n"})
    rep = await check_ports("127.0.0.1", OPTS, ports=[yes.port], service_map={yes.port: "imap"})
    assert by_title(rep)["IMAP offers STARTTLS"].severity == "info"
    assert sent(yes) == "a CAPABILITY\r\na LOGOUT\r\n"


async def test_ssh_banner_only_sends_nothing(fake):
    srv = fake(b"SSH-2.0-OpenSSH_9.6p1 Ubuntu-3\r\n", {})
    rep = await check_ports("127.0.0.1", OPTS, ports=[srv.port], service_map={srv.port: "ssh"})
    f = next(f for f in rep.findings if f.title.startswith("SSH version"))
    assert f.severity == "info" and "OpenSSH_9.6p1" in f.title and srv.received == []


async def test_non_protocol_service_records_raw_banner_and_silent_port_does_not_hang(fake):
    weird = fake(b"HELLO I AM NOT FTP\r\n", {})
    rep = await check_ports("127.0.0.1", OPTS, ports=[weird.port], service_map={weird.port: "ftp"})
    f = by_title(rep)["FTP did not speak the expected protocol"]
    assert "HELLO I AM NOT FTP" in f.detail and f.severity == "info"
    silent = fake(b"", {}, silent=True)
    import time
    t0 = time.monotonic()
    rep = await check_ports("127.0.0.1", ProbeOptions(timeout=10), ports=[silent.port],
                            service_map={silent.port: "smtp"})
    assert time.monotonic() - t0 < 6 and rep.exit_code == 0     # read cap, not the 10s timeout


async def test_no_banner_is_bare_connect(fake):
    srv = fake(FTP_BANNER, {"FEAT": FEAT_PLAIN})
    rep = await check_ports("127.0.0.1", OPTS, ports=[srv.port], service_map={srv.port: "ftp"}, banner=False)
    assert [f.severity for f in rep.findings] == ["info"] and len(rep.findings) == 1
    assert srv.received == []
    assert "banner" not in rep.findings[0].detail


async def test_plaintext_auth_ftp_refused_and_accepted(fake):
    refused = fake(FTP_BANNER, {"FEAT": FEAT_TLS, "USER": b"530 Please use AUTH TLS\r\n"})
    rep = await check_ports("127.0.0.1", OPTS, ports=[refused.port], service_map={refused.port: "ftp"},
                            plaintext_auth=True)
    assert by_title(rep)["FTP plaintext login refused (TLS enforced)"].severity == "info"
    assert "PASS" not in sent(refused) and "USER anonymous" in sent(refused)
    accepted = fake(FTP_BANNER, {"FEAT": FEAT_TLS, "USER": b"331 Password required\r\n"})
    rep = await check_ports("127.0.0.1", OPTS, ports=[accepted.port], service_map={accepted.port: "ftp"},
                            plaintext_auth=True)
    assert by_title(rep)["FTP plaintext login accepted (TLS not enforced)"].severity == "medium"
    assert "PASS" not in sent(accepted)
    # default mode never sends USER
    default = fake(FTP_BANNER, {"FEAT": FEAT_TLS, "USER": b"331 x\r\n"})
    await check_ports("127.0.0.1", OPTS, ports=[default.port], service_map={default.port: "ftp"})
    assert "USER" not in sent(default)


async def test_plaintext_auth_smtp(fake):
    ehlo = b"250-mx\r\n250-STARTTLS\r\n250 AUTH LOGIN\r\n"
    ref = fake(b"220 mx ESMTP\r\n", {"EHLO": ehlo, "AUTH LOGIN": b"538 Encryption required\r\n"})
    rep = await check_ports("127.0.0.1", OPTS, ports=[ref.port], service_map={ref.port: "smtp"}, plaintext_auth=True)
    assert "SMTP plaintext login refused (TLS enforced)" in by_title(rep)
    acc = fake(b"220 mx ESMTP\r\n", {"EHLO": ehlo, "AUTH LOGIN": b"334 VXNlcm5hbWU6\r\n"})
    rep = await check_ports("127.0.0.1", OPTS, ports=[acc.port], service_map={acc.port: "smtp"}, plaintext_auth=True)
    assert by_title(rep)["SMTP plaintext AUTH accepted (TLS not enforced)"].severity == "medium"
    assert sent(acc).count("\r\n") == 3                      # EHLO, AUTH LOGIN, QUIT: no credentials


# ── implicit TLS ──────────────────────────────────────────────────────────

async def test_implicit_tls_port_is_info_and_does_not_hang(tmp_path, fake, monkeypatch):
    pytest.importorskip("cryptography")
    from esuls.tests.test_probe_fleet import make_cert
    cert, key = make_cert(tmp_path)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    srv = fake(b"* OK IMAP ready\r\n", {}, ctx=ctx)
    from esuls.probe import banner as B
    monkeypatch.setitem(B.IMPLICIT_TLS, srv.port, "imaps")
    rep = await check_ports("127.0.0.1", OPTS, ports=[srv.port])
    t = [f.title for f in rep.findings]
    assert any("speaks implicit TLS (TLSv1" in x for x in t)
    assert not any("plaintext" in x.lower() or "no STARTTLS" in x for x in t)
    assert rep.exit_code == 0
    # a plain (non-TLS) socket on an implicit-TLS port must not hang either
    plain = fake(b"220 hello\r\n", {})
    monkeypatch.setitem(B.IMPLICIT_TLS, plain.port, "smtps")
    rep = await check_ports("127.0.0.1", ProbeOptions(timeout=3), ports=[plain.port])
    assert any("TLS handshake failed" in f.title for f in rep.findings)


def test_cli_flags():
    r = CliRunner()
    out = r.invoke(app, ["ports", "--help"]).output
    assert "--no-banner" in out and "--check-plaintext-auth" in out and "more invasive" in out.lower()
    assert "--no-banner" in r.invoke(app, ["full", "--help"]).output
    assert "--check-plaintext-auth" not in r.invoke(app, ["full", "--help"]).output
