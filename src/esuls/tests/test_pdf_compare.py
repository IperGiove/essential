"""
Page boxes, text lines and two-document comparison (esuls.pdf.layout /
esuls.pdf.compare).

A synthetic bank-statement template is rendered by `_statement()` with knobs
for every way two files can differ: values, a global offset, PDF version,
font subset naming, page boxes, logo resolution — and the edit patterns the
comparison must catch: a retyped amount that breaks a right-aligned column,
a field split into runs differently, a value painted at the end of the stream,
a field in another font/size. Helvetica (standard 14) is used so glyph widths
are known exactly and right alignment is real.
"""
import json
import zlib
from pathlib import Path

from click.testing import CliRunner
from pypdf import PdfWriter
from pypdf._font import Font
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
    StreamObject,
)

from esuls.pdf import (
    compare_pdfs,
    group_text_lines,
    pdf_fingerprint,
    read_pdf_page_boxes,
    read_pdf_text_lines,
    read_pdf_text_runs,
)
from esuls.pdf.cli import app

_HELV = DictionaryObject({
    NameObject("/Type"): NameObject("/Font"),
    NameObject("/Subtype"): NameObject("/Type1"),
    NameObject("/BaseFont"): NameObject("/Helvetica"),
    NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
})
_HELV_FONT = Font.from_font_resource(_HELV)


def _w(text: str, size: float) -> float:
    cw = _HELV_FONT.character_widths
    return sum(cw.get(c, cw["default"]) for c in text) / 1000.0 * size


GENUINE = {"name": "Mario Rossi", "date": "31/10/2026",
           "open": "1.000,00", "moves": "250,00", "close": "1.250,00"}
OTHER = {"name": "Giulia Bianchi", "date": "30/09/2026",
         "open": "12.400,00", "moves": "1.600,00", "close": "14.000,00"}

RIGHT = 400.0          # right edge of the amounts column


def _statement(path: Path, values: dict, *, dy=0.0, header=b"%PDF-1.4",
               font_name="Helvetica", trimbox=False, logo_px=(313, 69),
               close_mode="right", close_value=None,
               close_font=None, close_size=10.0, open_line_last=False) -> Path:
    """Render the template. `close_*` knobs alter only the 'Saldo finale'
    value: mode "right" (generator), "left_anchored" (retyped from the old
    left edge), "glyphs" (one run per glyph), "late" (painted after the
    footer)."""
    w = PdfWriter()
    w.pdf_header = header
    page = w.add_blank_page(width=595.28, height=841.89)
    if trimbox:
        page[NameObject("/TrimBox")] = ArrayObject([NumberObject(v) for v in (10, 10, 585, 831)])
        page[NameObject("/BleedBox")] = ArrayObject([NumberObject(v) for v in (0, 0, 595, 841)])

    font = DictionaryObject(_HELV)
    font[NameObject("/BaseFont")] = NameObject(f"/{font_name}")
    fonts = {NameObject("/F1"): w._add_object(font)}
    if close_font:
        alt = DictionaryObject(_HELV)
        alt[NameObject("/BaseFont")] = NameObject(f"/{close_font}")
        fonts[NameObject("/F2")] = w._add_object(alt)

    logo = StreamObject()
    logo._data = zlib.compress(b"\x10" * (logo_px[0] * logo_px[1] * 3))
    logo.update({
        NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Image"),
        NameObject("/Width"): NumberObject(logo_px[0]), NameObject("/Height"): NumberObject(logo_px[1]),
        NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
        NameObject("/BitsPerComponent"): NumberObject(8),
        NameObject("/Filter"): NameObject("/FlateDecode"),
        NameObject("/Length"): NumberObject(len(logo._data)),
    })
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject(fonts),
        NameObject("/XObject"): DictionaryObject({NameObject("/Im1"): w._add_object(logo)}),
    })

    def txt(x, y, s, font="/F1", size=10.0):
        return f"BT {font} {size:g} Tf {x:.2f} {y + dy:.2f} Td ({s}) Tj ET\n".encode("latin-1")

    def right(y, s):
        return txt(RIGHT - _w(s, 10), y, s)

    close = close_value or values["close"]
    c = b"q 95 0 0 21 40 %.2f cm /Im1 Do Q\n" % (740 + dy)
    c += txt(50, 780, "Estratto conto")
    c += txt(50, 700, "Intestatario:") + txt(150, 700, values["name"])
    c += txt(50, 680, "Data:") + txt(150, 680, values["date"])
    open_line = txt(50, 640, "Saldo iniziale") + right(640, values["open"])
    if not open_line_last:
        c += open_line
    c += txt(50, 620, "Movimenti") + right(620, values["moves"])
    c += txt(50, 600, "Saldo finale")
    late = b""
    if close_mode == "right":
        c += txt(RIGHT - _w(close, close_size), 600, close,
                 font="/F2" if close_font else "/F1", size=close_size)
    elif close_mode == "left_anchored":
        # Retyped value starting where the ORIGINAL value started.
        c += txt(RIGHT - _w(values["close"], 10), 600, close)
    elif close_mode == "glyphs":
        x = RIGHT - _w(close, 10)
        for ch in close:
            c += txt(x, 600, ch)
            x += _w(ch, 10)
    elif close_mode == "late":
        late = right(600, close)
    c += txt(50, 60, "Pagina 1 di 1")
    c += late
    if open_line_last:
        c += open_line
    s = DecodedStreamObject()
    s.set_data(c)
    page[NameObject("/Contents")] = w._add_object(s)
    with path.open("wb") as fh:
        w.write(fh)
    return path


