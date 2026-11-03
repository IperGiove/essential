"""Forensic inventory of page fonts and images, and how they change across
revisions.

Text added to an existing PDF needs glyphs, and an editor rarely reuses the
original embedded subset (it only holds the glyphs the author typed): it adds
a NEW font — often a second subset of the same face, or a non-embedded base
font. A pasted logo, signature scan or patched number is a new image XObject.
So the fonts and images a page uses are fingerprints of who wrote it.

Three surfaces:

- `read_pdf_fonts`  — every font reachable from page content, per page;
- `read_pdf_images` — every image XObject reachable from page content, with
  the image-editor signature found in JPEG metadata, if any;
- `read_pdf_revision_content` — for files with incremental updates, rebuild
  each earlier revision (a revision is the file truncated at its `%%EOF`) and
  report the fonts/images each later revision introduced or rewrote.

Only PAGE content is inventoried (page `/Resources`, recursing into Form
XObjects). Annotation appearances — signature widgets, form-field values,
comments — are deliberately excluded: those are legitimately added after
creation, and signatures are judged by `signatures.py` instead.
"""

import io
import mmap
import re
from pathlib import Path
from typing import Iterable, List, Optional, Union

from pypdf import PdfReader
from pypdf.generic import IndirectObject

_SUBSET_RE = re.compile(r"^([A-Z]{6})\+(.+)$")

# PDF "standard 14": every viewer must render these without embedding, so a
# non-embedded Helvetica is normal; a non-embedded "Calibri" is not.
STANDARD_14_FONTS = frozenset({
    "Courier", "Courier-Bold", "Courier-Oblique", "Courier-BoldOblique",
    "Helvetica", "Helvetica-Bold", "Helvetica-Oblique", "Helvetica-BoldOblique",
    "Times-Roman", "Times-Bold", "Times-Italic", "Times-BoldItalic",
    "Symbol", "ZapfDingbats",
})

# Lower-cased needles searched in JPEG APPn/COM segments (EXIF Software, XMP
# CreatorTool, ...). "Adobe Photoshop" and not "Photoshop": the APP13
# "Photoshop 3.0" marker is the generic IPTC container many tools write.
_IMAGE_EDITORS = (
    (b"adobe photoshop", "Adobe Photoshop"),
    (b"adobe imageready", "Adobe ImageReady"),
    (b"gimp", "GIMP"),
    (b"paint.net", "Paint.NET"),
    (b"pixelmator", "Pixelmator"),
    (b"affinity photo", "Affinity Photo"),
    (b"photopea", "Photopea"),
    (b"snapseed", "Snapseed"),
    (b"picsart", "PicsArt"),
)

_MAX_FORM_DEPTH = 12          # nested Form XObjects (cycle/DoS guard)
_JPEG_HEADER_SCAN = 512 * 1024


# ─── low-level helpers ─────────────────────────────────────────────────────

def _obj_id(ref) -> Optional[int]:
    return ref.idnum if isinstance(ref, IndirectObject) else None


def _resolve(obj):
    return obj.get_object() if hasattr(obj, "get_object") else obj


def _page_resources(page):
    """`/Resources` may be inherited from any ancestor `/Pages` node."""
    node, hops = page, 0
    while node is not None and hops < 64:
        res = node.get("/Resources")
        if res is not None:
            return _resolve(res)
        node = _resolve(node.get("/Parent")) if node.get("/Parent") is not None else None
        hops += 1
    return None


def _font_record(page_no: int, name: str, ref) -> dict:
    font = _resolve(ref)
    subtype = str(font.get("/Subtype", "")) or None
    base_font = str(font.get("/BaseFont", "")).lstrip("/") or None

    descriptor = font.get("/FontDescriptor")
    if descriptor is None and subtype == "/Type0":
        try:
            descendant = _resolve(_resolve(font["/DescendantFonts"])[0])
            descriptor = descendant.get("/FontDescriptor")
        except Exception:
            descriptor = None
    descriptor = _resolve(descriptor) if descriptor is not None else None

    font_file_len = None
    embedded = subtype == "/Type3"  # glyphs are content streams: always "embedded"
    if descriptor is not None:
        for key in ("/FontFile", "/FontFile2", "/FontFile3"):
            if key in descriptor:
                embedded = True
                try:
                    stream = _resolve(descriptor[key])
                    font_file_len = len(stream._data)
                except Exception:
                    pass
                break

    m = _SUBSET_RE.match(base_font or "")
    return {
        "page": page_no,
        "name": name,
        "obj_id": _obj_id(ref),
        "base_font": base_font,
        "family": m.group(2) if m else base_font,
        "subset_prefix": m.group(1) if m else None,
        "subtype": subtype.lstrip("/") if subtype else None,
        "embedded": embedded,
        "standard14": (base_font in STANDARD_14_FONTS) if base_font else False,
        "font_file_bytes": font_file_len,
    }


