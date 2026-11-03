"""
Cryptographic signature verification (esuls.pdf.signatures + check_pdf).

Every PDF here is synthetic: a blank page, signed on the fly by pyHanko
with self-signed certificates minted by `cryptography`. No fixture files, no
network: the validator runs offline, and trust is anchored only on the roots
each test passes explicitly.
"""
import asyncio
import hashlib
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("pyhanko")

from asn1crypto import keys as asn1_keys, x509 as asn1_x509
from click.testing import CliRunner
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from pyhanko.pdf_utils.generic import (
    ArrayObject, DictionaryObject, NameObject, NumberObject, StreamObject, pdf_string,
)
from pyhanko.pdf_utils.writer import PageObject, PdfFileWriter
from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
from pyhanko.sign import signers
from pyhanko_certvalidator.registry import SimpleCertificateStore
from pyhanko.sign.timestamps.dummy_client import DummyTimeStamper

from esuls.pdf import check_pdf, verify_pdf_signatures
from esuls.pdf.cli import app
from esuls.pdf.signatures import SignatureCheck, async_verify_pdf_signatures


NOW = datetime.now(timezone.utc)


# ─── synthetic PKI ────────────────────────────────────────────────────────

class Identity:
    """A certificate + its key, in both library flavours.

    Self-signed by default; pass `issuer` to get a leaf issued by that CA.
    """

    def __init__(self, cn: str, *, issuer: "Identity | None" = None,
                 not_before=None, not_after=None, tsa=False, rsa_key=False):
        self.key = (
            rsa.generate_private_key(public_exponent=65537, key_size=2048)
            if rsa_key else ec.generate_private_key(ec.SECP256R1())
        )
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
        is_ca = issuer is None
        b = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(issuer.cert.subject if issuer else name)
            .public_key(self.key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(not_before or NOW - timedelta(days=1))
            .not_valid_after(not_after or NOW + timedelta(days=365))
            .add_extension(x509.BasicConstraints(ca=is_ca, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(
                digital_signature=True, content_commitment=True,
                key_encipherment=False, data_encipherment=False,
                key_agreement=False, key_cert_sign=is_ca, crl_sign=False,
                encipher_only=False, decipher_only=False,
            ), critical=True)
        )
        if tsa:
            b = b.add_extension(
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.TIME_STAMPING]), critical=True,
            )
        self.cert = b.sign((issuer or self).key, hashes.SHA256())
        self.der = self.cert.public_bytes(serialization.Encoding.DER)
        self.pem = self.cert.public_bytes(serialization.Encoding.PEM)
        self.asn1_cert = asn1_x509.Certificate.load(self.der)
        self.asn1_key = asn1_keys.PrivateKeyInfo.load(self.key.private_bytes(
            serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ))


@pytest.fixture(scope="module")
def alice() -> Identity:
    return Identity("Alice Signer")


@pytest.fixture(scope="module")
def mallory() -> Identity:
    return Identity("Mallory")


# ─── PDF builders ─────────────────────────────────────────────────────────

def _blank_pdf() -> bytes:
    """One blank page, written by pyHanko itself.

    Not pypdf: signing a pypdf file makes pyHanko set Info /Producer to
    "pypdf; pyHanko x.y" but XMP pdf:Producer to "pyHanko x.y", which the
    (unrelated) Info↔XMP coherence check rightly flags. Producing the base
    file with pyHanko keeps these tests about signatures only.
    """
    w = PdfFileWriter()
    w.set_info(DictionaryObject({NameObject("/Title"): pdf_string("Invoice 42")}))
    w.insert_page(PageObject(
        contents=w.add_object(StreamObject(stream_data=b"")),
        media_box=ArrayObject([NumberObject(x) for x in (0, 0, 72, 72)]),
    ))
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def _sign(data: bytes, ident: Identity, *, field="Sig1", signing_key=None,
          timestamper=None, chain=()) -> bytes:
    """Append a signature (incremental update). `signing_key` lets a test
    sign with a key that does NOT match the embedded certificate; `chain`
    embeds intermediate/CA certs next to the signer's."""
    signer = signers.SimpleSigner(
        signing_cert=ident.asn1_cert,
        signing_key=signing_key or ident.asn1_key,
        cert_registry=SimpleCertificateStore.from_certs([c.asn1_cert for c in chain]),
    )
    out = signers.sign_pdf(
        IncrementalPdfFileWriter(io.BytesIO(data)),
        signers.PdfSignatureMetadata(field_name=field),
        signer=signer,
        timestamper=timestamper,
    )
    return out.getvalue()