def _page(result) -> dict:
    return result["pages"][0]


def _anoms(result, kind=None) -> list:
    return [a for a in _page(result)["anomalies"] if kind is None or a["type"] == kind]


# ─── boxes and lines ──────────────────────────────────────────────────────

def test_page_boxes_written_and_paper(tmp_path: Path):
    plain = _statement(tmp_path / "plain.pdf", GENUINE)
    printy = _statement(tmp_path / "print.pdf", GENUINE, trimbox=True)
    [b] = read_pdf_page_boxes(plain)
    assert b["paper"] == "A4" and b["width"] == 595.28
    assert b["mediabox"] == [0.0, 0.0, 595.28, 841.89]
    assert b["cropbox"] is None and b["trimbox"] is None      # no defaulting
    [b] = read_pdf_page_boxes(printy)
    assert b["trimbox"] == [10.0, 10.0, 585.0, 831.0] and b["bleedbox"] is not None
    print("  [PASS] page boxes as written, paper size recognised")


def test_text_lines_merge_runs(tmp_path: Path):
    pdf = _statement(tmp_path / "g.pdf", GENUINE, close_mode="glyphs")
    lines = {ln["text"]: ln for ln in read_pdf_text_lines(pdf)}
    assert "Saldo finale 1.250,00" in lines
    ln = lines["Saldo finale 1.250,00"]
    assert ln["runs"] == 1 + len("1.250,00")
    assert abs(ln["x1"] - RIGHT) < 0.05                       # widths are exact
    assert lines["Intestatario: Mario Rossi"]["runs"] == 2
    runs = read_pdf_text_runs(pdf)
    assert group_text_lines(runs) == read_pdf_text_lines(pdf)
    assert all(r["font"] == "Helvetica" and r["size"] == 10 for r in runs)
    print("  [PASS] runs merged into visual lines; right edge exact")


# ─── legitimate pairs ─────────────────────────────────────────────────────

def test_two_genuine_documents_same_template(tmp_path: Path):
    a = _statement(tmp_path / "a.pdf", OTHER)
    b = _statement(tmp_path / "b.pdf", GENUINE)
    r = compare_pdfs(a, b)
    assert r["fingerprint"]["differences"] == []
    assert r["summary"]["same_generator"] is True
    pg = _page(r)
    assert pg["offset"]["dx"] == 0 and pg["offset"]["dy"] == 0
    assert pg["same"] == 2          # title and footer; every other line has a value
    diffs = {d["a"]: d["b"] for c in pg["changed"] for d in c["diff"]}
    assert diffs["14.000,00"] == "1.250,00"
    assert diffs["Giulia Bianchi"] == "Mario Rossi"
    assert all(not c["flags"] for c in pg["changed"])
    assert pg["anomalies"] == [], pg["anomalies"]
    print("  [PASS] two genuine statements: values differ, no anomalies")


