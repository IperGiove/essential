from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, List, Literal, Optional, Union

from .content import read_pdf_content, read_pdf_revision_content
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
    # One SignatureCheck per embedded signature; None when signatures were not
    # verified (unsigned file, verification disabled, or pyHanko missing).
    signatures: Optional[list] = None
    # {"fonts": [...], "images": [...], "revisions": [...]} — page-content
    # inventory and what each incremental update added (see content.py).
    content: Optional[dict] = None

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
            "signatures": (
                None if self.signatures is None
                else [s.to_dict() for s in self.signatures]
            ),
            "content": self.content,
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


def _check_signatures(*, summary, signatures=None, signature_skip=None,
                      signature_read_error=None, **_) -> List[Finding]:
    """Turn per-signature crypto results into findings.

    - integrity or CMS failure, revocation, or post-signing changes beyond
      what the signature permits → critical (the signed content is not what
      the file now shows, or the signature is forged);
    - cryptographically sound but not chaining to a trust root → warning
      (anyone can mint a self-signed cert with any name on it);
    - not verified at all on a file that has signatures → info.
    """
    if signature_read_error:
        return [Finding(
            severity="warning",
            category="signature",
            code="signature.read_error",
            message=f"{summary.get('signatures')} signature(s) present but pyHanko cannot parse the file: {signature_read_error}",
            evidence={"error": signature_read_error},
        )]
    if signatures is None:
        if summary.get("signatures") and signature_skip:
            return [Finding(
                severity="info",
                category="signature",
                code="signature.not_verified",
                message=f"{summary['signatures']} signature(s) present but not cryptographically verified: {signature_skip}",
                evidence={"reason": signature_skip},
            )]
        return []

    out: List[Finding] = []
    for s in signatures:
        who = s.field_name or "?"
        ev = {"field": s.field_name, "signer": s.signer,
              "signer_cert_sha256": s.signer_cert_sha256}
        if s.error:
            out.append(Finding(
                severity="critical", category="signature",
                code="signature.unparseable",
                message=f"signature {who!r} could not be validated: {s.error}",
                evidence={**ev, "error": s.error},
            ))
            continue
        if not s.intact:
            out.append(Finding(
                severity="critical", category="signature",
                code="signature.digest_mismatch",
                message=f"signature {who!r}: signed bytes were altered (digest mismatch)",
                evidence=ev,
            ))
        if not s.valid:
            out.append(Finding(
                severity="critical", category="signature",
                code="signature.invalid",
                message=f"signature {who!r}: CMS signature does not verify against the signer key",
                evidence=ev,
            ))
        if s.revoked:
            out.append(Finding(
                severity="critical", category="signature",
                code="signature.revoked",
                message=f"signature {who!r}: signer certificate is revoked",
                evidence=ev,
            ))
        # Post-signing changes. modification_level is None when the diff
        # analysis could not run; docmdp_ok is the verdict against the
        # signature's own permission policy.
        if (s.intact and s.valid) and (
            s.docmdp_ok is False or s.coverage == "UNCLEAR"
        ):
            out.append(Finding(
                severity="critical", category="signature",
                code="signature.modified_after_signing",
                message=(
                    f"signature {who!r}: document modified after signing beyond what "
                    f"the signature allows (coverage={s.coverage}, "
                    f"modification={s.modification_level})"
                ),
                evidence={**ev, "coverage": s.coverage,
                          "modification_level": s.modification_level},
            ))
        if s.intact and s.valid and not s.revoked and not s.trusted:
            out.append(Finding(
                severity="warning", category="signature",
                code="signature.untrusted",
                message=(
                    f"signature {who!r} is cryptographically valid but the signer "
                    f"({s.signer}) does not chain to a trusted root"
                    + (f" [{s.trust_problem}]" if s.trust_problem else "")
                ),
                evidence={**ev, "issuer": s.issuer, "trust_problem": s.trust_problem},
            ))
        if s.timestamp_trusted is False:
            out.append(Finding(
                severity="warning", category="signature",
                code="signature.timestamp_untrusted",
                message=f"signature {who!r}: embedded timestamp token is not valid/trusted",
                evidence=ev,
            ))
    # Revisions appended after the LAST signature are signed by nobody. When
    # pyHanko's diff policy deems them harmless (DSS/LTV data, form filling,
    # but also Info-dict metadata edits) no signature fails — yet a viewer may
    # show content/metadata nobody signed. Surface it without moving the verdict.
    if signatures and not any(s.coverage == "ENTIRE_FILE" for s in signatures):
        last = signatures[-1]
        if last.intact and last.valid and last.docmdp_ok is not False:
            out.append(Finding(
                severity="info", category="signature",
                code="signature.unsigned_updates",
                message=(
                    f"incremental update(s) after the last signature "
                    f"({last.field_name!r}) are not covered by any signature "
                    f"(classified as {last.modification_level})"
                ),
                evidence={"field": last.field_name,
                          "modification_level": last.modification_level},
            ))
    return out