def _write(tmp_path: Path, name: str, data: bytes) -> Path:
    p = tmp_path / name
    p.write_bytes(data)
    return p


def _codes(report) -> set:
    return {f.code for f in report.findings}


# ─── happy path ───────────────────────────────────────────────────────────

def test_signed_trusted_is_clean(tmp_path: Path, alice):
    pdf = _write(tmp_path, "signed.pdf", _sign(_blank_pdf(), alice))

    [sig] = verify_pdf_signatures(pdf, trust_roots=[alice.asn1_cert])
    assert isinstance(sig, SignatureCheck)
    assert sig.kind == "signature"
    assert sig.field_name == "Sig1"
    assert "Alice Signer" in sig.signer
    assert sig.intact and sig.valid and sig.trusted and sig.bottom_line
    assert not sig.revoked and sig.error is None
    assert sig.coverage == "ENTIRE_FILE"
    assert sig.modification_level == "NONE"
    assert sig.md_algorithm == "sha256"
    assert abs((sig.signing_time - NOW).total_seconds()) < 300

    report = check_pdf(pdf, trust_roots=[alice.asn1_cert])
    assert report.verdict == "clean", report.findings
    assert len(report.signatures) == 1
    print("  [PASS] signed + trusted root → clean")


def test_trust_root_input_forms(tmp_path: Path, alice):
    """Path (PEM), raw DER bytes, PEM bytes, cryptography cert, asn1crypto cert."""
    pdf = _write(tmp_path, "signed.pdf", _sign(_blank_pdf(), alice))
    pem_file = _write(tmp_path, "alice.pem", alice.pem)
    der_file = _write(tmp_path, "alice.der", alice.der)
    for root in (pem_file, str(der_file), alice.der, alice.pem, alice.cert, alice.asn1_cert):
        [sig] = verify_pdf_signatures(pdf, trust_roots=[root])
        assert sig.trusted, type(root)
    print("  [PASS] trust roots accepted as path / bytes / cert objects")


def test_bad_trust_root_raises(tmp_path: Path, alice):
    pdf = _write(tmp_path, "signed.pdf", _sign(_blank_pdf(), alice))
    with pytest.raises(TypeError):
        verify_pdf_signatures(pdf, trust_roots=[12345])
    with pytest.raises(OSError):
        check_pdf(pdf, trust_roots=[tmp_path / "missing.pem"])
    print("  [PASS] unusable trust root is a caller error, not a finding")


def test_unsigned_pdf(tmp_path: Path):
    pdf = _write(tmp_path, "plain.pdf", _blank_pdf())
    assert verify_pdf_signatures(pdf) == []
    report = check_pdf(pdf)
    assert report.signatures is None
    assert not any(c.startswith("signature.") for c in _codes(report))
    print("  [PASS] unsigned PDF → no signature results")


# ─── trust ────────────────────────────────────────────────────────────────

def test_self_signed_without_root_is_untrusted(tmp_path: Path, alice):
    pdf = _write(tmp_path, "signed.pdf", _sign(_blank_pdf(), alice))
    [sig] = verify_pdf_signatures(pdf)            # no roots → nothing trusted
    assert sig.intact and sig.valid
    assert not sig.trusted and not sig.bottom_line
    assert sig.trust_problem is not None

    report = check_pdf(pdf)
    assert report.verdict == "suspicious"
    assert "signature.untrusted" in _codes(report)
    print("  [PASS] self-signed, no root → untrusted warning")


def test_wrong_root_is_untrusted(tmp_path: Path, alice, mallory):
    pdf = _write(tmp_path, "signed.pdf", _sign(_blank_pdf(), alice))
    [sig] = verify_pdf_signatures(pdf, trust_roots=[mallory.asn1_cert])
    assert sig.intact and sig.valid and not sig.trusted
    print("  [PASS] trusting a different root does not trust the signer")