def test_offset_and_fingerprint_differences(tmp_path: Path):
    a = _statement(tmp_path / "a.pdf", GENUINE, dy=8.16, header=b"%PDF-1.3",
                   font_name="AAAAAA+Helvetica", logo_px=(188, 42))
    b = _statement(tmp_path / "b.pdf", GENUINE, font_name="DYVICM+Helvetica", trimbox=True)
    r = compare_pdfs(a, b)
    traits = {d["trait"]: d for d in r["fingerprint"]["differences"]}
    assert traits["pdf_version"]["a"] == "1.3" and traits["pdf_version"]["b"] == "1.4"
    assert traits["font_subset_scheme"]["a"] == "sequential"
    assert traits["font_subset_scheme"]["b"] == "random"
    assert traits["boxes_written"]["b"] == ["mediabox", "bleedbox", "trimbox"]
    assert r["summary"]["same_generator"] is False

    pg = _page(r)
    assert pg["offset"]["dy"] == -8.16 and pg["offset"]["dx"] == 0
    assert pg["same"] == pg["lines_a"] and not pg["changed"] and not pg["displaced"]
    [img] = pg["images"]
    assert img["pixels_a"] == [188, 42] and img["pixels_b"] == [313, 69]
    assert img["position_delta"] == [0.0, 0.0]
    assert pg["anomalies"] == []
    print("  [PASS] 8pt offset recovered; generator traits and logo resolution differ")


# ─── edit patterns ────────────────────────────────────────────────────────

def test_retyped_amount_breaks_right_alignment(tmp_path: Path):
    a = _statement(tmp_path / "a.pdf", GENUINE, close_mode="left_anchored",
                   close_value="91.250,00")
    b = _statement(tmp_path / "b.pdf", GENUINE)
    r = compare_pdfs(a, b)
    [brk] = _anoms(r, "alignment_break")
    assert brk["file"] == "a" and brk["edge"] == "right"
    assert brk["line"] == "Saldo finale 91.250,00"
    assert brk["gap_pt"] > 5
    [c] = _page(r)["changed"]
    assert c["diff"] == [{"a": "91.250,00", "b": "1.250,00"}]
    print("  [PASS] amount retyped from its old left edge → right alignment break")


def test_regenerated_amount_keeps_alignment(tmp_path: Path):
    """Same longer value, but right-aligned like the generator does it."""
    a = _statement(tmp_path / "a.pdf", GENUINE, close_value="91.250,00")
    b = _statement(tmp_path / "b.pdf", GENUINE)
    assert _anoms(compare_pdfs(a, b), "alignment_break") == []
    print("  [PASS] properly right-aligned different value → no break")


def test_field_split_differently(tmp_path: Path):
    a = _statement(tmp_path / "a.pdf", GENUINE, close_mode="glyphs")
    b = _statement(tmp_path / "b.pdf", GENUINE)
    [seg] = _anoms(compare_pdfs(a, b), "segmentation")
    assert seg["a"] == "Saldo finale 1.250,00"
    assert (seg["runs_a"], seg["runs_b"]) == (9, 2)
    print("  [PASS] one line split per glyph while others match → segmentation")


def test_value_painted_last(tmp_path: Path):
    a = _statement(tmp_path / "a.pdf", GENUINE, close_mode="late")
    b = _statement(tmp_path / "b.pdf", GENUINE)
    r = compare_pdfs(a, b)
    [sp] = _anoms(r, "split_painting")
    assert sp["file"] == "a" and sp["line"] == "Saldo finale 1.250,00"
    assert sp["interleaved_runs"] == 1        # the footer, painted in between
    assert _page(r)["changed"] == []          # same text, same place
    print("  [PASS] value painted after the footer → split painting")


