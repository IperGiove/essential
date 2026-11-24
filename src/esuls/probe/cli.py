import asyncio
import json
import sys
from functools import wraps

import click

from . import (
    ProbeOptions, check_auth, check_cors, check_dns, check_email, check_exposed, check_headers,
    check_paths, check_ports, check_ratelimit, check_tls, run_full_probe,
)
from .full import ALL_CHECKS
from .ports import parse_ports
from .paths import load_wordlist

EXIT_HELP = "Exit codes: 0 = nothing above info, 1 = findings low or higher, 3 = error."


def _parse_header(ctx, param, values):
    out = {}
    for v in values:
        if ":" not in v:
            raise click.BadParameter(f"expected 'Name: value', got {v!r}")
        k, val = v.split(":", 1)
        out[k.strip()] = val.strip()
    return out


def shared_options(fn):
    opts = [
        click.argument("target"),
        click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of text."),
        click.option("-o", "--output", "out_file", type=click.Path(dir_okay=False),
                     help="Write the report to FILE instead of stdout."),
        click.option("-c", "--concurrency", default=10, show_default=True, type=click.IntRange(min=1)),
        click.option("--timeout", default=10.0, show_default=True, type=float, help="Per-request seconds."),
        click.option("--delay", default=0.0, show_default=True, type=float,
                     help="Seconds to sleep before each request."),
        click.option("-H", "--header", "headers", multiple=True, callback=_parse_header,
                     metavar="'Name: value'", help="Extra request header (repeatable)."),
        click.option("--cookie", help="Cookie header value, e.g. 'sid=abc'."),
        click.option("--user-agent", default=ProbeOptions().user_agent, show_default=True),
        click.option("--insecure", is_flag=True, help="Skip TLS certificate verification."),
    ]
    for o in reversed(opts):
        fn = o(fn)

    @wraps(fn)
    def wrapper(*args, as_json, out_file, concurrency, timeout, delay, headers, cookie,
                user_agent, insecure, **kw):
        options = ProbeOptions(concurrency=concurrency, timeout=timeout, delay=delay,
                               headers=headers, cookie=cookie, user_agent=user_agent,
                               insecure=insecure)
        try:
            report = asyncio.run(fn(*args, options=options, **kw))
        except Exception as e:  # noqa: BLE001
            click.echo(f"error: {type(e).__name__}: {e}", err=True)
            sys.exit(3)
        text = json.dumps(report.to_dict(), indent=2, default=str) if as_json else report.render_text()
        if out_file:
            with open(out_file, "w", encoding="utf-8") as fh:
                fh.write(text + "\n")
        else:
            click.echo(text)
        sys.exit(report.exit_code)

    return wrapper


def _wordlist_options(fn):
    for o in reversed([
        click.option("-w", "--wordlist", type=click.Path(exists=True, dir_okay=False),
                     help="Custom wordlist file (default: built-in common.txt)."),
        click.option("-e", "--extensions", default="", help="Comma list for %EXT% entries, e.g. php,bak."),
        click.option("--recursive", is_flag=True, help="Descend into found directories."),
        click.option("--depth", default=2, show_default=True, type=click.IntRange(min=1)),
    ]):
        fn = o(fn)
    return fn


def _exts(s):
    return [e for e in (x.strip() for x in s.split(",")) if e]


def _ratelimit_options(fn):
    for o in reversed([
        click.option("--path", "rl_path", default="/", show_default=True),
        click.option("--method", "rl_method", default="GET", show_default=True),
        click.option("--data", "rl_data", default=None, help="Request body."),
        click.option("-n", "--requests", "rl_requests", default=60, show_default=True,
                     type=click.IntRange(min=1)),
    ]):
        fn = o(fn)
    return fn