def test_impostor_cert_same_name(tmp_path: Path, alice):
    """A second self-signed cert with the SAME subject name is not Alice."""
    fake_alice = Identity("Alice Signer")
    pdf = _write(tmp_path, "fake.pdf", _sign(_blank_pdf(), fake_alice))
    [sig] = verify_pdf_signatures(pdf, trust_roots=[alice.asn1_cert])
    assert "Alice Signer" in sig.signer             # same display name...
    assert not sig.trusted                          # ...but not trusted
    assert sig.signer_cert_sha256 != hashlib.sha256(alice.der).hexdigest()
    print("  [PASS] same-name impostor certificate is not trusted")


def test_ca_issued_leaf(tmp_path: Path):
    """Real-world shape: trust the CA, not the signer's own certificate."""
    ca = Identity("Test Root CA")
    leaf = Identity("Bob Employee", issuer=ca)
    pdf = _write(tmp_path, "bob.pdf", _sign(_blank_pdf(), leaf, chain=[ca]))

    [sig] = verify_pdf_signatures(pdf, trust_roots=[ca.asn1_cert])
    assert sig.trusted and "Bob Employee" in sig.signer
    assert "Test Root CA" in sig.issuer

    [sig] = verify_pdf_signatures(pdf, trust_roots=[Identity("Other CA").asn1_cert])
    assert sig.intact and sig.valid and not sig.trusted
    print("  [PASS] leaf trusted via its CA, not via an unrelated CA")


def test_expired_cert_and_validation_time(tmp_path: Path):
    # Expiry is checked on the CHAIN below the anchor: per RFC 5280 a trust
    # anchor's own validity dates are not enforced, so a lone self-signed
    # cert can't exercise this — use CA → leaf.
    ca = Identity("Test Root CA", not_before=NOW - timedelta(days=800))
    old = Identity(
        "Old Signer", issuer=ca,
        not_before=NOW - timedelta(days=400),
        not_after=NOW - timedelta(days=30),
    )
    pdf = _write(tmp_path, "old.pdf", _sign(_blank_pdf(), old, chain=[ca]))

    [now_sig] = verify_pdf_signatures(pdf, trust_roots=[ca.asn1_cert])
    assert now_sig.intact and now_sig.valid and not now_sig.trusted

    [then_sig] = verify_pdf_signatures(
        pdf, trust_roots=[ca.asn1_cert],
        validation_time=NOW - timedelta(days=60),
    )
    assert then_sig.trusted
    print("  [PASS] expired cert untrusted now, trusted at validation_time")


# ─── tampering ────────────────────────────────────────────────────────────

def test_byte_flip_in_signed_range(tmp_path: Path, alice):
    data = _sign(_blank_pdf(), alice)
    i = data.index(b"Invoice 42")
    tampered = data[:i] + b"Invoice 99" + data[i + 10:]
    pdf = _write(tmp_path, "tampered.pdf", tampered)

    [sig] = verify_pdf_signatures(pdf, trust_roots=[alice.asn1_cert])
    assert not sig.intact and not sig.bottom_line

    report = check_pdf(pdf, trust_roots=[alice.asn1_cert])
    assert report.verdict == "tampered"
    assert "signature.digest_mismatch" in _codes(report)
    print("  [PASS] edited byte inside /ByteRange → digest mismatch, tampered")


def test_key_does_not_match_cert(tmp_path: Path, alice, mallory):
    """Signed with Mallory's key but claiming Alice's certificate."""
    data = _sign(_blank_pdf(), alice, signing_key=mallory.asn1_key)
    pdf = _write(tmp_path, "forged.pdf", data)

    [sig] = verify_pdf_signatures(pdf, trust_roots=[alice.asn1_cert])
    assert sig.intact            # the digest is honest...
    assert not sig.valid         # ...but the signature over it is not Alice's
    assert not sig.trusted

    report = check_pdf(pdf, trust_roots=[alice.asn1_cert])
    assert report.verdict == "tampered"
    assert "signature.invalid" in _codes(report)
    assert "signature.untrusted" not in _codes(report)
    print("  [PASS] signature by a key ≠ certificate key → invalid, tampered")


