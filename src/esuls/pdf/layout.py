"""Where images land on the page, and which text they paint over.

The cheapest way to change a number on a PDF is not to edit the text — that
needs a font with the right glyphs — but to paint an image over it (a
screenshot of the new value, or a white box) and, optionally, new text on top.
The page then LOOKS edited correctly, while the original text is still in the
content stream, underneath.

PDF paints in content-stream order: what comes later covers what came
earlier. So the signal is precise: an image painted AFTER visible text whose
position falls inside the image's box. Images painted BEFORE the text
(backgrounds, letterheads, scans under an OCR layer) never count.

`read_pdf_image_placements` runs a small content-stream interpreter (graphics
state, CTM, text matrices, Form XObjects, inline images) and returns every
image placement with its box on the page, the earlier text it covers, and the
later text drawn over it.

Text positions are estimated: glyph widths come from the font when pypdf can
read them, else half an em per glyph. A text run counts as covered when its
CENTRE lies inside the image box; good enough for "this amount is under a
picture", not a layout engine.
"""

from pathlib import Path
from typing import List, Optional, Union

from pypdf import PdfReader
from pypdf.generic import ContentStream, IndirectObject

_IDENTITY = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
_MAX_FORM_DEPTH = 12
_MAX_OPERATIONS = 2_000_000      # per page, across nested forms (DoS guard)
_INVISIBLE_MODES = {3, 7}        # Tr 3 = invisible (OCR layers), 7 = clip only
_SNIPPET = 200


def _mult(m: list, n: list) -> list:
    """m × n for PDF row-vector matrices [a b c d e f]."""
    return [
        m[0] * n[0] + m[1] * n[2],
        m[0] * n[1] + m[1] * n[3],
        m[2] * n[0] + m[3] * n[2],
        m[2] * n[1] + m[3] * n[3],
        m[4] * n[0] + m[5] * n[2] + n[4],
        m[4] * n[1] + m[5] * n[3] + n[5],
    ]


def _apply(m: list, x: float, y: float) -> tuple:
    return (m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5])


def _nums(operands, n: int) -> Optional[list]:
    try:
        vals = [float(v) for v in operands[:n]]
    except (TypeError, ValueError):
        return None
    return vals if len(vals) == n else None


def _resolve(obj):
    return obj.get_object() if hasattr(obj, "get_object") else obj


