"""
Image placement and text-under-image detection (esuls.pdf.layout).

Synthetic one-page PDFs built with pypdf: a standard Helvetica font (widths
known without embedding), content streams written by hand so paint order and
coordinates are exact, and tiny Flate images. No fixture files.
"""
import zlib
from pathlib import Path

from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
    StreamObject,
)

from esuls.pdf import check_pdf, read_pdf_image_placements


# ─── builders ─────────────────────────────────────────────────────────────

def _stream(w, data: bytes, **entries):
    s = DecodedStreamObject()
    s.set_data(data)
    for k, v in entries.items():
        s[NameObject(f"/{k}")] = v
    return w._add_object(s)


def _helvetica(w):
    return w._add_object(DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
        NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
    }))


def _image(w, *, alpha=False):
    """A 2x2 white RGB image (Flate), optionally with a soft mask."""
    px = zlib.compress(b"\xff" * 12)
    s = StreamObject()
    s._data = px
    s.update({
        NameObject("/Type"): NameObject("/XObject"),
        NameObject("/Subtype"): NameObject("/Image"),
        NameObject("/Width"): NumberObject(2),
        NameObject("/Height"): NumberObject(2),
        NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
        NameObject("/BitsPerComponent"): NumberObject(8),
        NameObject("/Filter"): NameObject("/FlateDecode"),
        NameObject("/Length"): NumberObject(len(px)),
    })
    if alpha:
        m = StreamObject()
        m._data = zlib.compress(b"\x80" * 4)
        m.update({
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Image"),
            NameObject("/Width"): NumberObject(2),
            NameObject("/Height"): NumberObject(2),
            NameObject("/ColorSpace"): NameObject("/DeviceGray"),
            NameObject("/BitsPerComponent"): NumberObject(8),
            NameObject("/Filter"): NameObject("/FlateDecode"),
            NameObject("/Length"): NumberObject(len(m._data)),
        })
        s[NameObject("/SMask")] = w._add_object(m)
    return w._add_object(s)


def _pdf(path: Path, content: bytes, *, alpha=False, xobjects=None) -> Path:
    w = PdfWriter()
    page = w.add_blank_page(width=300, height=200)
    xo = {"Im1": _image(w, alpha=alpha)}
    if xobjects:
        xo.update({k: build(w) for k, build in xobjects.items()})
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): _helvetica(w)}),
        NameObject("/XObject"): DictionaryObject(
            {NameObject(f"/{k}"): v for k, v in xo.items()}),
    })
    page[NameObject("/Contents")] = _stream(w, content)
    with path.open("wb") as fh:
        w.write(fh)
    return path


ORIGINAL = b"BT /F1 10 Tf 1 0 0 1 50 100 Tm (Totale: 1.250,00 EUR) Tj ET\n"
PATCH = b"q 130 0 0 16 45 96 cm /Im1 Do Q\n"          # covers the line above
NEW_VALUE = b"BT /F1 10 Tf 50 100 Td (Totale: 9.999,00 EUR) Tj ET\n"


def _findings(pdf, code="content.text_covered_by_image"):
    return [f for f in check_pdf(pdf).findings if f.code == code]


# ─── geometry ─────────────────────────────────────────────────────────────

def test_placement_bbox_and_page_fraction(tmp_path: Path):
    pdf = _pdf(tmp_path / "a.pdf", b"q 150 0 0 100 10 20 cm /Im1 Do Q")
    [p] = read_pdf_image_placements(pdf)
    assert p["page"] == 1 and p["name"] == "/Im1"
    assert p["bbox"] == [10.0, 20.0, 160.0, 120.0]
    assert p["page_fraction"] == round(150 * 100 / (300 * 200), 4)
    assert p["transparent"] is False and p["covered_text"] is None
    print("  [PASS] image box from the CTM, as a fraction of the page")


def test_nested_cm_and_form_xobject_matrix(tmp_path: Path):
    """Image inside a Form XObject with its own /Matrix, under a scaled CTM."""
    def form(w):
        return _stream(
            w, b"q 10 0 0 10 0 0 cm /Im1 Do Q",
            Type=NameObject("/XObject"), Subtype=NameObject("/Form"),
            BBox=ArrayObject([NumberObject(v) for v in (0, 0, 10, 10)]),
            Matrix=ArrayObject([NumberObject(v) for v in (1, 0, 0, 1, 5, 5)]),
            Resources=DictionaryObject({NameObject("/XObject"): DictionaryObject(
                {NameObject("/Im1"): _image(w)})}),
        )
    pdf = _pdf(tmp_path / "form.pdf", b"q 2 0 0 2 100 50 cm /Fm1 Do Q", xobjects={"Fm1": form})
    [p] = read_pdf_image_placements(pdf)
    # unit square → ×10 → +5 (form matrix) → ×2 +100/+50 (page CTM)
    assert p["bbox"] == [110.0, 60.0, 130.0, 80.0]
    print("  [PASS] Form XObject /Matrix composed with the outer CTM")


# ─── the patch ────────────────────────────────────────────────────────────

def test_image_patch_over_amount(tmp_path: Path):
    pdf = _pdf(tmp_path / "patch.pdf", ORIGINAL + PATCH + NEW_VALUE)
    [p] = read_pdf_image_placements(pdf)
    assert p["covered_text"] == "Totale: 1.250,00 EUR"
    assert p["text_on_top"] == "Totale: 9.999,00 EUR"

    [f] = _findings(pdf)
    assert f.severity == "warning"
    assert "1.250,00" in f.message and "9.999,00" in f.message
    assert f.evidence["bbox"] == [45.0, 96.0, 175.0, 112.0]
    assert check_pdf(pdf).verdict == "suspicious"
    print("  [PASS] value hidden under an image and retyped → warning quoting both")


