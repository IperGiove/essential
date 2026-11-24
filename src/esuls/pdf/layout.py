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

import math
import re
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


_HEX = re.compile(rb"<([0-9A-Fa-f\s]*)>")


def _hex(tok: bytes) -> bytes:
    h = re.sub(rb"\s", b"", tok)
    if len(h) % 2:
        h += b"0"
    return bytes.fromhex(h.decode("ascii"))


def _utf16(b: bytes) -> str:
    try:
        return b.decode("utf-16-be", "surrogatepass")
    except Exception:
        return ""


def parse_tounicode(data: bytes) -> dict:
    """Minimal ToUnicode CMap reader: `bfchar` and `bfrange` (both the
    incrementing form and the array form) → {code bytes: unicode str}.
    Destinations may be several characters (ligatures: one glyph → "fi")."""
    out: dict = {}
    for block in re.findall(rb"beginbfchar(.*?)endbfchar", data, re.S):
        toks = _HEX.findall(block)
        for src, dst in zip(toks[0::2], toks[1::2]):
            out[_hex(src)] = _utf16(_hex(dst))
    for block in re.findall(rb"beginbfrange(.*?)endbfrange", data, re.S):
        for m in re.finditer(rb"<([0-9A-Fa-f\s]+)>\s*<([0-9A-Fa-f\s]+)>\s*(\[[^\]]*\]|<[0-9A-Fa-f\s]*>)", block):
            lo, hi = _hex(m.group(1)), _hex(m.group(2))
            width = len(lo)
            a, b = int.from_bytes(lo, "big"), int.from_bytes(hi, "big")
            if b < a or b - a > 0xFFFF:
                continue
            dst = m.group(3)
            if dst.startswith(b"["):
                for k, tok in enumerate(_HEX.findall(dst)):
                    if a + k > b:
                        break
                    out[(a + k).to_bytes(width, "big")] = _utf16(_hex(tok))
            else:
                base = _hex(dst[1:-1])
                if not base:
                    continue
                # Increment the LAST code unit of the destination.
                head, last = base[:-2], int.from_bytes(base[-2:], "big")
                for k in range(b - a + 1):
                    out[(a + k).to_bytes(width, "big")] = _utf16(head + ((last + k) & 0xFFFF).to_bytes(2, "big"))
    return out


def parse_cid_widths(w_array) -> dict:
    """CIDFont `/W`: `c [w1 w2 …]` and `c_first c_last w` → {cid: width}."""
    out: dict = {}
    items = list(_resolve(w_array) or [])
    k = 0
    while k < len(items):
        try:
            first = int(_resolve(items[k]))
            nxt = _resolve(items[k + 1])
            if isinstance(nxt, list):
                for off, wv in enumerate(nxt):
                    out[first + off] = float(_resolve(wv))
                k += 2
            else:
                last, wv = int(nxt), float(_resolve(items[k + 2]))
                if 0 <= last - first <= 0xFFFF:
                    for cid in range(first, last + 1):
                        out[cid] = wv
                k += 3
        except Exception:
            break
    return out


