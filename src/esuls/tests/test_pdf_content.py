"""
Font / image forensics (esuls.pdf.content + the content.* findings).

PDFs are assembled object by object with pypdf: font dictionaries with
(dummy) embedded font programs, image XObjects with hand-built JPEG headers,
and incremental updates written with `PdfWriter(incremental=True)` — the way
a real editor appends an edit. No fixture files.
"""
import io
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
    StreamObject,
    TextStringObject,
)

from esuls.pdf import (
    check_pdf,
    read_pdf_content,
    read_pdf_fonts,
    read_pdf_images,
    read_pdf_revision_content,
)
from esuls.pdf.content import _jpeg_editor


# ─── object builders ──────────────────────────────────────────────────────

def _stream(w: PdfWriter, data: bytes, **entries) -> object:
    s = DecodedStreamObject()
    s.set_data(data)
    for k, v in entries.items():
        s[NameObject(f"/{k}")] = v
    return w._add_object(s)


def _raw_stream(w: PdfWriter, data: bytes, **entries):
    """A stream whose bytes are already encoded (e.g. a JPEG for DCTDecode)."""
    s = StreamObject()
    s._data = data
    for k, v in entries.items():
        s[NameObject(f"/{k}")] = v
    s[NameObject("/Length")] = NumberObject(len(data))
    return w._add_object(s)


def _font(w: PdfWriter, base: str, *, embedded=True, subtype="/TrueType",
          program=b"\x00\x01\x00\x00fake-font-program"):
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject(subtype),
        NameObject("/BaseFont"): NameObject(f"/{base}"),
    })
    if embedded:
        desc = DictionaryObject({
            NameObject("/Type"): NameObject("/FontDescriptor"),
            NameObject("/FontName"): NameObject(f"/{base}"),
            NameObject("/FontFile2"): _stream(w, program),
        })
        font[NameObject("/FontDescriptor")] = w._add_object(desc)
    return w._add_object(font)


def _jpeg(app_payload: bytes = b"") -> bytes:
    """SOI + optional APP1 + a token SOS/EOI: enough for a metadata scan."""
    seg = b""
    if app_payload:
        seg = b"\xff\xe1" + (len(app_payload) + 2).to_bytes(2, "big") + app_payload
    return b"\xff\xd8" + seg + b"\xff\xda\x00\x02" + b"\x00" * 16 + b"\xff\xd9"


def _image(w: PdfWriter, *, width=200, height=80, editor_note: bytes = b""):
    return _raw_stream(
        w, _jpeg(editor_note),
        Type=NameObject("/XObject"), Subtype=NameObject("/Image"),
        Width=NumberObject(width), Height=NumberObject(height),
        ColorSpace=NameObject("/DeviceRGB"), BitsPerComponent=NumberObject(8),
        Filter=NameObject("/DCTDecode"),
    )


def _set_resources(w: PdfWriter, page, fonts: dict, images: dict | None = None,
                   content: bytes = b"BT /F1 12 Tf 10 10 Td (Hi) Tj ET"):
    res = DictionaryObject()
    res[NameObject("/Font")] = DictionaryObject(
        {NameObject(f"/{k}"): v for k, v in fonts.items()})
    if images:
        res[NameObject("/XObject")] = DictionaryObject(
            {NameObject(f"/{k}"): v for k, v in images.items()})
    page[NameObject("/Resources")] = res
    page[NameObject("/Contents")] = _stream(w, content)


def _original(path: Path, *, fonts=None, images=None, pages=1) -> Path:
    """A 'clean' document: one embedded subset font per page."""
    w = PdfWriter()
    for _ in range(pages):
        page = w.add_blank_page(width=200, height=200)
        f = {"F1": _font(w, "AAAAAA+Calibri")}
        if fonts:
            f.update({k: build(w) for k, build in fonts.items()})
        imgs = {k: build(w) for k, build in (images or {}).items()}
        _set_resources(w, page, f, imgs)
    with path.open("wb") as fh:
        w.write(fh)
    return path


def _edit_incrementally(src: Path, dst: Path, mutate) -> Path:
    """Append an incremental update made by `mutate(writer, page0)`."""
    w = PdfWriter(str(src), incremental=True)
    page = w.pages[0]
    mutate(w, page)
    with dst.open("wb") as fh:
        w.write(fh)
    return dst