def test_whole_line_painted_out_of_order(tmp_path: Path):
    a = _statement(tmp_path / "a.pdf", GENUINE, open_line_last=True)
    b = _statement(tmp_path / "b.pdf", GENUINE)
    [po] = _anoms(compare_pdfs(a, b), "paint_order")
    assert po["a"] == "Saldo iniziale 1.000,00" and po["out_of_order_with"] == 3
    print("  [PASS] whole line moved to the end of the stream → paint_order")


def test_equal_length_values_are_not_a_right_column(tmp_path: Path):
    """In B, name and date happen to have the same width (both end at the
    same x). In A they differ in length: still left-aligned, no break."""
    b_vals = {**GENUINE, "name": "31/12/2025"}         # same glyphs widths as the date
    a = _statement(tmp_path / "a.pdf", OTHER)
    b = _statement(tmp_path / "b.pdf", b_vals)
    lines = {ln["text"].split(":")[0]: ln for ln in read_pdf_text_lines(b)}
    assert abs(lines["Intestatario"]["x1"] - lines["Data"]["x1"]) < 0.01
    assert _anoms(compare_pdfs(a, b), "alignment_break") == []
    print("  [PASS] equal-length values sharing an edge are not a column")


def test_changed_field_in_other_font_and_size(tmp_path: Path):
    a = _statement(tmp_path / "a.pdf", GENUINE, close_value="9.999,00",
                   close_font="Arial", close_size=9.5)
    b = _statement(tmp_path / "b.pdf", GENUINE)
    r = compare_pdfs(a, b)
    kinds = {x["type"] for x in _anoms(r)}
    assert {"changed_field_font", "changed_field_size"} <= kinds
    [c] = _page(r)["changed"]
    assert set(c["flags"]) >= {"font", "size"}
    print("  [PASS] changed field in another font/size flagged")


def test_lines_only_on_one_side(tmp_path: Path):
    a = _statement(tmp_path / "a.pdf", GENUINE)
    b = _statement(tmp_path / "b.pdf", GENUINE, dy=0)
    # Drop the footer from B by rewriting it with an empty footer text.
    data = b.read_bytes().replace(b"(Pagina 1 di 1)", b"(             )")
    b.write_bytes(data)
    pg = _page(compare_pdfs(a, b))
    assert [ln["text"] for ln in pg["only_a"]] == ["Pagina 1 di 1"]
    print("  [PASS] line present only in A reported")


# ─── fingerprint / CLI ────────────────────────────────────────────────────

def test_fingerprint_has_no_content(tmp_path: Path):
    fp = pdf_fingerprint(_statement(tmp_path / "a.pdf", GENUINE))
    assert "Mario" not in json.dumps(fp)
    assert fp["paper"] == "A4" and fp["font_names"] == ["/F#"] and fp["image_names"] == ["/Im#"]
    assert fp["runs_per_line"] > 1
    print("  [PASS] fingerprint = producer traits only")


def test_cli_compare_and_inspect(tmp_path: Path):
    a = _statement(tmp_path / "a.pdf", GENUINE, close_mode="left_anchored",
                   close_value="91.250,00", dy=8, header=b"%PDF-1.3")
    b = _statement(tmp_path / "b.pdf", GENUINE)
    runner = CliRunner()

    res = runner.invoke(app, ["compare", str(a), str(b)])
    assert res.exit_code == 0, res.output
    assert "pdf_version: A='1.3'  B='1.4'" in res.output
    assert "dy=-8" in res.output
    assert "91.250,00 → 1.250,00" in res.output
    assert "no longer right-aligned" in res.output

    res = runner.invoke(app, ["compare", "--json", str(a), str(b)])
    assert json.loads(res.output)["summary"]["anomalies"] >= 1

    res = runner.invoke(app, ["inspect", "--full", str(b)])
    assert "Page boxes" in res.output and "(A4)" in res.output
    assert "'Saldo finale 1.250,00'" in res.output
    print("  [PASS] esuls-pdf compare / inspect --full")
