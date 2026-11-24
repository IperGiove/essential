import asyncio
import json
import sys
from functools import wraps

import click

from . import (
    ProbeOptions, check_cors, check_exposed, check_headers, check_paths,
    check_ratelimit, run_full_probe,
)
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


@app.command("ratelimit", help="Burst requests and look for 429/403/503. " + EXIT_HELP)
@shared_options
@_ratelimit_options
async def ratelimit_cmd(target, options, rl_path, rl_method, rl_data, rl_requests):
    return await check_ratelimit(target, options, path=rl_path, method=rl_method,
                                 data=rl_data, requests=rl_requests)


@app.command("cors", help="CORS reflection / null / wildcard-with-credentials. " + EXIT_HELP)
@shared_options
async def cors_cmd(target, options):
    return await check_cors(target, options)


@app.command("full", help="Run every check, one merged report. " + EXIT_HELP)
@shared_options
@_wordlist_options
@_ratelimit_options
async def full_cmd(target, options, wordlist, extensions, recursive, depth,
                   rl_path, rl_method, rl_data, rl_requests):
    return await run_full_probe(
        target, options, wordlist=load_wordlist(wordlist) if wordlist else None,
        extensions=_exts(extensions), recursive=recursive, depth=depth,
        ratelimit_path=rl_path, ratelimit_method=rl_method, ratelimit_data=rl_data,
        ratelimit_requests=rl_requests)