def _fmt_fonts(fonts: list) -> str:
    return ", ".join(
        f"{f['base_font']} (p.{','.join(map(str, f.get('pages', [f['page']])))})"
        for f in fonts
    )


def _check_revision_content(*, content=None, **_) -> List[Finding]:
    """Fonts/images that a LATER revision added to page content.

    New page fonts mean new text was typed onto the document; a new image
    XObject means something was pasted on it (logo, stamp, a patch over a
    number). Annotation appearances (signatures, form fields, comments) are
    excluded upstream, so these are edits of the page itself.
    """
    if not content:
        return []
    out: List[Finding] = []
    for rev in content.get("revisions") or []:
        n = rev["revision"]
        if rev.get("added_fonts"):
            out.append(Finding(
                severity="warning", category="content",
                code="content.fonts_added_in_update",
                message=f"revision {n} added font(s) to page content: {_fmt_fonts(rev['added_fonts'])}",
                evidence={"revision": n, "fonts": rev["added_fonts"]},
            ))
        if rev.get("modified_fonts"):
            out.append(Finding(
                severity="warning", category="content",
                code="content.fonts_modified_in_update",
                message=(
                    f"revision {n} rewrote existing font object(s) in place: "
                    + ", ".join(f"{f['previous_base_font']} → {f['base_font']}"
                                for f in rev["modified_fonts"])
                ),
                evidence={"revision": n, "fonts": rev["modified_fonts"]},
            ))
        if rev.get("added_images"):
            imgs = rev["added_images"]
            out.append(Finding(
                severity="warning", category="content",
                code="content.images_added_in_update",
                message=(
                    f"revision {n} added {len(imgs)} image(s) to page content: "
                    + ", ".join(f"{i['width']}x{i['height']} (p.{','.join(map(str, i['pages']))})"
                                for i in imgs)
                ),
                evidence={"revision": n, "images": imgs},
            ))
    return out


def _check_fonts(*, content=None, **_) -> List[Finding]:
    """Font mixes that betray a second author, even in a single-revision file
    (a re-saved file has no revisions left to diff)."""
    if not content or not content.get("fonts"):
        return []
    out: List[Finding] = []
    fonts = content["fonts"]

    # Two distinct subsets of one face, same font type, on one page: the
    # original subset may have lacked glyphs someone needed later. Only INFO:
    # Chromium/Skia routinely emits several subsets of one face per page, as
    # does Acrobat when combining files. Different TYPES of the same face are
    # skipped entirely (Word writes Arial as TrueType + Type0).
    by_page: dict = {}
    for f in fonts:
        if f["subset_prefix"]:
            by_page.setdefault((f["page"], f["family"], f["subtype"]), set()).add(f["subset_prefix"])
    dup_pages: dict = {}
    for (page, family, subtype), prefixes in by_page.items():
        if len(prefixes) > 1:
            dup_pages.setdefault((family, subtype), []).append((page, sorted(prefixes)))
    for (family, subtype), hits in dup_pages.items():
        out.append(Finding(
            severity="info", category="content",
            code="content.duplicate_font_subset",
            message=(
                f"font {family} ({subtype}) embedded as several subsets on the same page: "
                + "; ".join(f"p.{p}: {'/'.join(pr)}" for p, pr in hits)
            ),
            evidence={"family": family, "subtype": subtype,
                      "pages": [{"page": p, "subset_prefixes": pr} for p, pr in hits]},
        ))

    # Non-embedded, non-standard font next to embedded ones: the original
    # producer embedded its fonts; something else added text relying on a
    # font installed on the editing machine.
    if any(f["embedded"] for f in fonts):
        foreign = sorted({f["base_font"] for f in fonts
                          if not f["embedded"] and not f["standard14"] and f["base_font"]})
        if foreign:
            out.append(Finding(
                severity="info", category="content",
                code="content.mixed_font_embedding",
                message=f"non-embedded font(s) {', '.join(foreign)} alongside embedded fonts",
                evidence={"fonts": foreign},
            ))
    return out