def _add_font_to_page(name: str, base: str, **kw):
    def mutate(w, page):
        res = page["/Resources"].get_object()
        res["/Font"].get_object()[NameObject(f"/{name}")] = _font(w, base, **kw)
        page[NameObject("/Contents")] = _stream(
            w, b"BT /F1 12 Tf 10 10 Td (Hi) Tj ET BT /" + name.encode() + b" 12 Tf 10 30 Td (999) Tj ET")
    return mutate


def _codes(report) -> set:
    return {f.code for f in report.findings}


# ─── inventory ────────────────────────────────────────────────────────────

def test_font_inventory(tmp_path: Path):
    pdf = _original(tmp_path / "a.pdf", fonts={
        "F2": lambda w: _font(w, "Helvetica", embedded=False, subtype="/Type1"),
    })
    fonts = {f["name"]: f for f in read_pdf_fonts(pdf)}
    assert fonts["/F1"]["family"] == "Calibri"
    assert fonts["/F1"]["subset_prefix"] == "AAAAAA"
    assert fonts["/F1"]["embedded"] and fonts["/F1"]["font_file_bytes"] > 0
    assert fonts["/F2"]["embedded"] is False and fonts["/F2"]["standard14"]
    assert fonts["/F1"]["page"] == 1
    print("  [PASS] fonts: family, subset prefix, embedding, standard-14")


def test_inherited_resources_and_form_xobjects(tmp_path: Path):
    """Fonts used inside a Form XObject, with /Resources on the /Pages node."""
    w = PdfWriter()
    page = w.add_blank_page(width=200, height=200)
    form_res = DictionaryObject({NameObject("/Font"): DictionaryObject(
        {NameObject("/FX"): _font(w, "BBBBBB+Garamond")})})
    form = _stream(w, b"BT /FX 9 Tf (x) Tj ET",
                   Type=NameObject("/XObject"), Subtype=NameObject("/Form"),
                   BBox=ArrayObject([NumberObject(0)] * 4), Resources=form_res)
    pages_node = w._root_object["/Pages"].get_object()
    pages_node[NameObject("/Resources")] = DictionaryObject({
        NameObject("/XObject"): DictionaryObject({NameObject("/Fm1"): form}),
    })
    if "/Resources" in page:
        del page["/Resources"]
    page[NameObject("/Contents")] = _stream(w, b"/Fm1 Do")
    buf = io.BytesIO()
    w.write(buf)
    pdf = tmp_path / "form.pdf"
    pdf.write_bytes(buf.getvalue())

    [f] = read_pdf_fonts(pdf)
    assert f["base_font"] == "BBBBBB+Garamond" and f["page"] == 1
    print("  [PASS] fonts found through inherited resources and Form XObjects")


def test_image_inventory_and_editor_detection(tmp_path: Path):
    pdf = _original(tmp_path / "img.pdf", images={
        "Im1": lambda w: _image(w, width=640, height=480),
        "Im2": lambda w: _image(
            w, width=300, height=120,
            editor_note=b"Exif\x00\x00...Software\x00Adobe Photoshop 25.1 (Windows)\x00"),
    })
    imgs = {i["name"]: i for i in read_pdf_images(pdf)}
    assert (imgs["/Im1"]["width"], imgs["/Im1"]["height"]) == (640, 480)
    assert imgs["/Im1"]["filters"] == ["DCTDecode"]
    assert imgs["/Im1"]["editor"] is None
    assert imgs["/Im2"]["editor"] == "Adobe Photoshop"
    assert read_pdf_content(pdf)["images"] == read_pdf_images(pdf)

    report = check_pdf(pdf)
    [f] = [f for f in report.findings if f.code == "content.image_editor_metadata"]
    assert f.severity == "warning" and f.evidence["editors"] == ["Adobe Photoshop"]
    assert report.verdict == "suspicious"
    print("  [PASS] images listed; Photoshop metadata in a JPEG flagged")


def test_jpeg_editor_scan_is_metadata_only():
    assert _jpeg_editor(_jpeg(b"http://ns.adobe.com/xap/1.0/\x00<xmp:CreatorTool>GIMP 2.10</xmp:CreatorTool>")) == "GIMP"
    # The generic IPTC container name is not evidence of editing.
    assert _jpeg_editor(_jpeg(b"Photoshop 3.0\x008BIM")) is None
    # Editor names inside the PIXEL data (after SOS) are ignored.
    assert _jpeg_editor(b"\xff\xd8\xff\xda\x00\x02Adobe Photoshop\xff\xd9") is None
    assert _jpeg_editor(b"not a jpeg") is None
    print("  [PASS] JPEG scan reads only APPn/COM segments")