def test_incremental_edit_after_signing(tmp_path: Path, alice):
    """Signature bytes untouched, but a later revision changes the page."""
    signed = _sign(_blank_pdf(), alice)
    w = IncrementalPdfFileWriter(io.BytesIO(signed))
    page = w.root["/Pages"]["/Kids"][0].get_object()
    page[NameObject("/Rotate")] = NumberObject(90)
    w.update_container(page)
    out = io.BytesIO()
    w.write(out)
    pdf = _write(tmp_path, "edited.pdf", out.getvalue())

    [sig] = verify_pdf_signatures(pdf, trust_roots=[alice.asn1_cert])
    assert sig.intact and sig.valid      # the signed revision itself is fine
    assert sig.coverage == "ENTIRE_REVISION"
    assert sig.modification_level == "OTHER"
    assert sig.docmdp_ok is False
    assert not sig.bottom_line

    report = check_pdf(pdf, trust_roots=[alice.asn1_cert])
    assert report.verdict == "tampered"
    assert "signature.modified_after_signing" in _codes(report)
    print("  [PASS] page edited in a later revision → modified after signing")


def test_info_dict_edit_after_signing(tmp_path: Path, alice):
    """Changing the Info dict in a later revision is also not allowed."""
    signed = _sign(_blank_pdf(), alice)
    w = IncrementalPdfFileWriter(io.BytesIO(signed))
    info = w.trailer["/Info"].get_object()
    info[NameObject("/Title")] = pdf_string("Invoice 99")
    w.update_container(info)
    out = io.BytesIO()
    w.write(out)
    pdf = _write(tmp_path, "info-edit.pdf", out.getvalue())

    [sig] = verify_pdf_signatures(pdf, trust_roots=[alice.asn1_cert])
    assert sig.intact and sig.valid and sig.trusted
    assert sig.coverage == "ENTIRE_REVISION"
    # pyHanko's diff policy treats Info-dict edits as metadata-only updates
    # (same bucket as DSS/LTV additions), so the signature stays valid...
    assert sig.modification_level == "LTA_UPDATES" and sig.docmdp_ok

    # ...but the report still surfaces that the last revision is unsigned.
    report = check_pdf(pdf, trust_roots=[alice.asn1_cert])
    [f] = [f for f in report.findings if f.code == "signature.unsigned_updates"]
    assert f.severity == "info"
    assert f.evidence["modification_level"] == "LTA_UPDATES"
    print("  [PASS] Info edit after signing → unsigned_updates info")


def test_garbage_appended_after_signing(tmp_path: Path, alice):
    pdf = _write(tmp_path, "junk.pdf", _sign(_blank_pdf(), alice) + b"\nJUNKJUNKJUNK")
    report = check_pdf(pdf, trust_roots=[alice.asn1_cert])
    assert report.verdict == "tampered"
    assert "structure.junk_after_eof" in _codes(report)
    print("  [PASS] junk after signed %%EOF still flagged")


# ─── multiple signatures / timestamps ─────────────────────────────────────

def test_two_signatures_countersign(tmp_path: Path, alice, mallory):
    """A second signature after the first is a legitimate update."""
    data = _sign(_sign(_blank_pdf(), alice, field="Alice"), mallory, field="Mallory")
    pdf = _write(tmp_path, "two.pdf", data)
    roots = [alice.asn1_cert, mallory.asn1_cert]

    first, second = verify_pdf_signatures(pdf, trust_roots=roots)
    assert (first.field_name, second.field_name) == ("Alice", "Mallory")
    assert first.coverage == "ENTIRE_REVISION"
    assert second.coverage == "ENTIRE_FILE"
    assert first.bottom_line and second.bottom_line

    report = check_pdf(pdf, trust_roots=roots)
    assert report.verdict == "clean", report.findings
    assert "signature.unsigned_updates" not in _codes(report)  # last sig covers all

    only_alice = check_pdf(pdf, trust_roots=[alice.asn1_cert])
    assert only_alice.verdict == "suspicious"
    [f] = [f for f in only_alice.findings if f.code == "signature.untrusted"]
    assert f.evidence["field"] == "Mallory"
    print("  [PASS] countersigned PDF: both valid; per-signer trust reported")