class _FontInfo:
    """Decoding + advance widths for one font resource (best effort)."""

    def __init__(self, font_dict):
        self.font = None
        self.two_byte = False
        try:
            fd = _resolve(font_dict)
            self.two_byte = fd.get("/Subtype") == "/Type0"
            from pypdf._font import Font  # private: degrade gracefully if it moves
            self.font = Font.from_font_resource(fd)
        except Exception:
            self.font = None

    def decode(self, raw: bytes) -> tuple:
        """-> (text, glyph_count, width_in_em)"""
        n = max(1, len(raw) // 2) if self.two_byte else len(raw)
        f = self.font
        if f is None:
            return "", n, 0.5 * n
        try:
            enc = f.encoding
            if isinstance(enc, str):
                try:
                    t = raw.decode(enc, "surrogatepass")
                except Exception:
                    t = raw.decode("utf-16-be" if enc == "charmap" else "charmap",
                                   "surrogatepass")
            else:
                t = "".join(enc.get(b, chr(b)) for b in raw)
            widths = f.character_widths
            default = widths.get("default", 500) or 500
            w = sum(widths.get(c, default) for c in t) / 1000.0
            text = "".join(f.character_map.get(c, c) for c in t)
            return text, n, (w if w > 0 else 0.5 * n)
        except Exception:
            return "", n, 0.5 * n


def _raw_bytes(s) -> bytes:
    if isinstance(s, bytes):
        return bytes(s)
    ob = getattr(s, "original_bytes", None)
    if ob is not None:
        return bytes(ob)
    return str(s).encode("latin-1", "replace")


class _PageInterpreter:
    def __init__(self, reader: PdfReader, page_no: int):
        self.reader = reader
        self.page_no = page_no
        self.seq = 0
        self.ops = 0
        self.texts: list = []
        self.images: list = []
        self._fonts: dict = {}

    # ── entry points ──
    def run_page(self, page, resources) -> None:
        contents = page.get_contents()
        if contents is None:
            return
        self._run(ContentStream(contents, self.reader), resources, list(_IDENTITY), 0)

    # ── core ──
    def _font(self, resources, name):
        try:
            ref = _resolve(resources["/Font"])[name]
        except Exception:
            return _FontInfo(None)
        key = ref.idnum if isinstance(ref, IndirectObject) else id(ref)
        if key not in self._fonts:
            self._fonts[key] = _FontInfo(ref)
        return self._fonts[key]

    def _run(self, stream: ContentStream, resources, ctm: list, depth: int) -> None:
        gs_stack: list = []
        tr = 0                                  # render mode is graphics state
        tm = list(_IDENTITY)
        tlm = list(_IDENTITY)
        font = _FontInfo(None)
        fs, tc, tw, th, tl, ts = 0.0, 0.0, 0.0, 1.0, 0.0, 0.0

        def show(raw: bytes):
            nonlocal tm
            text, n, w_em = font.decode(raw)
            spaces = raw.count(b" ") if not font.two_byte else 0
            width = (w_em * fs + tc * n + tw * spaces) * th
            trm = _mult(tm, ctm)
            cx, cy = _apply(trm, width / 2.0, ts + fs * 0.35)
            if not text.strip() and raw.strip(b" \x00"):
                # Undecodable (no ToUnicode, unknown font): still text on the
                # page, so it still counts as covered — just unquotable.
                text = f"[{n} glyph{'s' if n != 1 else ''}]"
            if tr not in _INVISIBLE_MODES and text.strip():
                x0, y0 = _apply(trm, 0.0, ts)
                self.seq += 1
                self.texts.append({
                    "seq": self.seq, "text": text, "center": (cx, cy),
                    "origin": (x0, y0),
                    "size": abs(fs * (trm[3] if trm[3] else 1.0)),
                })
            tm = _mult([1, 0, 0, 1, width, 0], tm)

        for operands, op in stream.operations:
            self.ops += 1
            if self.ops > _MAX_OPERATIONS:
                return
            if op == b"q":
                gs_stack.append((list(ctm), tr))
            elif op == b"Q":
                if gs_stack:
                    ctm, tr = gs_stack.pop()
            elif op == b"cm":
                m = _nums(operands, 6)
                if m:
                    ctm = _mult(m, ctm)
            elif op == b"BT":
                tm, tlm = list(_IDENTITY), list(_IDENTITY)
            elif op == b"Tf" and len(operands) >= 2:
                font = self._font(resources, operands[0])
                fs = float(operands[1])
            elif op == b"Tc":
                tc = float(operands[0])
            elif op == b"Tw":
                tw = float(operands[0])
            elif op == b"Tz":
                th = float(operands[0]) / 100.0
            elif op == b"TL":
                tl = float(operands[0])
            elif op == b"Ts":
                ts = float(operands[0])
            elif op == b"Tr":
                tr = int(operands[0])
            elif op in (b"Td", b"TD"):
                v = _nums(operands, 2)
                if v:
                    if op == b"TD":
                        tl = -v[1]
                    tlm = _mult([1, 0, 0, 1, v[0], v[1]], tlm)
                    tm = list(tlm)
            elif op == b"Tm":
                m = _nums(operands, 6)
                if m:
                    tlm, tm = list(m), list(m)
            elif op == b"T*":
                tlm = _mult([1, 0, 0, 1, 0, -tl], tlm)
                tm = list(tlm)
            elif op == b"Tj" and operands:
                show(_raw_bytes(operands[0]))
            elif op in (b"'", b'"'):
                if op == b'"' and len(operands) >= 3:
                    tw, tc = float(operands[0]), float(operands[1])
                tlm = _mult([1, 0, 0, 1, 0, -tl], tlm)
                tm = list(tlm)
                if operands:
                    show(_raw_bytes(operands[-1]))
            elif op == b"TJ" and operands:
                for item in operands[0]:
                    if isinstance(item, (bytes, str)):
                        show(_raw_bytes(item))
                    else:
                        try:
                            adj = -float(item) / 1000.0 * fs * th
                        except (TypeError, ValueError):
                            continue
                        tm = _mult([1, 0, 0, 1, adj, 0], tm)
            elif op == b"Do" and operands:
                self._do(operands[0], resources, ctm, depth)
            elif op == b"INLINE IMAGE":
                settings = operands.get("settings", {}) if isinstance(operands, dict) else {}
                self._place_image("(inline)", None, settings, ctm)

    def _do(self, name, resources, ctm: list, depth: int) -> None:
        try:
            ref = _resolve(resources["/XObject"])[name]
            xo = _resolve(ref)
            subtype = xo.get("/Subtype")
        except Exception:
            return
        if subtype == "/Image":
            self._place_image(str(name), ref, xo, ctm)
        elif subtype == "/Form" and depth < _MAX_FORM_DEPTH:
            m = _nums(xo.get("/Matrix", _IDENTITY), 6) or list(_IDENTITY)
            res = xo.get("/Resources")
            res = _resolve(res) if res is not None else resources
            try:
                stream = ContentStream(xo, self.reader)
            except Exception:
                return
            self._run(stream, res, _mult(m, ctm), depth + 1)

    def _place_image(self, name: str, ref, img, ctm: list) -> None:
        corners = [_apply(ctm, x, y) for x, y in ((0, 0), (1, 0), (0, 1), (1, 1))]
        xs, ys = [c[0] for c in corners], [c[1] for c in corners]
        get = img.get if hasattr(img, "get") else (lambda k, d=None: d)
        has_alpha = (get("/SMask") is not None or get("/Mask") is not None
                     or get("/SM") is not None)
        self.seq += 1
        self.images.append({
            "seq": self.seq,
            "page": self.page_no,
            "name": name,
            "obj_id": ref.idnum if isinstance(ref, IndirectObject) else None,
            "bbox": [round(min(xs), 2), round(min(ys), 2),
                     round(max(xs), 2), round(max(ys), 2)],
            "transparent": bool(has_alpha),
        })


def _inside(pt: tuple, bbox: list, tol: float = 0.5) -> bool:
    return (bbox[0] - tol <= pt[0] <= bbox[2] + tol
            and bbox[1] - tol <= pt[1] <= bbox[3] + tol)


def _join(texts: list) -> str:
    # Reading order: top-to-bottom, then left-to-right.
    ordered = sorted(texts, key=lambda t: (-round(t["origin"][1]), t["origin"][0]))
    s = " ".join(t["text"].strip() for t in ordered)
    return s if len(s) <= _SNIPPET else s[:_SNIPPET] + "…"


def read_pdf_image_placements(pdf_path: Union[str, Path]) -> List[dict]:
    """
    Every image painted by page content, in paint order, with:

    - `bbox` — [x0, y0, x1, y1] on the page, in PDF points (axis-aligned box
      of the image's unit square under the CTM; Form XObjects composed);
    - `page_fraction` — share of the page's MediaBox the box covers;
    - `transparent` — the image has a soft mask / colour-key mask, so what is
      under it may still show (stamps, logos with alpha);
    - `covered_text` — visible text painted EARLIER whose centre is inside
      the box (what the image hides);
    - `text_on_top` — text painted LATER inside the box (what is shown
      instead, if anything).

    Inline images (`BI … EI`) are included with name "(inline)". Invisible
    text (render mode 3, e.g. OCR layers) and clip-only text are ignored.
    """
    reader = PdfReader(str(pdf_path))
    out: List[dict] = []
    from .content import _page_resources  # shared inheritance walk

    for page_no, page in enumerate(reader.pages, start=1):
        interp = _PageInterpreter(reader, page_no)
        try:
            interp.run_page(page, _page_resources(page) or {})
        except Exception:
            continue
        try:
            mb = [float(v) for v in page.mediabox]
            page_area = abs((mb[2] - mb[0]) * (mb[3] - mb[1])) or None
        except Exception:
            page_area = None
        for img in interp.images:
            bbox = img["bbox"]
            area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
            under = [t for t in interp.texts
                     if t["seq"] < img["seq"] and _inside(t["center"], bbox)]
            over = [t for t in interp.texts
                    if t["seq"] > img["seq"] and _inside(t["center"], bbox)]
            out.append({
                **{k: v for k, v in img.items() if k != "seq"},
                "paint_order": img["seq"],
                "page_fraction": round(area / page_area, 4) if page_area else None,
                "covered_text": _join(under) if under else None,
                "covered_runs": len(under),
                "text_on_top": _join(over) if over else None,
            })
    return out