def test_clean_original_has_no_content_findings(tmp_path: Path):
    pdf = _original(tmp_path / "clean.pdf", pages=2, images={"Im1": lambda w: _image(w)})
    report = check_pdf(pdf)
    assert not any(c.startswith("content.") for c in _codes(report)), report.findings
    assert len(report.content["fonts"]) == 2      # one per page
    assert report.content["revisions"] == []
    assert report.to_dict()["content"]["images"][0]["width"] == 200
    print("  [PASS] single-revision original: inventory only, no findings")


# ─── revisions ────────────────────────────────────────────────────────────

def test_font_added_in_incremental_update(tmp_path: Path):
    src = _original(tmp_path / "orig.pdf")
    pdf = _edit_incrementally(src, tmp_path / "edited.pdf",
                              _add_font_to_page("F9", "CCCCCC+Arial"))

    [rev] = read_pdf_revision_content(pdf)
    assert rev["revision"] == 1
    [added] = rev["added_fonts"]
    assert added["base_font"] == "CCCCCC+Arial" and added["pages"] == [1]
    assert rev["added_images"] == [] and rev["modified_fonts"] == []

    report = check_pdf(pdf)
    assert {"content.fonts_added_in_update", "structure.incremental_updates"} <= _codes(report)
    assert report.verdict == "suspicious"
    print("  [PASS] font added by a later revision → fonts_added_in_update")


def test_image_added_in_incremental_update(tmp_path: Path):
    src = _original(tmp_path / "orig.pdf")

    def paste_image(w, page):
        res = page["/Resources"].get_object()
        res[NameObject("/XObject")] = DictionaryObject(
            {NameObject("/Patch"): _image(w, width=90, height=20)})
        page[NameObject("/Contents")] = _stream(w, b"BT /F1 12 Tf (Hi) Tj ET q 90 0 0 20 50 50 cm /Patch Do Q")

    pdf = _edit_incrementally(src, tmp_path / "patched.pdf", paste_image)
    [rev] = read_pdf_revision_content(pdf)
    [img] = rev["added_images"]
    assert (img["width"], img["height"]) == (90, 20)
    assert "content.images_added_in_update" in _codes(check_pdf(pdf))
    print("  [PASS] image pasted in a later revision → images_added_in_update")


def test_font_rewritten_in_place(tmp_path: Path):
    """Same font object number, new subset: glyphs were added to the font."""
    src = _original(tmp_path / "orig.pdf")

    def resubset(w, page):
        font = page["/Resources"]["/Font"]["/F1"].get_object()
        font[NameObject("/BaseFont")] = NameObject("/DDDDDD+Calibri")

    pdf = _edit_incrementally(src, tmp_path / "resubset.pdf", resubset)
    [rev] = read_pdf_revision_content(pdf)
    [mod] = rev["modified_fonts"]
    assert (mod["previous_base_font"], mod["base_font"]) == ("AAAAAA+Calibri", "DDDDDD+Calibri")
    assert rev["added_fonts"] == []
    assert "content.fonts_modified_in_update" in _codes(check_pdf(pdf))
    print("  [PASS] font object rewritten in place → fonts_modified_in_update")


def test_metadata_only_update_adds_no_content(tmp_path: Path):
    src = _original(tmp_path / "orig.pdf")

    def retitle(w, page):
        w.add_metadata({"/Title": "renamed"})

    pdf = _edit_incrementally(src, tmp_path / "meta.pdf", retitle)
    [rev] = read_pdf_revision_content(pdf)
    assert not (rev["added_fonts"] or rev["added_images"] or rev["modified_fonts"])
    codes = _codes(check_pdf(pdf))
    assert "structure.incremental_updates" in codes
    assert not any(c.startswith("content.") for c in codes)
    print("  [PASS] metadata-only update: no content findings")


def test_annotation_fonts_are_not_page_content(tmp_path: Path):
    """A font used only by an annotation appearance (form field, stamp,
    signature widget) is a legitimate later addition, not a page edit."""
    src = _original(tmp_path / "orig.pdf")

    def annotate(w, page):
        ap = _stream(w, b"BT /Helv 10 Tf (note) Tj ET",
                     Type=NameObject("/XObject"), Subtype=NameObject("/Form"),
                     BBox=ArrayObject([NumberObject(0), NumberObject(0), NumberObject(50), NumberObject(20)]),
                     Resources=DictionaryObject({NameObject("/Font"): DictionaryObject(
                         {NameObject("/Helv"): _font(w, "EEEEEE+Arial")})}))
        annot = w._add_object(DictionaryObject({
            NameObject("/Type"): NameObject("/Annot"),
            NameObject("/Subtype"): NameObject("/FreeText"),
            NameObject("/Rect"): ArrayObject([NumberObject(0), NumberObject(0), NumberObject(50), NumberObject(20)]),
            NameObject("/Contents"): TextStringObject("note"),
            NameObject("/AP"): DictionaryObject({NameObject("/N"): ap}),
        }))
        page[NameObject("/Annots")] = ArrayObject([annot])

    pdf = _edit_incrementally(src, tmp_path / "annot.pdf", annotate)
    [rev] = read_pdf_revision_content(pdf)
    assert rev["added_fonts"] == []
    print("  [PASS] annotation-only fonts ignored")