def _auth_options(fn):
    for o in reversed([
        click.option("--auth-path", default="/login", show_default=True, help="Login endpoint."),
        click.option("--auth-method", default="POST", show_default=True),
        click.option("--user-field", "auth_user_field", default="username", show_default=True),
        click.option("--pass-field", "auth_pass_field", default="password", show_default=True),
        click.option("--username", "auth_username", default="esuls-probe@invalid.example", show_default=True),
        click.option("--content-type", "auth_content_type", type=click.Choice(["form", "json"]),
                     default="form", show_default=True),
        click.option("--max-attempts", "auth_max_attempts", default=25, show_default=True,
                     type=click.IntRange(min=1)),
    ]):
        fn = o(fn)
    return fn


def _ports_option(fn):
    return click.option("--ports", "ports_spec", default=None, callback=_parse_ports_cb,
                        help="Comma list and N-M ranges, e.g. 22,80,8000-8010 (default: ~40 common ports).")(fn)


def _parse_ports_cb(ctx, param, value):
    if value is None:
        return None
    try:
        return parse_ports(value)
    except ValueError as e:
        raise click.BadParameter(str(e))


def _selector_option(fn):
    return click.option("--selector", "selectors", multiple=True,
                        help="DKIM selector to look up (repeatable); none are guessed.")(fn)


def _banner_option(fn):
    return click.option("--no-banner", "no_banner", is_flag=True,
                        help="Bare connect only: do not read service banners/capabilities.")(fn)


@click.group()
@click.version_option()
def app():
    """esuls-probe — self-assessment web probe. Use it on YOUR OWN sites.

    \b
    Finds what is exposed and which defenses don't trigger.
    Exit codes: 0 = nothing above info, 1 = findings low or higher, 3 = error.
    """


@app.command("paths", help="Wordlist path discovery with soft-404 baseline. " + EXIT_HELP)
@shared_options
@_wordlist_options
async def paths_cmd(target, options, wordlist, extensions, recursive, depth):
    return await check_paths(target, options, wordlist=load_wordlist(wordlist) if wordlist else None,
                             extensions=_exts(extensions), recursive=recursive, depth=depth)


@app.command("exposed", help="Sensitive files (.git, .env, dumps, ...) with content validation. " + EXIT_HELP)
@shared_options
async def exposed_cmd(target, options):
    return await check_exposed(target, options)


@app.command("headers", help="Security headers, cookie flags, HTTP->HTTPS redirect. " + EXIT_HELP)
@shared_options
async def headers_cmd(target, options):
    return await check_headers(target, options)


@app.command("ratelimit", help="Burst requests and look for 429/403/503. "
             "Loopback targets (127.0.0.1, ::1, localhost) are often exempt from rate limiting by the "
             "server itself (e.g. hlweb's Limiter); without an X-Forwarded-For/Forwarded/X-Real-IP header "
             "'no limiting' is then only reported as info. Re-run with -H 'X-Forwarded-For: 203.0.113.7'. "
             "A burst where every answer is an error (e.g. 422) cannot be assessed. " + EXIT_HELP)
@shared_options
@_ratelimit_options
async def ratelimit_cmd(target, options, rl_path, rl_method, rl_data, rl_requests):
    return await check_ratelimit(target, options, path=rl_path, method=rl_method,
                                 data=rl_data, requests=rl_requests)


@app.command("cors", help="CORS reflection / null / wildcard-with-credentials. " + EXIT_HELP)
@shared_options
async def cors_cmd(target, options):
    return await check_cors(target, options)


@app.command("full", help="Run every check, one merged report. Each HTTP check runs under its own "
             "simulated client IP (X-Forwarded-For from 203.0.113.0/24) so the checks do not share a "
             "rate-limit bucket; your own -H 'X-Forwarded-For: A.B.C.D' is used as-is for the first HTTP "
             "check and the others vary inside its /24. " + EXIT_HELP)
@shared_options
@_wordlist_options
@_ratelimit_options
@_ports_option
@_banner_option
@_selector_option
@_auth_options
@click.option("--skip", "skips", multiple=True, type=click.Choice(list(ALL_CHECKS)),
              help="Skip a check (repeatable). 'auth' fires real lockouts: skip it on shared IPs.")
