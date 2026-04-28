from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Literal, Optional, Union

from .inspect import (
    parse_pdf_date,
    read_pdf_dynamic,
    read_pdf_revisions,
    read_pdf_summary,
)
from .metadata import read_pdf_metadata, read_pdf_xmp


Severity = Literal["info", "warning", "critical"]
Verdict = Literal["clean", "suspicious", "tampered"]


@dataclass
class Finding:
    severity: Severity
    category: str
    code: str
    message: str
    evidence: dict = field(default_factory=dict)


@dataclass
class PdfReport:
    path: Path
    verdict: Verdict
    findings: List[Finding]
    summary: dict
    info: Optional[dict]
    xmp: Optional[dict]
    revisions: dict
    dynamic: dict

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "verdict": self.verdict,
            "findings": [asdict(f) for f in self.findings],
            "summary": self.summary,
            "info": self.info,
            "xmp": _serialize_xmp(self.xmp),
            "revisions": self.revisions,
            "dynamic": self.dynamic,
        }

    def critical(self) -> List[Finding]:
        return [f for f in self.findings if f.severity == "critical"]

    def warnings(self) -> List[Finding]:
        return [f for f in self.findings if f.severity == "warning"]


def _serialize_xmp(xmp: Optional[dict]) -> Optional[dict]:
    if xmp is None:
        return None
    out = {}
    for k, v in xmp.items():
        if isinstance(v, datetime):
            out[k] = v.isoformat()
        elif isinstance(v, dict):
            out[k] = {kk: str(vv) for kk, vv in v.items()}
        elif isinstance(v, list):
            out[k] = [str(x) for x in v]
        else:
            out[k] = str(v)
    return out


# ─── individual checks (each takes pre-parsed surfaces, returns findings) ─

def _check_producer_xmp(*, info, xmp, **_) -> List[Finding]:
    if not (info and xmp):
        return []
    a, b = info.get("/Producer"), xmp.get("pdf_producer")
    if a and b and str(a).strip() != str(b).strip():
        return [Finding(
            severity="critical",
            category="coherence",
            code="coherence.producer_mismatch",
            message=f"Info /Producer ({a!r}) ≠ XMP pdf_producer ({b!r})",
            evidence={"info_producer": str(a), "xmp_producer": str(b)},
        )]
    return []


def _check_creator_xmp(*, info, xmp, **_) -> List[Finding]:
    if not (info and xmp):
        return []
    a, b = info.get("/Creator"), xmp.get("xmp_creator_tool")
    if a and b and str(a).strip() != str(b).strip():
        return [Finding(
            severity="warning",
            category="coherence",
            code="coherence.creator_mismatch",
            message=f"Info /Creator ({a!r}) ≠ XMP xmp_creator_tool ({b!r})",
            evidence={"info_creator": str(a), "xmp_creator_tool": str(b)},
        )]
    return []


def _check_keywords_xmp(*, info, xmp, **_) -> List[Finding]:
    if not (info and xmp):
        return []
    a, b = info.get("/Keywords"), xmp.get("pdf_keywords")
    if a and b and str(a).strip() != str(b).strip():
        return [Finding(
            severity="warning",
            category="coherence",
            code="coherence.keywords_mismatch",
            message=f"Info /Keywords ({a!r}) ≠ XMP pdf_keywords ({b!r})",
            evidence={"info_keywords": str(a), "xmp_keywords": str(b)},
        )]
    return []


def _check_create_date_xmp(*, info, xmp, **_) -> List[Finding]:
    if not (info and xmp):
        return []
    cd = parse_pdf_date(info.get("/CreationDate"))
    xcd = xmp.get("xmp_create_date")
    if not (cd and xcd and hasattr(xcd, "replace")):
        return []
    try:
        xcd_n = xcd.replace(microsecond=0, tzinfo=None)
    except Exception:
        return []
    if abs((xcd_n - cd).total_seconds()) > 60:
        return [Finding(
            severity="warning",
            category="coherence",
            code="coherence.create_date_mismatch",
            message=f"Info /CreationDate ({info['/CreationDate']}) ≠ XMP xmp_create_date ({xcd})",
            evidence={
                "info_create_date": str(info["/CreationDate"]),
                "xmp_create_date": xcd.isoformat(),
            },
        )]
    return []


