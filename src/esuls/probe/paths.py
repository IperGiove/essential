"""Wordlist path discovery with a mandatory soft-404 baseline."""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from importlib import resources
from typing import Iterable, Optional, Sequence

import httpx

from ..utils import run_parallel
from ._http import ProbeOptions, Resp, Session
from .report import ProbeReport

CHECK = "paths"
EXISTS_STATUSES = {200, 201, 202, 204, 301, 302, 303, 307, 308, 401, 403}


def load_wordlist(path: Optional[str] = None) -> list[str]:
    """Read a wordlist (built-in `common.txt` by default); skips blanks/comments."""
    if path is None:
        text = resources.files("esuls.probe").joinpath("wordlists/common.txt").read_text("utf-8")
    else:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def expand_words(words: Iterable[str], extensions: Sequence[str] = ()) -> list[str]:
    """Expand ``%EXT%`` per extension; entries with the placeholder are dropped
    when no extensions are given. Order-preserving, de-duplicated."""
    exts = [e.strip().lstrip(".") for e in extensions if e.strip()]
    out: dict[str, None] = {}
    for w in words:
        w = w.lstrip("/")
        if "%EXT%" in w:
            for e in exts:
                out[w.replace("%EXT%", e)] = None
        else:
            out[w] = None
    return list(out)


@dataclass
class _Sig:
    status: int
    length: int
    digest: str
    loc: str


def _sig(resp: Resp, req_path: str) -> _Sig:
    # Strip the echoed request path so "Not found: /xyz" pages hash identically.
    body = resp.body.replace(req_path.encode(), b"").replace(req_path.lstrip("/").encode(), b"")
    loc = resp.headers.get("location", "")
    loc = loc.replace(req_path, "{p}") if loc else ""
    return _Sig(resp.status, len(body), hashlib.sha1(body).hexdigest(), loc)


def _matches(base: _Sig, cand: _Sig) -> bool:
    if base.status != cand.status:
        return False
    if base.digest == cand.digest:
        return True
    if base.loc and base.loc == cand.loc:
        return True
    return abs(base.length - cand.length) <= max(16, int(base.length * 0.02))


async def _baseline(s: Session, prefix: str) -> Optional[list[_Sig]]:
    sigs = []
    for suffix in ("", "/", ".html"):
        token = uuid.uuid4().hex[:16]
        path = f"{prefix}{token}{suffix}"
        r = await s.fetch(path)
        if r is not None:
            sigs.append(_sig(r, "/" + path.lstrip("/")))
    return sigs or None


async def check_paths(
    target: str,
    options: Optional[ProbeOptions] = None,
    *,
    client: Optional[httpx.AsyncClient] = None,
    wordlist: Optional[Iterable[str]] = None,
    extensions: Sequence[str] = (),
    recursive: bool = False,
    depth: int = 2,
) -> ProbeReport:
    """Discover existing paths. 401/403 count as "exists"; soft-404s are dropped."""
    options = options or ProbeOptions()
    report = ProbeReport(target=target)
    words = expand_words(load_wordlist() if wordlist is None else wordlist, extensions)
    async with Session(target, options, client) as s:
        report.target = s.target
        prefixes = [""]
        levels = max(depth, 1) if recursive else 1
        seen: set[str] = set()
        for _ in range(levels):
            nxt: list[str] = []
            for prefix in prefixes:
                for path, resp in await _scan(s, prefix, words, options):
                    if path in seen:
                        continue
                    seen.add(path)
                    note = " (access restricted)" if resp.status in (401, 403) else ""
                    report.add(CHECK, "info", s.url(path), f"Found /{path} [{resp.status}]{note}",
                               f"status {resp.status}, {resp.size} bytes"
                               + (f", -> {resp.headers['location']}" if "location" in resp.headers else ""),
                               None)
                    last = path.rstrip("/").rsplit("/", 1)[-1]
                    if recursive and "." not in last:
                        nxt.append(path.rstrip("/") + "/")
            prefixes = nxt
            if not prefixes:
                break
        s.finalize(report, CHECK)
    return report


async def _scan(s: Session, prefix: str, words: list[str], options: ProbeOptions):
    baseline = await _baseline(s, prefix)
    if baseline is None:
        return []
    paths = [prefix + w for w in words]
    results = await run_parallel(*[s.fetch(p) for p in paths], limit=max(options.concurrency, 1))
    found = []
    for p, r in zip(paths, results):
        if not isinstance(r, Resp) or r.status not in EXISTS_STATUSES:
            continue
        cand = _sig(r, "/" + p)
        if any(_matches(b, cand) for b in baseline):
            continue
        found.append((p, r))
    return found