class _FontInfo:
    """Decoding + advance widths for one font resource (best effort).

    Type0 fonts with an Identity encoding (what browsers, Skia, Office and
    most libraries emit for embedded TrueType) are read directly: 2-byte
    codes, the ToUnicode CMap, and the descendant's `/W` + `/DW` widths.
    Everything else goes through pypdf's font tables. `decode` reports
    whether the text and the widths are real or placeholders.
    """

    def __init__(self, font_dict):
        self.font = None             # pypdf Font, for non-Identity fonts
        self.two_byte = False
        self.base_font = None
        self.to_unicode: Optional[dict] = None
        self.cid_widths: Optional[dict] = None
        self.dw = 1000.0
        self.error: Optional[str] = None
        if font_dict is None:
            self.error = "font resource not found"
            return
        try:
            fd = _resolve(font_dict)
            self.base_font = str(fd.get("/BaseFont", "")).lstrip("/") or None
            self.two_byte = fd.get("/Subtype") == "/Type0"
            enc = fd.get("/Encoding")
            identity = self.two_byte and str(_resolve(enc) if enc is not None else "") in (
                "/Identity-H", "/Identity-V")
            if identity:
                self._init_identity(fd)
                return
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
        try:
            from pypdf._font import Font  # private: degrade gracefully if it moves
            self.font = Font.from_font_resource(_resolve(font_dict))
        except Exception as e:
            self.error = self.error or f"{type(e).__name__}: {e}"

    def _init_identity(self, fd) -> None:
        tu = fd.get("/ToUnicode")
        if tu is not None:
            try:
                self.to_unicode = parse_tounicode(_resolve(tu).get_data())
            except Exception as e:
                self.error = f"ToUnicode unreadable: {type(e).__name__}: {e}"
        else:
            self.error = "no /ToUnicode"
        try:
            desc = _resolve(_resolve(fd["/DescendantFonts"])[0])
            # Per spec a missing /W means every glyph is /DW wide, and a
            # missing /DW means 1000: either way the widths are defined.
            self.cid_widths = parse_cid_widths(desc["/W"]) if desc.get("/W") is not None else {}
            if desc.get("/DW") is not None:
                self.dw = float(desc["/DW"])
        except Exception as e:
            self.error = self.error or f"widths unreadable: {type(e).__name__}: {e}"

    def decode(self, raw: bytes) -> tuple:
        """-> (text, glyph_count, width_in_em, text_decoded, widths_known)"""
        if self.to_unicode is not None or self.cid_widths is not None:
            codes = [raw[i:i + 2] for i in range(0, len(raw) - 1, 2)]
            n = max(1, len(codes))
            text, complete = "", False
            if self.to_unicode is not None:
                parts = [self.to_unicode.get(c) for c in codes]
                complete = all(p is not None for p in parts)
                # Unmapped codes stay visible as U+FFFD instead of vanishing.
                text = "".join("\ufffd" if p is None else p for p in parts)
            if self.cid_widths is not None:
                w = sum(self.cid_widths.get(int.from_bytes(c, "big"), self.dw) for c in codes) / 1000.0
                known = True
            else:
                w, known = 0.5 * n, False
            return text, n, w, complete and bool(text.strip()), known

        n = max(1, len(raw) // 2) if self.two_byte else len(raw)
        f = self.font
        if f is None:
            return "", n, 0.5 * n, False, False
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
            mapped = [f.character_map.get(c, c) for c in t]
            # pypdf keys widths by the MAPPED character, not the code.
            w = sum(widths.get(u, widths.get(c, default)) for c, u in zip(t, mapped)) / 1000.0
            text = "".join(mapped)
            return text, n, (w if w > 0 else 0.5 * n), bool(text.strip()), w > 0
        except Exception:
            return "", n, 0.5 * n, False, False


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
        self.text_op = 0       # id of the current text-showing operator
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
            text, n, w_em, decoded, known = font.decode(raw)
            spaces = raw.count(b" ") if not font.two_byte else 0
            width = (w_em * fs + tc * n + tw * spaces) * th
            trm = _mult(tm, ctm)
            cx, cy = _apply(trm, width / 2.0, ts + fs * 0.35)
            if not text.strip() and raw.strip(b" \x00"):
                # Undecodable (no ToUnicode, unknown font): still text on the
                # page, so it still counts as covered — just unquotable.
                text = f"[{n} glyph{'s' if n != 1 else ''}]"
            if text.strip():
                x0, y0 = _apply(trm, 0.0, ts)
                x1, _ = _apply(trm, width, ts)
                self.seq += 1
                self.texts.append({
                    "seq": self.seq, "text": text, "center": (cx, cy),
                    "origin": (x0, y0), "end_x": x1,
                    # rendered size: font size × vertical scale of Tm·CTM
                    "size": abs(fs) * math.hypot(trm[2], trm[3]),
                    "font": font.base_font,
                    "visible": tr not in _INVISIBLE_MODES,
                    "decoded": decoded,
                    "widths_known": known,
                    "op": self.text_op,
                })
            tm = _mult([1, 0, 0, 1, width, 0], tm)

        for operands, op in stream.operations:
            self.ops += 1
            if op in (b"Tj", b"TJ", b"'", b'"'):
                self.text_op += 1
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


def _interpret(reader: PdfReader):
    """Yield (page_no, page, interpreter) for every page that could be run."""
    from .content import _page_resources  # shared inheritance walk

    for page_no, page in enumerate(reader.pages, start=1):
        interp = _PageInterpreter(reader, page_no)
        try:
            interp.run_page(page, _page_resources(page) or {})
        except Exception:
            continue
        yield page_no, page, interp


# ─── page boxes ───────────────────────────────────────────────────────────

_BOXES = ("/MediaBox", "/CropBox", "/BleedBox", "/TrimBox", "/ArtBox")
_INHERITABLE = {"/MediaBox", "/CropBox"}

# (name, short side, long side) in points; ±2pt tolerance.
_PAPER = (
    ("A3", 841.89, 1190.55), ("A4", 595.28, 841.89), ("A5", 419.53, 595.28),
    ("Letter", 612.0, 792.0), ("Legal", 612.0, 1008.0), ("Tabloid", 792.0, 1224.0),
)


def _paper_name(w: float, h: float) -> Optional[str]:
    short, long_ = sorted((abs(w), abs(h)))
    for name, s_, l_ in _PAPER:
        if abs(short - s_) <= 2 and abs(long_ - l_) <= 2:
            return name + (" landscape" if abs(w) > abs(h) else "")
    return None


def _box(page, key: str) -> Optional[list]:
    node, hops = page, 0
    while node is not None and hops < 64:
        if key in node:
            try:
                return [round(float(v), 2) for v in _resolve(node[key])][:4]
            except Exception:
                return None
        if key not in _INHERITABLE:
            return None
        parent = node.get("/Parent")
        node = _resolve(parent) if parent is not None else None
        hops += 1
    return None


def read_pdf_page_boxes(pdf_path: Union[str, Path]) -> List[dict]:
    """
    Page boxes as WRITTEN in the file, one dict per page: `mediabox`,
    `cropbox`, `bleedbox`, `trimbox`, `artbox` ([x0, y0, x1, y1] in points,
    None when absent — no defaulting, since which boxes a producer writes is
    itself a fingerprint: print/design tools add Bleed/Trim boxes, office and
    report generators usually write only a MediaBox), plus `rotate`,
    `width`/`height` of the MediaBox and `paper` (A4, Letter, …) when it
    matches a standard size.
    """
    out: List[dict] = []
    for page_no, page in enumerate(PdfReader(str(pdf_path)).pages, start=1):
        rec = {"page": page_no}
        for key in _BOXES:
            rec[key[1:].lower()] = _box(page, key)
        mb = rec["mediabox"]
        if mb:
            rec["width"] = round(mb[2] - mb[0], 2)
            rec["height"] = round(mb[3] - mb[1], 2)
            rec["paper"] = _paper_name(rec["width"], rec["height"])
        else:
            rec["width"] = rec["height"] = rec["paper"] = None
        try:
            rec["rotate"] = int(page.get("/Rotate", 0) or 0)
        except Exception:
            rec["rotate"] = 0
        out.append(rec)
    return out


# ─── text ─────────────────────────────────────────────────────────────────

def _public_run(page_no: int, t: dict) -> dict:
    return {
        "page": page_no,
        "order": t["seq"],
        "text": t["text"],
        "x": round(t["origin"][0], 2),
        "y": round(t["origin"][1], 2),
        "x_end": round(t["end_x"], 2),
        "size": round(t["size"], 2),
        "font": t["font"],
        "visible": t["visible"],
        "decoded": t["decoded"],
        "widths_known": t["widths_known"],
        "op": t["op"],
    }


def read_pdf_text_runs(pdf_path: Union[str, Path]) -> List[dict]:
    """
    Every shown string on every page, in paint order: `text` (decoded; `�`
    for glyphs the font cannot map, `[n glyphs]` when nothing decodes),
    baseline origin `x`/`y` and end `x_end` in points, rendered `size`, `font`
    (BaseFont), `order` (paint sequence shared with images on that page),
    `op` (the text operator it belongs to: the pieces of one kerned TJ share
    it), `visible` (False for render modes 3/7, e.g. OCR layers), `decoded`
    (the text is real) and `widths_known` (`x_end` uses the font's glyph
    widths rather than a half-em guess).

    How a producer splits text into operators (whole lines, words, single
    glyphs) is characteristic of it.
    """
    return [
        _public_run(page_no, t)
        for page_no, _page, interp in _interpret(PdfReader(str(pdf_path)))
        for t in interp.texts
    ]


def group_text_lines(runs: List[dict]) -> List[dict]:
    """
    Merge visible runs into visual lines: same page, baselines within 30% of
    the font size, ordered left to right; a gap wider than 20% of the size
    becomes a space. Each line: `page`, `text`, `x0`, `x1`, `y`, `size`
    (largest) and `sizes` (all), `fonts`, `runs` (text operators that built
    it) and `pieces` (shown strings, counting kerned TJ parts),
    `decoded` / `widths_known` (all its runs),
    `first_segment` / `last_segment` (extent of the leftmost / rightmost
    gap-separated group, e.g. label and value), `order` (paint order of its
    first run) and `orders` (all of them).

    Producers split text differently; lines are what a reader sees, so
    comparisons between files from different generators work on lines.
    """
    lines: List[dict] = []
    by_page: dict = {}
    for r in runs:
        if r["visible"] and r["text"].strip():
            by_page.setdefault(r["page"], []).append(r)
    for page_no in sorted(by_page):
        rows: List[list] = []
        for r in sorted(by_page[page_no], key=lambda r: (-r["y"], r["x"])):
            tol = max(1.0, 0.3 * (r["size"] or 1.0))
            for row in rows:
                if abs(row[0]["y"] - r["y"]) <= tol:
                    row.append(r)
                    break
            else:
                rows.append([r])
        for row in rows:
            row.sort(key=lambda r: r["x"])
            text, prev = "", None
            segments: List[list] = []        # [x0, x1] of gap-separated groups
            for r in row:
                piece = r["text"]
                if prev is not None:
                    gap = r["x"] - prev["x_end"]
                    wide = gap > 0.2 * max(r["size"], prev["size"], 1.0)
                    if wide and not text.endswith(" ") and not piece.startswith(" "):
                        text += " "
                    if wide:
                        segments.append([r["x"], r["x_end"]])
                    else:
                        segments[-1][1] = max(segments[-1][1], r["x_end"])
                else:
                    segments.append([r["x"], r["x_end"]])
                text += piece
                prev = r
            lines.append({
                "page": page_no,
                "text": " ".join(text.split()),
                "x0": row[0]["x"],
                "x1": max(r["x_end"] for r in row),
                "y": round(sum(r["y"] for r in row) / len(row), 2),
                "size": max(r["size"] for r in row),
                "sizes": sorted({r["size"] for r in row}),
                "fonts": sorted({r["font"] or "?" for r in row}),
                # Leftmost / rightmost gap-separated group (label, value…):
                # which group sits on a column edge.
                "first_segment": [round(v, 2) for v in segments[0]],
                "last_segment": [round(v, 2) for v in segments[-1]],
                # A run is one text-showing operator: the pieces of a kerned
                # TJ array count once, so kerning isn't mistaken for splitting.
                "runs": len({r["op"] for r in row}),
                "pieces": len(row),
                "decoded": all(r["decoded"] for r in row),
                "widths_known": all(r["widths_known"] for r in row),
                "order": min(r["order"] for r in row),
                "orders": [r["order"] for r in row],
            })
    return lines


def read_pdf_text_lines(pdf_path: Union[str, Path]) -> List[dict]:
    """`group_text_lines(read_pdf_text_runs(pdf_path))`."""
    return group_text_lines(read_pdf_text_runs(pdf_path))


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
    out: List[dict] = []
    for page_no, page, interp in _interpret(PdfReader(str(pdf_path))):
        try:
            mb = [float(v) for v in page.mediabox]
            page_area = abs((mb[2] - mb[0]) * (mb[3] - mb[1])) or None
        except Exception:
            page_area = None
        for img in interp.images:
            bbox = img["bbox"]
            area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
            visible = [t for t in interp.texts if t["visible"]]
            under = [t for t in visible
                     if t["seq"] < img["seq"] and _inside(t["center"], bbox)]
            over = [t for t in visible
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