def _check_mod_before_creation(*, info, **_) -> List[Finding]:
    if not info:
        return []
    cd = parse_pdf_date(info.get("/CreationDate"))
    md = parse_pdf_date(info.get("/ModDate"))
    if not (cd and md):
        return []
    delta = (cd - md).total_seconds()
    # Tolerate up to 60s of skew: clock drift between concurrent timestamp
    # writes is real (Canva itself produces files with ModDate one second
    # before CreationDate). Real tampering produces minute/hour-scale gaps.
    if delta > 60:
        return [Finding(
            severity="critical",
            category="dates",
            code="dates.mod_before_creation",
            message=f"/ModDate ({info['/ModDate']}) precedes /CreationDate ({info['/CreationDate']}) by {int(delta)}s — impossible",
            evidence={
                "creation_date": str(info["/CreationDate"]),
                "mod_date": str(info["/ModDate"]),
                "delta_seconds": delta,
            },
        )]
    return []


def _check_creation_in_future(*, info, **_) -> List[Finding]:
    if not info:
        return []
    cd = parse_pdf_date(info.get("/CreationDate"))
    if not cd:
        return []
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if (cd - now).total_seconds() > 3600:
        return [Finding(
            severity="critical",
            category="dates",
            code="dates.creation_in_future",
            message=f"/CreationDate ({info['/CreationDate']}) is in the future",
            evidence={"creation_date": str(info["/CreationDate"]), "now_utc": now.isoformat()},
        )]
    return []


def _check_incremental_updates(*, summary, revisions, **_) -> List[Finding]:
    if revisions["eof_count"] > 1 and summary["signatures"] == 0:
        return [Finding(
            severity="warning",
            category="structure",
            code="structure.incremental_updates",
            message=(
                f"%%EOF count = {revisions['eof_count']} on an unsigned file — "
                f"document was modified after creation"
            ),
            evidence={"eof_count": revisions["eof_count"]},
        )]
    return []


def _check_junk_after_eof(*, revisions, **_) -> List[Finding]:
    if revisions["trailing_bytes_after_eof"] > 4:
        return [Finding(
            severity="critical",
            category="structure",
            code="structure.junk_after_eof",
            message=f"{revisions['trailing_bytes_after_eof']} bytes appended after the last %%EOF",
            evidence={"trailing_bytes": revisions["trailing_bytes_after_eof"]},
        )]
    return []


def _check_id_missing(*, revisions, **_) -> List[Finding]:
    if revisions["id_original"] is None:
        return [Finding(
            severity="info",
            category="structure",
            code="structure.id_missing",
            message="/ID array missing from trailer",
            evidence={},
        )]
    return []


_CHECKS: List[Callable] = [
    _check_producer_xmp,
    _check_creator_xmp,
    _check_keywords_xmp,
    _check_create_date_xmp,
    _check_mod_before_creation,
    _check_creation_in_future,
    _check_incremental_updates,
    _check_junk_after_eof,
    _check_id_missing,
]


def check_pdf(
    pdf_path: Union[str, Path], *, profile: str = "default"
) -> PdfReport:
    """
    Run all legitimacy checks on a PDF and return a PdfReport with verdict.

    `profile` is reserved for future use (e.g., per-issuer rule sets); for
    now there is one default profile that runs the full check catalogue.
    """
    pdf_path = Path(pdf_path)
    summary = read_pdf_summary(pdf_path)
    info = read_pdf_metadata(pdf_path)
    xmp = read_pdf_xmp(pdf_path)
    revisions = read_pdf_revisions(pdf_path)
    dynamic = read_pdf_dynamic(pdf_path)

    findings: List[Finding] = []
    for check in _CHECKS:
        try:
            findings.extend(check(
                info=info, xmp=xmp,
                summary=summary, revisions=revisions, dynamic=dynamic,
            ))
        except Exception as e:
            findings.append(Finding(
                severity="info",
                category="internal",
                code="internal.check_error",
                message=f"check {check.__name__} crashed: {e}",
                evidence={"check": check.__name__, "error": str(e)},
            ))

    counts = Counter(f.severity for f in findings)
    if counts.get("critical", 0):
        verdict: Verdict = "tampered"
    elif counts.get("warning", 0):
        verdict = "suspicious"
    else:
        verdict = "clean"

    return PdfReport(
        path=pdf_path,
        verdict=verdict,
        findings=findings,
        summary=summary,
        info=info,
        xmp=xmp,
        revisions=revisions,
        dynamic=dynamic,
    )


def check_metadata_coherence(info, xmp) -> List[str]:
    """
    Legacy alias kept for backward compat with the old API: returns a flat
    list of human-readable strings for coherence checks (Info ↔ XMP and
    date order). Prefer `check_pdf` for new code.
    """
    findings: List[Finding] = []
    findings.extend(_check_producer_xmp(info=info, xmp=xmp))
    findings.extend(_check_creator_xmp(info=info, xmp=xmp))
    findings.extend(_check_keywords_xmp(info=info, xmp=xmp))
    findings.extend(_check_create_date_xmp(info=info, xmp=xmp))
    findings.extend(_check_mod_before_creation(info=info))
    return [f.message for f in findings]
