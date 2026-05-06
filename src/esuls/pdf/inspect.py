import hashlib
from datetime import datetime
from pathlib import Path
from typing import Optional, Union

from pypdf import PdfReader


def read_pdf_summary(pdf_path: Union[str, Path]) -> dict:
    """
    High-level identity of the PDF: page count, file size, header version,
    encryption flag, signature count, sha256, linearization flag.
    """
    pdf_path = Path(pdf_path)
    data = pdf_path.read_bytes()
    sha256 = hashlib.sha256(data).hexdigest()

    pdf_version = None
    if data.startswith(b"%PDF-"):
        nl = data.find(b"\n")
        end = nl if nl != -1 else 16
        pdf_version = data[5:end].decode("ascii", errors="replace").strip()

    linearized = b"/Linearized" in data[:1024]

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
    """
    pdf_path = Path(pdf_path)
    data = pdf_path.read_bytes()

    eof_count = data.count(b"%%EOF")
    last_eof = data.rfind(b"%%EOF")
    if last_eof == -1:
        trailing = len(data)
    else:
        trailing = len(data[last_eof + len(b"%%EOF"):].lstrip(b"\r\n"))

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
    """Parse a PDF date string `D:YYYYMMDDHHMMSS+ZZ'ZZ'` to a naive datetime
    (timezone offset stripped, since this is used only for ordering checks).

    The format is selected by the length of the date prefix. PDF dates are
    fixed-width per spec, and length-keyed dispatch avoids strptime greedy
    matches like `%Y%m%d%H` accepting 8-char `YYYYMMDD` strings as
    `year=YYYY, month=Y, day=Y, hour=YY` on lenient implementations.
    """
    if not s:
        return None
    s = str(s)
    if s.startswith("D:"):
        s = s[2:]
    s = s.replace("'", "")
    base = s
    for sep in ("+", "-", "Z"):
        if sep in base[8:]:
            base = base.split(sep, 1)[0]
            break
    fmt = _PDF_DATE_FORMATS.get(len(base))
    if fmt is None:
        return None
    try:
        return datetime.strptime(base, fmt)
    except ValueError:
        return None
