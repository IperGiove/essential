"""
Type0 / Identity-H text decoding (esuls.pdf.layout) and how undecodable text
is treated by `compare_pdfs`.

Browsers, Skia, Office and most PDF libraries write embedded TrueType as a
Type0 font with Identity-H encoding: 2-byte glyph codes, a ToUnicode CMap to
recover the text, and a /W array for the widths. These fonts are synthesised
here (the font program itself is a dummy — only the dictionaries matter).
"""
from pathlib import Path

from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
    TextStringObject,
)

from esuls.pdf import compare_pdfs, read_pdf_image_placements, read_pdf_text_lines, read_pdf_text_runs
from esuls.pdf.layout import parse_cid_widths, parse_tounicode

# CID → (unicode, width). CID 9 is a ligature glyph for "fi".
GLYPHS = {1: ("T", 570), 2: ("o", 560), 3: ("t", 330), 4: ("a", 540), 5: ("l", 240),
          6: ("e", 530), 7: (" ", 250), 8: ("1", 560), 9: ("fi", 590),
          10: ("0", 560), 11: (",", 200), 12: ("2", 560), 13: ("5", 560), 14: (".", 260)}

# Exercises every CMap form: bfchar (incl. a 2-char ligature destination),
# bfrange with an array destination, bfrange with an incrementing one.
CMAP = b"""/CIDInit /ProcSet findresource begin
12 dict begin begincmap
/CMapName /Adobe-Identity-UCS def /CMapType 2 def
1 begincodespacerange <0000> <FFFF> endcodespacerange
3 beginbfchar
<0001> <0054>
<0007> <0020>
<0009> <00660069>
endbfchar
2 beginbfrange
<0002> <0003> [<006F> <0074>]
<0004> <0004> <0061>
endbfrange
7 beginbfchar
<0005> <006C>
<0006> <0065>
<0008> <0031>
<000A> <0030>
<000B> <002C>
<000C> <0032>
<000D> <0035>
endbfchar
1 beginbfchar
<000E> <002E>
endbfchar
endcmap CMapName currentdict /CMap defineresource pop end end"""


def _stream(w, data: bytes, **entries):
    s = DecodedStreamObject()
    s.set_data(data)
    for k, v in entries.items():
        s[NameObject(f"/{k}")] = v
    return w._add_object(s)


def _type0(w, *, tounicode=True, widths=True):
    desc = w._add_object(DictionaryObject({
        NameObject("/Type"): NameObject("/FontDescriptor"),
        NameObject("/FontName"): NameObject("/AAAAAA+Roboto-Regular"),
        NameObject("/FontFile2"): _stream(w, b"\x00\x01\x00\x00dummy"),
    }))
    cid = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/CIDFontType2"),
        NameObject("/BaseFont"): NameObject("/AAAAAA+Roboto-Regular"),
        NameObject("/CIDSystemInfo"): DictionaryObject({
            NameObject("/Registry"): TextStringObject("Adobe"),
            NameObject("/Ordering"): TextStringObject("Identity"),
            NameObject("/Supplement"): NumberObject(0),
        }),
        NameObject("/FontDescriptor"): desc,
        NameObject("/DW"): NumberObject(1000),
    })
    if widths:
        # Both /W forms: "c [w1 w2 …]" for 1–7, "first last w" for 8–14.
        cid[NameObject("/W")] = ArrayObject([
            NumberObject(1), ArrayObject([NumberObject(GLYPHS[k][1]) for k in range(1, 8)]),
            NumberObject(8), NumberObject(8), NumberObject(560),
            NumberObject(9), ArrayObject([NumberObject(590)]),
            NumberObject(10), NumberObject(10), NumberObject(560),
            NumberObject(11), ArrayObject([NumberObject(200), NumberObject(560), NumberObject(560),
                                           NumberObject(260)]),
        ])
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type0"),
        NameObject("/BaseFont"): NameObject("/AAAAAA+Roboto-Regular"),
        NameObject("/Encoding"): NameObject("/Identity-H"),
        NameObject("/DescendantFonts"): ArrayObject([w._add_object(cid)]),
    })
    if tounicode:
        font[NameObject("/ToUnicode")] = _stream(w, CMAP)
    return w._add_object(font)


def _encode(text: str) -> str:
    """Text → hex string of CIDs (greedy: 'fi' uses the ligature glyph)."""
    rev = {u: cid for cid, (u, _) in GLYPHS.items()}
    out, i = [], 0
    while i < len(text):
        if text[i:i + 2] == "fi":
            out.append(9)
            i += 2
        else:
            out.append(rev[text[i]])
            i += 1
    return "<" + "".join(f"{c:04X}" for c in out) + ">"


def _width(text: str, size: float) -> float:
    rev = {u: cid for cid, (u, _) in GLYPHS.items()}
    total, i = 0, 0
    while i < len(text):
        if text[i:i + 2] == "fi":
            total += GLYPHS[9][1]
            i += 2
        else:
            total += GLYPHS[rev[text[i]]][1]
            i += 1
    return total / 1000 * size


def _pdf(path: Path, content: bytes, **font_kw) -> Path:
    w = PdfWriter()
    page = w.add_blank_page(width=300, height=200)
    img = _stream(w, b"\xff\xff\xff", Type=NameObject("/XObject"), Subtype=NameObject("/Image"),
                  Width=NumberObject(1), Height=NumberObject(1),
                  ColorSpace=NameObject("/DeviceRGB"), BitsPerComponent=NumberObject(8))
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F20"): _type0(w, **font_kw)}),
        NameObject("/XObject"): DictionaryObject({NameObject("/X4"): img}),
    })
    page[NameObject("/Contents")] = _stream(w, content)
    with path.open("wb") as fh:
        w.write(fh)
    return path


