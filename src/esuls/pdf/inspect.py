import hashlib
import mmap
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Union

from pypdf import PdfReader


def read_pdf_summary(pdf_path: Union[str, Path]) -> dict:
    """
    High-level identity of the PDF: page count, file size, header version,
    encryption flag, signature count, sha256, linearization flag.

    Streams the file for sha256 and only reads the first 1 KB for the
    header inspection, so a multi-GB PDF does not get loaded into RAM
    (avoids OOM/DoS on untrusted input).
    """
    pdf_path = Path(pdf_path)

    # Single open for both head-read and sha256: seek(0) between the
    # two so file_digest hashes from the start. `hashlib.file_digest`
    # streams the file via `f.read()` from the current position.
    with pdf_path.open("rb") as f:
        head = f.read(1024)
        f.seek(0)
        sha256 = hashlib.file_digest(f, "sha256").hexdigest()

    pdf_version = None
    if head.startswith(b"%PDF-"):
        nl = head.find(b"\n")
        end = nl if nl != -1 else 16
        pdf_version = head[5:end].decode("ascii", errors="replace").strip()

    linearized = b"/Linearized" in head

    reader = PdfReader(str(pdf_path))
    pages = len(reader.pages)
    encrypted = bool(reader.is_encrypted)

    sig_count = 0
    try:
        root = reader.trailer["/Root"]
        if "/AcroForm" in root:
            af = root["/AcroForm"]
            for f in af.get("/Fields", []) or []:
                obj = f.get_object() if hasattr(f, "get_object") else f
                if obj.get("/FT") == "/Sig" and obj.get("/V"):
                    sig_count += 1
    except Exception:
        pass

    return {
        "file_size": pdf_path.stat().st_size,
        "sha256": sha256,
        "pdf_version": pdf_version,
        "pages": pages,
        "linearized": linearized,
        "encrypted": encrypted,
        "signatures": sig_count,
    }


def read_pdf_revisions(pdf_path: Union[str, Path]) -> dict:
    """
    Revision/integrity surface: %%EOF count (>1 = incremental updates),
    /Prev in trailer, /ID array (original vs current), bytes appended after
    the last %%EOF marker.

    Uses mmap so the OS pages-in only the regions actually scanned: a
    multi-GB PDF does not allocate process-side RAM, and the `%%EOF`
    search runs at filesystem-cache speed.
    """
    pdf_path = Path(pdf_path)
    file_size = pdf_path.stat().st_size

    eof_count = 0
    last_eof = -1
    if file_size > 0:
        with pdf_path.open("rb") as f:
            with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                pos = 0
                while True:
                    found = mm.find(b"%%EOF", pos)
                    if found < 0:
                        break
                    eof_count += 1
                    last_eof = found
                    pos = found + len(b"%%EOF")

    if last_eof == -1:
        trailing = file_size
    else:
        # bytes after last %%EOF, stripping leading CR/LF
        with pdf_path.open("rb") as f:
            f.seek(last_eof + len(b"%%EOF"))
            tail = f.read()
        trailing = len(tail.lstrip(b"\r\n"))

    reader = PdfReader(str(pdf_path))
    has_prev = False
    try:
        has_prev = "/Prev" in reader.trailer
    except Exception:
        pass

    id_orig = id_curr = None
    try:
        ids = reader.trailer.get("/ID")
        if ids:
            id_orig = bytes(ids[0]).hex()
            id_curr = bytes(ids[1]).hex()
    except Exception:
        pass

    return {
        "eof_count": eof_count,
        "has_prev_in_trailer": has_prev,
        "id_original": id_orig,
        "id_current": id_curr,
        "id_match": (id_orig == id_curr) if id_orig and id_curr else None,
        "trailing_bytes_after_eof": trailing,
    }


def read_pdf_dynamic(pdf_path: Union[str, Path]) -> dict:
    """
    Audit of dynamic / active content: AcroForm presence, auto-actions
    (/OpenAction, /AA), embedded files count, JavaScript actions count.
    """
    reader = PdfReader(str(pdf_path))
    try:
        root = reader.trailer["/Root"]
    except Exception:
        return {
            "has_acroform": False, "has_openaction": False,
            "has_additional_actions": False,
            "embedded_files": 0, "javascript_actions": 0,
        }

    has_acroform = "/AcroForm" in root
    has_openaction = "/OpenAction" in root
    has_aa = "/AA" in root

    embedded_count = 0
    js_count = 0
    try:
        names = root.get("/Names")
        if names:
            emb = names.get("/EmbeddedFiles")
            if emb:
                embedded_count = len(emb.get("/Names", []) or []) // 2
            js = names.get("/JavaScript")
            if js:
                js_count = len(js.get("/Names", []) or []) // 2
    except Exception:
        pass

    return {
        "has_acroform": has_acroform,
        "has_openaction": has_openaction,
        "has_additional_actions": has_aa,
        "embedded_files": embedded_count,
        "javascript_actions": js_count,
    }


_PDF_DATE_FORMATS = {
    14: "%Y%m%d%H%M%S",
    12: "%Y%m%d%H%M",
    10: "%Y%m%d%H",
    8: "%Y%m%d",
    6: "%Y%m",
    4: "%Y",
}


def parse_pdf_date(s: Optional[str]) -> Optional[datetime]:
    """Parse a PDF date string `D:YYYYMMDDHHMMSS+HH'mm'` to a naive UTC
    datetime: any declared timezone offset is applied and stripped so the
    result is directly comparable against `datetime.utcnow()`-style
    callers, and timestamps from different zones can be ordered without
    surprising naive-vs-aware TypeError.

    Dates without an offset are returned naive as-is (interpreted as
    already-UTC by callers — the PDF spec recommends an explicit offset,
    so the no-offset case is a producer that didn't follow the spec).

    The format is selected by the length of the date prefix. PDF dates
    are fixed-width per spec, and length-keyed dispatch avoids strptime
    greedy matches like `%Y%m%d%H` accepting 8-char `YYYYMMDD` strings
    as `year=YYYY, month=Y, day=Y, hour=YY` on lenient implementations.
    """
    if not s:
        return None
    s = str(s)
    if s.startswith("D:"):
        s = s[2:]
    s = s.replace("'", "")

    # Split off timezone suffix. Search starts at index 8 so the date's
    # leading digits (e.g. the "-" was never valid there in PDF dates,
    # but defensive) are never mistaken for the tz separator.
    base = s
    tz_seconds: Optional[int] = None
    for sep in ("Z", "+", "-"):
        idx = base.find(sep, 8)
        if idx > 0:
            tz_part = base[idx:]
            base = base[:idx]
            if tz_part == "Z":
                tz_seconds = 0
            else:
                sign = 1 if tz_part[0] == "+" else -1
                try:
                    hh = int(tz_part[1:3]) if len(tz_part) >= 3 else 0
                    mm = int(tz_part[3:5]) if len(tz_part) >= 5 else 0
                except ValueError:
                    return None
                tz_seconds = sign * (hh * 3600 + mm * 60)
            break

    fmt = _PDF_DATE_FORMATS.get(len(base))
    if fmt is None:
        return None
    try:
        dt = datetime.strptime(base, fmt)
    except ValueError:
        return None

    # Normalise to naive UTC so callers can compare against UTC `now`
    # without aware-vs-naive TypeError and without timezone false-positives
    # (e.g. a PDF created in +13:00 appearing to be "in the future" when
    # compared raw against UTC midnight).
    if tz_seconds is not None:
        dt = dt - timedelta(seconds=tz_seconds)
    return dt
