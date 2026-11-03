"""Cryptographic verification of PDF digital signatures (pyHanko).

`read_pdf_summary` only COUNTS signature fields; it cannot tell a genuine
signature from a forged blob, nor a signed file from one that was edited after
signing. This module answers those questions by actually validating each
embedded signature:

- integrity  — the digest over the signed `/ByteRange` matches the one in the
  CMS signed attributes (`intact`): any byte changed inside the signed range
  breaks it;
- crypto     — the CMS signature over those attributes verifies against the
  signer's public key (`valid`);
- trust      — the signer certificate chains to a trust root (`trusted`);
- coverage   — how much of the file the signature covers, and whether the
  incremental updates appended after it are benign (`modification_level`,
  `docmdp_ok`) according to pyHanko's difference analysis.

Needs the optional `pdf-sign` extra (`pip install 'esuls[pdf-sign]'`). pyHanko
is imported lazily inside the functions, so `import esuls.pdf` keeps working
without it; calling `verify_pdf_signatures` without it raises ImportError.

Validation is OFFLINE by design: no OCSP/CRL/AIA fetching. The URLs a
certificate points at are attacker-controlled on untrusted input, and network
I/O would make the result non-deterministic. Revocation is therefore checked
only against what is embedded in the file (soft-fail).
"""

import asyncio
import hashlib
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, List, Optional, Union

TrustRoot = Union[str, Path, bytes, Any]
"""A trust root: path to a PEM/DER file (may hold several certs), raw
PEM/DER bytes, an `asn1crypto.x509.Certificate`, or a
`cryptography.x509.Certificate`."""


@dataclass
class SignatureCheck:
    """Outcome of validating one embedded signature (or document timestamp)."""

    field_name: Optional[str]
    kind: str                         # "signature" | "doc_timestamp"
    signer: Optional[str]             # signer cert subject, human-readable
    signer_cert_sha256: Optional[str]
    issuer: Optional[str]
    signing_time: Optional[datetime]  # self-reported (or TSA time for doc TS)
    md_algorithm: Optional[str]
    intact: bool                      # digest over /ByteRange matches
    valid: bool                       # CMS signature verifies
    trusted: bool                     # chains to a trust root, key usage OK
    revoked: bool
    trust_problem: Optional[str]      # AdES sub-indication, when untrusted
    coverage: Optional[str]           # ENTIRE_FILE | ENTIRE_REVISION | ...
    modification_level: Optional[str] # NONE | LTA_UPDATES | FORM_FILLING | ...
    docmdp_ok: Optional[bool]
    timestamp_trusted: Optional[bool] # embedded signature timestamp, if any
    bottom_line: bool                 # pyHanko's overall judgement
    error: Optional[str] = None       # validation itself crashed

    def to_dict(self) -> dict:
        d = asdict(self)
        if self.signing_time is not None:
            d["signing_time"] = self.signing_time.isoformat()
        return d


def _load_trust_roots(roots: Iterable[TrustRoot]) -> list:
    from asn1crypto import x509 as asn1_x509
    from pyhanko.keys import load_certs_from_pemder_data

    out = []
    for r in roots:
        if isinstance(r, asn1_x509.Certificate):
            out.append(r)
        elif isinstance(r, (str, Path)):
            out.extend(load_certs_from_pemder_data(Path(r).read_bytes()))
        elif isinstance(r, (bytes, bytearray)):
            out.extend(load_certs_from_pemder_data(bytes(r)))
        elif hasattr(r, "public_bytes"):  # cryptography.x509.Certificate
            from cryptography.hazmat.primitives.serialization import Encoding
            out.append(asn1_x509.Certificate.load(r.public_bytes(Encoding.DER)))
        else:
            raise TypeError(f"unsupported trust root type: {type(r).__name__}")
    return out


def _validation_context(trust_roots, validation_time):
    from pyhanko_certvalidator import ValidationContext

    kwargs = dict(allow_fetching=False, revocation_mode="soft-fail")
    if validation_time is not None:
        kwargs["moment"] = validation_time
    # Always explicit, never the OS store: that list is TLS server CAs, not
    # document-signing trust (AATL / EU trusted lists), it differs between
    # machines, and pyHanko deprecates falling back to it. No roots given ⇒
    # nothing is trusted: a signature can still be intact and valid.
    kwargs["trust_roots"] = _load_trust_roots(trust_roots or ())
    return ValidationContext(**kwargs)


def _enum_name(v) -> Optional[str]:
    return None if v is None else getattr(v, "name", str(v))


def _str_or_none(v) -> Optional[str]:
    # pyHanko hands back its own TextStringObject: plain str keeps the result
    # serialisable and detached from the (closed) reader.
    return None if v is None else str(v)


def _cert_fields(cert) -> tuple:
    if cert is None:
        return None, None, None
    return (
        cert.subject.human_friendly,
        hashlib.sha256(cert.dump()).hexdigest(),
        cert.issuer.human_friendly,
    )