def _check_images(*, content=None, **_) -> List[Finding]:
    if not content or not content.get("images"):
        return []
    hits = [i for i in content["images"] if i.get("editor")]
    if not hits:
        return []
    editors = sorted({i["editor"] for i in hits})
    return [Finding(
        severity="warning", category="content",
        code="content.image_editor_metadata",
        message=(
            f"{len(hits)} image(s) carry image-editor metadata ({', '.join(editors)}): "
            + ", ".join(f"{i['width']}x{i['height']} p.{i['page']}" for i in hits)
        ),
        evidence={"editors": editors, "images": hits},
    )]


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
    _check_signatures,
    _check_revision_content,
    _check_fonts,
    _check_images,
]


def check_pdf(
    pdf_path: Union[str, Path],
    *,
    profile: str = "default",
    verify_signatures: bool = True,
    trust_roots: Optional[Iterable] = None,
) -> PdfReport:
    """
    Run all legitimacy checks on a PDF and return a PdfReport with verdict.

    `profile` is reserved for future use (e.g., per-issuer rule sets); for
    now there is one default profile that runs the full check catalogue.

    When the file carries signatures and `verify_signatures` is true, each
    one is validated cryptographically with pyHanko (the `pdf-sign` extra);
    without pyHanko the check degrades to an info finding. `trust_roots` is
    forwarded to `verify_pdf_signatures`; without it no signer is trusted, so
    a signed file is at best "suspicious" (signature.untrusted).
    """
    pdf_path = Path(pdf_path)
    summary = read_pdf_summary(pdf_path)
    info = read_pdf_metadata(pdf_path)
    xmp = read_pdf_xmp(pdf_path)
    revisions = read_pdf_revisions(pdf_path)
    dynamic = read_pdf_dynamic(pdf_path)

    signatures = None
    signature_skip: Optional[str] = None
    signature_read_error: Optional[str] = None
    if summary.get("signatures"):
        if not verify_signatures:
            signature_skip = "verification disabled"
        else:
            try:
                from .signatures import verify_pdf_signatures
                import pyhanko  # noqa: F401  (probe the optional extra)
            except ImportError:
                signature_skip = "pyHanko not installed (pip install 'esuls[pdf-sign]')"
            else:
                try:
                    signatures = verify_pdf_signatures(pdf_path, trust_roots=trust_roots)
                except (TypeError, OSError):
                    raise  # bad trust_roots argument: caller error
                except Exception as e:
                    # pypdf counted signatures but pyHanko cannot parse the
                    # file: on untrusted input that is itself a red flag.
                    signature_read_error = f"{type(e).__name__}: {e}"

    content: Optional[dict] = None
    try:
        content = {
            **read_pdf_content(pdf_path),
            "revisions": (read_pdf_revision_content(pdf_path)
                          if revisions["eof_count"] > 1 else []),
        }
    except Exception as e:
        content_error = e
    else:
        content_error = None

    findings: List[Finding] = []
    if content_error is not None:
        findings.append(Finding(
            severity="info", category="internal", code="internal.check_error",
            message=f"content inventory failed: {content_error}",
            evidence={"check": "content", "error": str(content_error)},
        ))
    for check in _CHECKS:
        try:
            findings.extend(check(
                info=info, xmp=xmp,
                summary=summary, revisions=revisions, dynamic=dynamic,
                signatures=signatures, signature_skip=signature_skip,
                signature_read_error=signature_read_error,
                content=content,
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
        signatures=signatures,
        content=content,
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