def _line(x, y, text, size=10):
    return f"BT /F20 {size} Tf {x} {y} Td {_encode(text)} Tj ET\n".encode()


# ─── parsers ──────────────────────────────────────────────────────────────

def test_parse_tounicode_forms():
    m = parse_tounicode(CMAP)
    assert m[b"\x00\x01"] == "T" and m[b"\x00\x09"] == "fi"          # bfchar, ligature
    assert m[b"\x00\x02"] == "o" and m[b"\x00\x03"] == "t"           # bfrange array form
    assert m[b"\x00\x04"] == "a"                                     # bfrange incrementing
    inc = parse_tounicode(b"beginbfrange <0010> <0012> <0041> endbfrange")
    assert [inc[bytes([0, c])] for c in (0x10, 0x11, 0x12)] == ["A", "B", "C"]
    print("  [PASS] ToUnicode bfchar / bfrange (array + incrementing) / ligatures")


def test_parse_cid_widths_forms():
    w = parse_cid_widths([1, [570, 560], 5, 7, 300])
    assert w == {1: 570.0, 2: 560.0, 5: 300.0, 6: 300.0, 7: 300.0}
    print("  [PASS] /W both forms")


# ─── decoding ─────────────────────────────────────────────────────────────

def test_type0_text_and_exact_width(tmp_path: Path):
    pdf = _pdf(tmp_path / "t.pdf", _line(50, 100, "Totale 1.250,00"))
    [r] = read_pdf_text_runs(pdf)
    assert r["text"] == "Totale 1.250,00"
    assert r["decoded"] and r["widths_known"]
    assert abs(r["x_end"] - (50 + _width("Totale 1.250,00", 10))) < 0.01
    assert r["font"] == "AAAAAA+Roboto-Regular"
    print("  [PASS] Identity-H text decoded, end position from /W exact")


def test_ligature_glyph_decodes_to_two_letters(tmp_path: Path):
    pdf = _pdf(tmp_path / "lig.pdf", _line(50, 100, "fi"))
    [r] = read_pdf_text_runs(pdf)
    assert r["text"] == "fi"
    print("  [PASS] one ligature glyph → 'fi'")


def test_kerned_tj_is_one_run(tmp_path: Path):
    content = (f"BT /F20 10 Tf 50 100 Td [{_encode('Tot')} -20 {_encode('ale')}] TJ ET\n").encode()
    pdf = _pdf(tmp_path / "kern.pdf", content)
    runs = read_pdf_text_runs(pdf)
    assert len(runs) == 2 and runs[0]["op"] == runs[1]["op"]
    [ln] = read_pdf_text_lines(pdf)
    assert ln["text"] == "Totale" and ln["runs"] == 1 and ln["pieces"] == 2
    print("  [PASS] kerned TJ pieces count as one run")


def test_unmapped_code_marked(tmp_path: Path):
    content = b"BT /F20 10 Tf 50 100 Td <00010063> Tj ET\n"      # 0x63 not in the CMap
    [r] = read_pdf_text_runs(_pdf(tmp_path / "u.pdf", content))
    assert r["text"] == "T�" and r["decoded"] is False
    print("  [PASS] unmapped glyph → U+FFFD, run not 'decoded'")


def test_missing_tounicode(tmp_path: Path):
    pdf = _pdf(tmp_path / "n.pdf", _line(50, 100, "Totale"), tounicode=False)
    [r] = read_pdf_text_runs(pdf)
    assert r["text"] == "[6 glyphs]" and r["decoded"] is False
    assert r["widths_known"] is True                    # /W still gives real widths
    assert abs(r["x_end"] - (50 + _width("Totale", 10))) < 0.01
    print("  [PASS] no ToUnicode: placeholder text, real widths")


def test_patch_over_type0_text_quotes_it(tmp_path: Path):
    content = _line(50, 100, "Totale 1.250,00") + b"q 90 0 0 14 48 97 cm /X4 Do Q\n"
    [p] = read_pdf_image_placements(_pdf(tmp_path / "p.pdf", content))
    assert p["covered_text"] == "Totale 1.250,00"
    print("  [PASS] image patch over Type0 text quotes the decoded text")


# ─── compare with undecodable text ────────────────────────────────────────

def _doc(path, total, **kw):
    return _pdf(path, _line(50, 150, "Totale") + _line(50, 100, total), **kw)


def test_compare_undecodable_lines_are_unverifiable(tmp_path: Path):
    a = _doc(tmp_path / "a.pdf", "1.250,00", tounicode=False)
    b = _doc(tmp_path / "b.pdf", "12.250,00", tounicode=False)
    pg = compare_pdfs(a, b)["pages"][0]
    assert pg["same"] == 0 and pg["changed"] == []
    assert len(pg["unverifiable"]) == 2
    counts = sorted((u["glyphs_a"], u["glyphs_b"]) for u in pg["unverifiable"])
    assert counts == [(6, 6), (8, 9)]                   # the total differs in length
    print("  [PASS] undecodable lines: unverifiable, with glyph counts")


def test_compare_decoded_type0(tmp_path: Path):
    a = _doc(tmp_path / "a.pdf", "1.250,00")
    b = _doc(tmp_path / "b.pdf", "12.250,00")
    r = compare_pdfs(a, b)
    pg = r["pages"][0]
    assert pg["same"] == 1 and pg["unverifiable"] == []
    [c] = pg["changed"]
    assert c["diff"] == [{"a": "1.250,00", "b": "12.250,00"}]
    assert r["summary"]["unverifiable_lines"] == 0
    print("  [PASS] decodable Type0 documents compare normally")