def _from_status(emb, status, kind: str) -> SignatureCheck:
    signer, fp, issuer = _cert_fields(status.signing_cert)
    if kind == "doc_timestamp":
        signing_time = getattr(status, "timestamp", None)
        ts_trusted = None
        # DocumentTimestampStatus has no bottom_line: mirror PdfSignatureStatus.
        bottom_line = bool(
            status.intact and status.valid and status.trusted
            and (status.docmdp_ok or status.modification_level is None)
        )
    else:
        signing_time = status.signer_reported_dt
        ts = status.timestamp_validity
        ts_trusted = None if ts is None else bool(ts.valid and ts.intact and ts.trusted)
        bottom_line = bool(status.bottom_line)
    return SignatureCheck(
        field_name=_str_or_none(emb.field_name),
        kind=kind,
        signer=signer,
        signer_cert_sha256=fp,
        issuer=issuer,
        signing_time=signing_time,
        md_algorithm=status.md_algorithm,
        intact=bool(status.intact),
        valid=bool(status.valid),
        trusted=bool(status.trusted),
        revoked=bool(status.revoked),
        trust_problem=_enum_name(status.trust_problem_indic),
        coverage=_enum_name(status.coverage),
        modification_level=_enum_name(status.modification_level),
        docmdp_ok=status.docmdp_ok,
        timestamp_trusted=ts_trusted,
        bottom_line=bottom_line,
    )


def _from_error(emb, kind: str, err: Exception) -> SignatureCheck:
    try:
        signer, fp, issuer = _cert_fields(emb.signer_cert)
    except Exception:
        signer = fp = issuer = None
    try:
        name = _str_or_none(emb.field_name)
    except Exception:
        name = None
    return SignatureCheck(
        field_name=name, kind=kind, signer=signer, signer_cert_sha256=fp,
        issuer=issuer, signing_time=None, md_algorithm=None,
        intact=False, valid=False, trusted=False, revoked=False,
        trust_problem=None, coverage=None, modification_level=None,
        docmdp_ok=None, timestamp_trusted=None, bottom_line=False,
        error=f"{type(err).__name__}: {err}",
    )


async def async_verify_pdf_signatures(
    pdf_path: Union[str, Path],
    *,
    trust_roots: Optional[Iterable[TrustRoot]] = None,
    validation_time: Optional[datetime] = None,
) -> List[SignatureCheck]:
    """Async form of `verify_pdf_signatures` (pyHanko's validator is async)."""
    from pyhanko.pdf_utils.reader import PdfFileReader
    from pyhanko.sign.validation.pdf_embedded import (
        async_validate_pdf_signature,
        async_validate_pdf_timestamp,
    )

    # Build the context once: loading roots (or the system store) is the
    # expensive part, and every signature in the file shares it.
    # A bad trust root (missing file, wrong type) raises here: that is a
    # caller error, not a property of the PDF.
    vc = _validation_context(trust_roots, validation_time)
    results: List[SignatureCheck] = []
    with Path(pdf_path).open("rb") as fh:
        reader = PdfFileReader(fh, strict=False)
        for emb in reader.embedded_signatures:
            is_ts = emb.sig_object_type == "/DocTimeStamp"
            kind = "doc_timestamp" if is_ts else "signature"
            try:
                if is_ts:
                    status = await async_validate_pdf_timestamp(emb, vc)
                else:
                    status = await async_validate_pdf_signature(emb, vc)
                results.append(_from_status(emb, status, kind))
            except Exception as e:
                results.append(_from_error(emb, kind, e))
    return results


def verify_pdf_signatures(
    pdf_path: Union[str, Path],
    *,
    trust_roots: Optional[Iterable[TrustRoot]] = None,
    validation_time: Optional[datetime] = None,
) -> List[SignatureCheck]:
    """
    Cryptographically validate every signature and document timestamp
    embedded in a PDF. Returns one `SignatureCheck` per signature, in file
    order; an unsigned PDF yields `[]`.

    `trust_roots` — the ONLY certificates trust is anchored on: your
    issuer's CA, or the exact self-signed cert you expect. The OS store is
    never consulted, so with `None` every signature reports `trusted=False`
    (integrity and cryptographic validity are still checked).

    `validation_time` — evaluate certificate validity at this moment instead
    of now (e.g. to check a signature whose cert has since expired).

    A signature that pyHanko cannot even parse is reported with `error` set
    and every flag False, rather than raising: on untrusted input a malformed
    signature is a finding, not a crash.

    Safe to call from inside a running event loop (it then validates on a
    worker thread); in async code prefer `async_verify_pdf_signatures`.
    """
    coro = async_verify_pdf_signatures(
        pdf_path, trust_roots=trust_roots, validation_time=validation_time,
    )
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    box: dict = {}

    def _worker():
        try:
            box["value"] = asyncio.run(coro)
        except BaseException as e:  # re-raised on the caller's thread
            box["error"] = e

    t = threading.Thread(target=_worker, name="esuls-pdf-verify")
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]
    return box["value"]