def test_patch_with_white_box_only(tmp_path: Path):
    """No new text: the image just erases the value."""
    pdf = _pdf(tmp_path / "erase.pdf", ORIGINAL + PATCH)
    [f] = _findings(pdf)
    assert f.evidence["text_on_top"] is None
    assert "1.250,00" in f.message
    print("  [PASS] value erased by an image → warning")


def test_partial_line_cover_picks_only_covered_runs(tmp_path: Path):
    content = (
        b"BT /F1 10 Tf 50 150 Td (Intestatario: Mario Rossi) Tj ET\n"
        b"BT /F1 10 Tf 50 100 Td (Importo:) Tj 60 0 Td (1.250,00) Tj ET\n"
        b"q 50 0 0 14 108 97 cm /Im1 Do Q\n"
    )
    pdf = _pdf(tmp_path / "partial.pdf", content)
    [p] = read_pdf_image_placements(pdf)
    assert p["covered_text"] == "1.250,00"          # not the label, not the name
    print("  [PASS] only the runs under the box are reported")


def test_tj_kerning_positions(tmp_path: Path):
    """TJ adjustments move the pen: the second run lands under the image."""
    content = (
        b"BT /F1 10 Tf 50 100 Td [(AB) -10000 (CD)] TJ ET\n"   # -10000 → +100pt
        b"q 30 0 0 14 155 97 cm /Im1 Do Q\n"
    )
    pdf = _pdf(tmp_path / "tj.pdf", content)
    [p] = read_pdf_image_placements(pdf)
    assert p["covered_text"] == "CD"
    print("  [PASS] TJ kerning advances the text position")


def test_undecodable_text_still_counts(tmp_path: Path):
    """Font missing from /Resources: glyphs can't be decoded, but they are on
    the page — covering them is still a patch."""
    content = b"BT /F9 10 Tf 50 100 Td (\x01\x02\x03) Tj ET\n" + PATCH
    pdf = _pdf(tmp_path / "nofont.pdf", content)
    [p] = read_pdf_image_placements(pdf)
    assert p["covered_text"] == "[3 glyphs]"
    assert _findings(pdf)
    print("  [PASS] undecodable text under an image still reported")


def test_inline_image_patch(tmp_path: Path):
    inline = b"q 130 0 0 16 45 96 cm BI /W 1 /H 1 /CS /G /BPC 8 ID \xff EI Q\n"
    pdf = _pdf(tmp_path / "inline.pdf", ORIGINAL + inline)
    [p] = [p for p in read_pdf_image_placements(pdf) if p["name"] == "(inline)"]
    assert p["covered_text"] == "Totale: 1.250,00 EUR"
    assert _findings(pdf)
    print("  [PASS] inline image (BI…EI) patch detected")


# ─── legitimate layouts ───────────────────────────────────────────────────

def test_background_image_under_text_is_fine(tmp_path: Path):
    """Letterheads and backgrounds are painted FIRST, text on top."""
    pdf = _pdf(tmp_path / "bg.pdf", b"q 300 0 0 200 0 0 cm /Im1 Do Q\n" + ORIGINAL)
    [p] = read_pdf_image_placements(pdf)
    assert p["covered_text"] is None
    assert p["text_on_top"] == "Totale: 1.250,00 EUR"
    assert not _findings(pdf)
    print("  [PASS] background painted before the text → no finding")


def test_ocr_invisible_text_under_scan_is_fine(tmp_path: Path):
    """OCR layers: invisible text (Tr 3), the scan painted over it."""
    content = (b"BT 3 Tr /F1 10 Tf 50 100 Td (Totale: 1.250,00 EUR) Tj ET\n"
               b"q 300 0 0 200 0 0 cm /Im1 Do Q\n")
    pdf = _pdf(tmp_path / "ocr.pdf", content)
    assert read_pdf_image_placements(pdf)[0]["covered_text"] is None
    assert not _findings(pdf)
    print("  [PASS] invisible OCR text under a scan → no finding")


def test_image_elsewhere_on_page_is_fine(tmp_path: Path):
    pdf = _pdf(tmp_path / "logo.pdf", ORIGINAL + b"q 60 0 0 30 220 160 cm /Im1 Do Q\n")
    assert not _findings(pdf)
    print("  [PASS] image painted later but not over text → no finding")


def test_transparent_stamp_over_text_is_info(tmp_path: Path):
    pdf = _pdf(tmp_path / "stamp.pdf", ORIGINAL + PATCH, alpha=True)
    [f] = _findings(pdf)
    assert f.severity == "info" and f.evidence["transparent"] is True
    assert check_pdf(pdf).verdict == "clean"
    print("  [PASS] stamp with alpha over text → info only")


def test_q_Q_restores_ctm(tmp_path: Path):
    """A cm inside q…Q must not leak into the next image."""
    content = b"q 1 0 0 1 200 0 cm Q\n" + ORIGINAL + PATCH
    pdf = _pdf(tmp_path / "qQ.pdf", content)
    [p] = read_pdf_image_placements(pdf)
    assert p["bbox"][0] == 45.0 and p["covered_text"]
    print("  [PASS] graphics state stack restores the CTM")


def test_report_carries_placements(tmp_path: Path):
    pdf = _pdf(tmp_path / "patch.pdf", ORIGINAL + PATCH + NEW_VALUE)
    d = check_pdf(pdf).to_dict()
    [p] = d["content"]["placements"]
    assert p["covered_text"] == "Totale: 1.250,00 EUR"
    print("  [PASS] placements serialised in the JSON report")