def test_signature_timestamp_and_doc_timestamp(tmp_path: Path, alice):
    tsa = Identity("Test TSA", tsa=True, rsa_key=True)  # dummy TSA is RSA-only
    stamper = DummyTimeStamper(tsa_cert=tsa.asn1_cert, tsa_key=tsa.asn1_key)

    signed = _sign(_blank_pdf(), alice, timestamper=stamper)
    out = signers.PdfTimeStamper(stamper).timestamp_pdf(
        IncrementalPdfFileWriter(io.BytesIO(signed)), "sha256",
    )
    pdf = _write(tmp_path, "ts.pdf", out.getvalue())

    roots = [alice.asn1_cert, tsa.asn1_cert]
    sig, doc_ts = verify_pdf_signatures(pdf, trust_roots=roots)
    assert sig.kind == "signature" and sig.timestamp_trusted is True
    assert sig.bottom_line
    assert doc_ts.kind == "doc_timestamp"
    assert doc_ts.intact and doc_ts.valid and doc_ts.trusted
    assert "Test TSA" in doc_ts.signer
    report = check_pdf(pdf, trust_roots=roots)
    assert report.verdict == "clean", report.findings
    assert not any(c.startswith("signature.") for c in _codes(report))

    # Same file, TSA not trusted: timestamps flagged, signature still fine.
    sig2, doc_ts2 = verify_pdf_signatures(pdf, trust_roots=[alice.asn1_cert])
    assert sig2.timestamp_trusted is False
    assert not doc_ts2.trusted
    codes = _codes(check_pdf(pdf, trust_roots=[alice.asn1_cert]))
    assert {"signature.timestamp_untrusted", "signature.untrusted"} <= codes
    print("  [PASS] signature timestamp + document timestamp validated")


# ─── degrade / runtime ────────────────────────────────────────────────────

def test_verification_disabled(tmp_path: Path, alice):
    pdf = _write(tmp_path, "signed.pdf", _sign(_blank_pdf(), alice))
    report = check_pdf(pdf, verify_signatures=False)
    assert report.signatures is None
    assert report.verdict == "clean"
    assert "signature.not_verified" in _codes(report)
    print("  [PASS] verify_signatures=False → info finding only")


def test_pyhanko_missing_degrades(tmp_path: Path, alice, monkeypatch):
    import builtins
    pdf = _write(tmp_path, "signed.pdf", _sign(_blank_pdf(), alice))
    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "pyhanko" or name.startswith("pyhanko."):
            raise ImportError("no pyhanko")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    report = check_pdf(pdf)
    [f] = [f for f in report.findings if f.code == "signature.not_verified"]
    assert "pdf-sign" in f.message
    print("  [PASS] pyHanko missing → not_verified info, no crash")


@pytest.fixture
def signed_pdf(tmp_path: Path, alice) -> Path:
    # Built in a sync fixture: pyHanko's sync sign_pdf uses asyncio.run().
    return _write(tmp_path, "signed.pdf", _sign(_blank_pdf(), alice))


async def test_verify_inside_running_loop(signed_pdf: Path, alice):
    pdf = signed_pdf
    [sync_sig] = verify_pdf_signatures(pdf, trust_roots=[alice.asn1_cert])
    [async_sig] = await async_verify_pdf_signatures(pdf, trust_roots=[alice.asn1_cert])
    assert sync_sig.trusted and async_sig.trusted
    assert asyncio.get_running_loop() is not None
    print("  [PASS] sync API usable inside a running event loop")


# ─── CLI ──────────────────────────────────────────────────────────────────

def test_cli_check_signatures(tmp_path: Path, alice):
    good = _write(tmp_path, "good.pdf", _sign(_blank_pdf(), alice))
    data = good.read_bytes()
    i = data.index(b"Invoice 42")
    bad = _write(tmp_path, "bad.pdf", data[:i] + b"Invoice 99" + data[i + 10:])
    root = _write(tmp_path, "alice.pem", alice.pem)
    runner = CliRunner()

    r = runner.invoke(app, ["check", str(good), "--trust-root", str(root)])
    assert r.exit_code == 0, r.output
    assert "Alice Signer" in r.output

    r = runner.invoke(app, ["check", str(good)])
    assert r.exit_code == 1, r.output
    assert "signature.untrusted" in r.output

    r = runner.invoke(app, ["check", str(bad), "--trust-root", str(root)])
    assert r.exit_code == 2, r.output
    assert "signature.digest_mismatch" in r.output

    r = runner.invoke(app, ["check", str(good), "--no-verify-signatures"])
    assert r.exit_code == 0, r.output
    assert "signature.not_verified" in r.output

    r = runner.invoke(app, ["check", "--json", str(good), "--trust-root", str(root)])
    assert r.exit_code == 0, r.output
    [rep] = json.loads(r.output)
    [sig] = rep["signatures"]
    assert sig["trusted"] is True and sig["coverage"] == "ENTIRE_FILE"
    print("  [PASS] esuls-pdf check: --trust-root, exit codes, JSON")