async def full_cmd(target, options, wordlist, extensions, recursive, depth,
                   rl_path, rl_method, rl_data, rl_requests, ports_spec, no_banner, selectors,
                   auth_path, auth_method, auth_user_field, auth_pass_field, auth_username,
                   auth_content_type, auth_max_attempts, skips):
    return await run_full_probe(
        target, options, wordlist=load_wordlist(wordlist) if wordlist else None,
        extensions=_exts(extensions), recursive=recursive, depth=depth,
        ratelimit_path=rl_path, ratelimit_method=rl_method, ratelimit_data=rl_data,
        ratelimit_requests=rl_requests, ports=ports_spec, banner=not no_banner, selectors=selectors,
        auth_path=auth_path, auth_method=auth_method, auth_user_field=auth_user_field,
        auth_pass_field=auth_pass_field, auth_username=auth_username,
        auth_content_type=auth_content_type, auth_max_attempts=auth_max_attempts,
        checks=[c for c in ALL_CHECKS if c not in skips])


@app.command("ports", help="TCP connect scan: which ports does the internet reach? Open datastores, admin "
             "APIs etc. are flagged; Redis gets one PING to detect no-auth. Known service ports (FTP, SMTP, POP3, IMAP, SSH) are fingerprinted read-only: banner + one capability command + quit, no authentication; implicit-TLS ports (465/993/995) get a TLS connect. HOST or URL. " + EXIT_HELP)
@shared_options
@_ports_option
@_banner_option
@click.option("--check-plaintext-auth", is_flag=True,
              help="MORE INVASIVE, off by default: after TLS is seen advertised, send one non-credential "
                   "login step (USER anonymous / AUTH LOGIN ...) to see whether TLS is enforced first. "
                   "No password is ever sent.")
async def ports_cmd(target, options, ports_spec, no_banner, check_plaintext_auth):
    return await check_ports(target, options, ports=ports_spec, banner=not no_banner,
                             plaintext_auth=check_plaintext_auth)


@app.command("tls", help="Certificate (expiry, SAN, trust) and TLS 1.0/1.1 acceptance. HOST or URL. " + EXIT_HELP)
@shared_options
@click.option("--port", type=click.IntRange(1, 65535), default=None, help="Default 443 (or the URL's port).")
async def tls_cmd(target, options, port):
    return await check_tls(target, options, port=port)


@app.command("email", help="SPF / DMARC / DKIM / MX DNS records of the domain (DNS reads only). " + EXIT_HELP)
@shared_options
@_selector_option
async def email_cmd(target, options, selectors):
    return await check_email(target, options, selectors=selectors)


@app.command("auth", help="Send wrong-credential logins until the lockout/rate limit fires, then STOP. "
             "WARNING: this triggers real defenses (fail2ban, WAF bans) and may lock YOUR source IP; "
             "only run it against systems you own. It stops as soon as a defense responds (cap: "
             "--max-attempts). Loopback targets are often exempt from rate limiting: without an "
             "X-Forwarded-For header a local target is reported as info only. " + EXIT_HELP)
@shared_options
@_auth_options
async def auth_cmd(target, options, auth_path, auth_method, auth_user_field, auth_pass_field,
                   auth_username, auth_content_type, auth_max_attempts):
    return await check_auth(target, options, path=auth_path, method=auth_method,
                            user_field=auth_user_field, pass_field=auth_pass_field,
                            username=auth_username, content_type=auth_content_type,
                            max_attempts=auth_max_attempts)


@app.command("dns", help="Authoritative DNS posture: open resolver (recursive query for an external name), "
             "zone transfer (AXFR) attempt per nameserver, DNSSEC presence. One query per NS; read-only. "
             "DOMAIN or URL. " + EXIT_HELP)
@shared_options
async def dns_cmd(target, options):
    return await check_dns(target, options)