def _filters(img) -> List[str]:
    f = img.get("/Filter")
    if f is None:
        return []
    f = _resolve(f)
    items = f if isinstance(f, list) else [f]
    return [str(_resolve(x)).lstrip("/") for x in items]


def _jpeg_editor(data: bytes) -> Optional[str]:
    """Scan the metadata segments of a JPEG (everything before SOS) for the
    name of an image editor. Pixel data is never touched."""
    if not data.startswith(b"\xff\xd8"):
        return None
    pos, end = 2, min(len(data), _JPEG_HEADER_SCAN)
    meta = bytearray()
    while pos + 4 <= end:
        if data[pos] != 0xFF:
            break
        marker = data[pos + 1]
        if marker == 0xFF:                       # fill byte
            pos += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        if marker == 0xDA:                       # start of scan: metadata over
            break
        seg_len = int.from_bytes(data[pos + 2:pos + 4], "big")
        if seg_len < 2:
            break
        if 0xE0 <= marker <= 0xEF or marker == 0xFE:
            meta += data[pos + 4:pos + 2 + seg_len]
        pos += 2 + seg_len
    low = bytes(meta).lower()
    for needle, label in _IMAGE_EDITORS:
        if needle in low:
            return label
    return None


def _image_record(page_no: int, name: str, ref, img) -> dict:
    filters = _filters(img)
    raw = None
    try:
        raw = img._data
    except Exception:
        pass
    editor = None
    if raw and filters == ["DCTDecode"]:
        editor = _jpeg_editor(raw)
    cs = img.get("/ColorSpace")
    cs = _resolve(cs) if cs is not None else None
    if isinstance(cs, list) and cs:
        cs = _resolve(cs[0])
    return {
        "page": page_no,
        "name": name,
        "obj_id": _obj_id(ref),
        "width": int(img.get("/Width", 0) or 0),
        "height": int(img.get("/Height", 0) or 0),
        "bits_per_component": (int(img["/BitsPerComponent"])
                               if img.get("/BitsPerComponent") is not None else None),
        "color_space": str(cs).lstrip("/") if cs is not None else None,
        "filters": filters,
        "stream_bytes": len(raw) if raw is not None else None,
        "editor": editor,
    }


def _walk(resources, page_no: int, fonts: list, images: list,
          seen_fonts: set, seen_xobj: set, depth: int = 0) -> None:
    if resources is None or depth > _MAX_FORM_DEPTH:
        return
    font_dict = resources.get("/Font")
    if font_dict is not None:
        for name, ref in _resolve(font_dict).items():
            key = _obj_id(ref) or (page_no, depth, str(name))
            if key in seen_fonts:
                continue
            seen_fonts.add(key)
            try:
                fonts.append(_font_record(page_no, str(name), ref))
            except Exception:
                continue

    xobj_dict = resources.get("/XObject")
    if xobj_dict is None:
        return
    for name, ref in _resolve(xobj_dict).items():
        key = _obj_id(ref) or (page_no, depth, str(name))
        if key in seen_xobj:
            continue
        seen_xobj.add(key)
        try:
            xo = _resolve(ref)
            subtype = xo.get("/Subtype")
        except Exception:
            continue
        if subtype == "/Image":
            try:
                images.append(_image_record(page_no, str(name), ref, xo))
            except Exception:
                continue
        elif subtype == "/Form":
            res = xo.get("/Resources")
            _walk(_resolve(res) if res is not None else None, page_no,
                  fonts, images, seen_fonts, seen_xobj, depth + 1)


def _inventory(reader: PdfReader) -> tuple:
    fonts: list = []
    images: list = []
    for i, page in enumerate(reader.pages, start=1):
        # Dedup per page: the same font on two pages is listed on both.
        _walk(_page_resources(page), i, fonts, images, set(), set())
    return fonts, images


# ─── public API ───────────────────────────────────────────────────────────

def read_pdf_content(pdf_path: Union[str, Path]) -> dict:
    """`{"fonts": read_pdf_fonts(...), "images": read_pdf_images(...)}` in a
    single parse."""
    fonts, images = _inventory(PdfReader(str(pdf_path)))
    return {"fonts": fonts, "images": images}


def read_pdf_fonts(pdf_path: Union[str, Path]) -> List[dict]:
    """Fonts used by page content, one record per (page, font object)."""
    return _inventory(PdfReader(str(pdf_path)))[0]