def test_multiple_updates_attributed_to_their_revision(tmp_path: Path):
    src = _original(tmp_path / "orig.pdf")
    one = _edit_incrementally(src, tmp_path / "one.pdf",
                              _add_font_to_page("F8", "FFFFFF+Arial"))
    two = _edit_incrementally(one, tmp_path / "two.pdf",
                              _add_font_to_page("F9", "GGGGGG+Verdana"))
    r1, r2 = read_pdf_revision_content(two)
    assert [f["base_font"] for f in r1["added_fonts"]] == ["FFFFFF+Arial"]
    assert [f["base_font"] for f in r2["added_fonts"]] == ["GGGGGG+Verdana"]

    assert read_pdf_revision_content(two, max_revisions=1)[0]["revision"] == 2
    print("  [PASS] each update's additions attributed to its own revision")


def test_unparseable_earlier_revision(tmp_path: Path):
    src = _original(tmp_path / "orig.pdf")
    pdf = _edit_incrementally(src, tmp_path / "edited.pdf",
                              _add_font_to_page("F9", "CCCCCC+Arial"))
    # Fake a %%EOF early in the file: that prefix is not a PDF.
    data = pdf.read_bytes()
    nl = data.index(b"\n")
    junk = tmp_path / "junk.pdf"
    junk.write_bytes(data[:nl + 1] + b"%%EOF\n" + data[nl + 1:])
    revs = read_pdf_revision_content(junk)
    assert any(r.get("error") for r in revs)
    check_pdf(junk)  # must not raise
    print("  [PASS] unreadable revision reported, not raised")


# ─── single-revision heuristics ──────────────────────────────────────────

def test_duplicate_subset_same_page(tmp_path: Path):
    pdf = _original(tmp_path / "dup.pdf", fonts={
        "F2": lambda w: _font(w, "ZZZZZZ+Calibri"),
    })
    report = check_pdf(pdf)
    [f] = [f for f in report.findings if f.code == "content.duplicate_font_subset"]
    assert f.severity == "info"   # Chromium does this legitimately
    assert f.evidence["pages"] == [{"page": 1, "subset_prefixes": ["AAAAAA", "ZZZZZZ"]}]
    print("  [PASS] two subsets of one face on a page → duplicate_font_subset")


def test_same_face_different_font_type_is_normal(tmp_path: Path):
    """Word embeds Arial both as TrueType and as a Type0/CID font."""
    pdf = _original(tmp_path / "word.pdf", fonts={
        "F2": lambda w: _font(w, "ZZZZZZ+Calibri", subtype="/Type0", embedded=False),
    })
    assert "content.duplicate_font_subset" not in _codes(check_pdf(pdf))
    print("  [PASS] TrueType + Type0 of the same face not flagged")


def test_subsets_on_different_pages_are_normal(tmp_path: Path):
    """Many producers subset per page."""
    w = PdfWriter()
    for prefix in ("AAAAAA", "BBBBBB"):
        page = w.add_blank_page(width=200, height=200)
        _set_resources(w, page, {"F1": _font(w, f"{prefix}+Calibri")})
    pdf = tmp_path / "perpage.pdf"
    with pdf.open("wb") as fh:
        w.write(fh)
    assert "content.duplicate_font_subset" not in _codes(check_pdf(pdf))
    print("  [PASS] one subset per page not flagged")


def test_mixed_embedding(tmp_path: Path):
    pdf = _original(tmp_path / "mixed.pdf", fonts={
        "F2": lambda w: _font(w, "Helvetica", embedded=False, subtype="/Type1"),
        "F3": lambda w: _font(w, "ComicSansMS", embedded=False),
    })
    [f] = [f for f in check_pdf(pdf).findings if f.code == "content.mixed_font_embedding"]
    assert f.severity == "info" and f.evidence["fonts"] == ["ComicSansMS"]
    print("  [PASS] non-embedded non-standard font next to embedded ones → info")