def read_pdf_images(pdf_path: Union[str, Path]) -> List[dict]:
    """Image XObjects used by page content, one record per (page, image).

    `editor` names the image editor recorded in a JPEG's metadata (EXIF
    Software / XMP CreatorTool, ...), e.g. "Adobe Photoshop"; None otherwise.
    Inline images (`BI … EI` in the content stream) are not listed.
    """
    return _inventory(PdfReader(str(pdf_path)))[1]


def _revision_ends(pdf_path: Path, *, linearized: bool) -> List[int]:
    """Byte offset just past every `%%EOF`: each prefix is one revision."""
    ends: List[int] = []
    if pdf_path.stat().st_size == 0:
        return ends
    with pdf_path.open("rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        pos = 0
        while (found := mm.find(b"%%EOF", pos)) >= 0:
            pos = found + 5
            ends.append(pos)
    # A linearized file has an extra %%EOF after its first-page section: that
    # prefix is not a revision, just a partial view of the original.
    if linearized and len(ends) >= 2:
        ends = ends[1:]
    return ends


def _font_signature(f: dict) -> tuple:
    return (f["base_font"], f["subtype"], f["embedded"], f["font_file_bytes"])


def read_pdf_revision_content(
    pdf_path: Union[str, Path], *, max_revisions: int = 32,
) -> List[dict]:
    """
    Fonts and images introduced by each incremental update.

    Returns one entry per revision after the first (empty list for a file
    saved once). Each entry has `revision` (1 = first update), `end_offset`,
    and `added_fonts` / `added_images` / `modified_fonts` relative to the
    previous revision. "Added" means a font/image object that page content
    of this revision uses and the previous revision's page content did not;
    "modified" means the same font object was rewritten in place (different
    BaseFont / subset, or a different embedded font program).

    Objects are matched by object number, which incremental updates
    preserve. At most the last `max_revisions` updates are analysed (each
    revision is parsed separately; the cap bounds work on hostile files).
    A revision that cannot be parsed is reported with `error` and skipped as
    a baseline.
    """
    pdf_path = Path(pdf_path)
    with pdf_path.open("rb") as f:
        linearized = b"/Linearized" in f.read(1024)
    ends = _revision_ends(pdf_path, linearized=linearized)
    if len(ends) < 2:
        return []
    first_index = max(0, len(ends) - 1 - max_revisions)
    ends = ends[first_index:]

    out: List[dict] = []
    prev = None  # (fonts_by_id, image_ids)
    with pdf_path.open("rb") as fh:
        for idx, end in enumerate(ends):
            rev_no = first_index + idx
            try:
                fh.seek(0)
                reader = PdfReader(io.BytesIO(fh.read(end)), strict=False)
                fonts, images = _inventory(reader)
            except Exception as e:
                if idx > 0:  # the window's first entry is only a baseline
                    out.append({"revision": rev_no, "end_offset": end,
                                "error": f"{type(e).__name__}: {e}",
                                "added_fonts": [], "added_images": [],
                                "modified_fonts": []})
                prev = None
                continue

            fonts_by_id = {f["obj_id"]: f for f in fonts if f["obj_id"] is not None}
            image_ids = {i["obj_id"] for i in images if i["obj_id"] is not None}
            if prev is not None:
                p_fonts, p_images = prev
                added_fonts = _unique_by_id(
                    f for f in fonts
                    if f["obj_id"] is not None and f["obj_id"] not in p_fonts
                )
                modified = _unique_by_id(
                    {**f, "previous_base_font": p_fonts[f["obj_id"]]["base_font"]}
                    for f in fonts
                    if f["obj_id"] in p_fonts
                    and _font_signature(f) != _font_signature(p_fonts[f["obj_id"]])
                )
                added_images = _unique_by_id(
                    i for i in images
                    if i["obj_id"] is not None and i["obj_id"] not in p_images
                )
                out.append({
                    "revision": rev_no,
                    "end_offset": end,
                    "added_fonts": added_fonts,
                    "added_images": added_images,
                    "modified_fonts": modified,
                })
            elif idx > 0:
                out.append({"revision": rev_no, "end_offset": end,
                            "error": "previous revision unreadable: no baseline",
                            "added_fonts": [], "added_images": [],
                            "modified_fonts": []})
            prev = (fonts_by_id, image_ids)
    return out


def _unique_by_id(records: Iterable[dict]) -> List[dict]:
    """Collapse per-page duplicates of one object, keeping its page list."""
    by_id: dict = {}
    for r in records:
        hit = by_id.get(r["obj_id"])
        if hit is None:
            by_id[r["obj_id"]] = {**r, "pages": [r["page"]]}
        elif r["page"] not in hit["pages"]:
            hit["pages"].append(r["page"])
    return list(by_id.values())
