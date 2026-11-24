"""Compare two PDFs that should come from the same issuer / template.

Two genuine documents from one system share a FINGERPRINT (PDF version, /ID
format, font-subset naming, resource naming, image encodings, which page boxes
are written) and a LAYOUT (same labels at the same places, same alignment,
same way of splitting text into runs, same paint order). Their VALUES differ —
names, dates, amounts — and that alone means nothing.

So `compare_pdfs` reports three different things:

- `fingerprint.differences` — container traits that differ: different
  generator, or the same one with different settings;
- per page, the layout match: estimated offset between the two pages, labels
  in common, `changed` lines (the variable fields, with a token diff), lines
  `moved` or present on one side only, matched images;
- per page, `anomalies`: places where ONE line behaves unlike the rest of its
  file relative to the other file — an alignment that holds in one file and
  breaks in the other, a line split into runs differently while every other
  line matches, a line painted at a different point of the stream while the
  rest keep the same order, a different font or size on a changed field.
  These are the edit signals.

Positions come from `layout.py` (estimated end-of-text widths), so tolerances
are in points and deliberately loose.
"""

import difflib
import re
import statistics
from pathlib import Path
from typing import List, Optional, Union

from pypdf import PdfReader

from .content import read_pdf_content
from .inspect import read_pdf_revisions, read_pdf_summary
from .layout import (
    group_text_lines,
    read_pdf_image_placements,
    read_pdf_page_boxes,
    read_pdf_text_runs,
)
from .metadata import read_pdf_metadata, read_pdf_xmp

_SEQ_PREFIX = re.compile(r"^[A-Z]A{5}$")


# ─── fingerprint ──────────────────────────────────────────────────────────

def _name_pattern(names) -> List[str]:
    return sorted({re.sub(r"\d+", "#", n) for n in names if n})


def _subset_scheme(fonts) -> Optional[str]:
    prefixes = {f["subset_prefix"] for f in fonts if f["subset_prefix"]}
    if not prefixes:
        return None
    seq = {p for p in prefixes if _SEQ_PREFIX.match(p)}
    if seq == prefixes:
        return "sequential"
    return "random" if not seq else "mixed"


def pdf_fingerprint(pdf_path: Union[str, Path]) -> dict:
    """Producer-identifying traits of a file (no content, no values)."""
    summary = read_pdf_summary(pdf_path)
    rev = read_pdf_revisions(pdf_path)
    info = read_pdf_metadata(pdf_path) or {}
    xmp = read_pdf_xmp(pdf_path)
    content = read_pdf_content(pdf_path)
    boxes = read_pdf_page_boxes(pdf_path)
    runs = read_pdf_text_runs(pdf_path)
    lines = group_text_lines(runs)
    first = boxes[0] if boxes else {}

    return {
        "pdf_version": summary["pdf_version"],
        "pages": summary["pages"],
        "id_bytes": len(rev["id_original"]) // 2 if rev["id_original"] else 0,
        "id_parts_equal": rev["id_match"],
        "has_info": bool(info),
        "has_xmp": xmp is not None,
        "producer": str(info["/Producer"]) if info.get("/Producer") else None,
        "creator": str(info["/Creator"]) if info.get("/Creator") else None,
        "page_size": [first.get("width"), first.get("height")] if first else None,
        "paper": first.get("paper"),
        "boxes_written": [k for k in ("mediabox", "cropbox", "bleedbox", "trimbox", "artbox")
                          if first.get(k) is not None],
        "font_subset_scheme": _subset_scheme(content["fonts"]),
        "font_names": _name_pattern(f["name"] for f in content["fonts"]),
        "image_names": _name_pattern(i["name"] for i in content["images"]),
        "image_encodings": sorted({
            "/".join(i["filters"]) + ":" + (i["color_space"] or "?") for i in content["images"]
        }),
        "runs_per_line": round(len([r for r in runs if r["visible"]]) / len(lines), 2) if lines else None,
    }


# ─── layout matching ──────────────────────────────────────────────────────

def _estimate_offset(lines_a, lines_b, places_a, places_b) -> dict:
    """Translation that maps page A onto page B, from anchors: lines whose
    text is identical and unique on both pages, and images of identical
    on-page size. Median, so a few edited anchors don't move it."""
    def uniq(lines):
        seen: dict = {}
        for ln in lines:
            if not ln.get("decoded", True):
                continue          # "[13 glyphs]" twice proves nothing
            seen.setdefault(ln["text"], []).append(ln)
        return {t: v[0] for t, v in seen.items() if len(v) == 1}

    ua, ub = uniq(lines_a), uniq(lines_b)
    deltas = [(ub[t]["x0"] - ua[t]["x0"], ub[t]["y"] - ua[t]["y"]) for t in ua.keys() & ub.keys()]

    def wh(p):
        return (round(p["bbox"][2] - p["bbox"][0], 1), round(p["bbox"][3] - p["bbox"][1], 1))

    size_b: dict = {}
    for p in places_b:
        size_b.setdefault(wh(p), []).append(p)
    size_a: dict = {}
    for p in places_a:
        size_a.setdefault(wh(p), []).append(p)
    for k, pa in size_a.items():
        pb = size_b.get(k)
        if len(pa) == 1 and pb and len(pb) == 1:
            deltas.append((pb[0]["bbox"][0] - pa[0]["bbox"][0], pb[0]["bbox"][1] - pa[0]["bbox"][1]))

    if not deltas:
        return {"dx": 0.0, "dy": 0.0, "anchors": 0, "spread": None}
    dx = statistics.median(d[0] for d in deltas)
    dy = statistics.median(d[1] for d in deltas)
    spread = max(max(abs(d[0] - dx), abs(d[1] - dy)) for d in deltas)
    return {"dx": round(dx, 2), "dy": round(dy, 2), "anchors": len(deltas),
            "spread": round(spread, 2)}


def _pair_lines(lines_a, lines_b, dx, dy) -> list:
    """One-to-one pairing of lines by position (A shifted by the offset)."""
    cand = []
    for i, a in enumerate(lines_a):
        ax, ay = a["x0"] + dx, a["y"] + dy
        for j, b in enumerate(lines_b):
            tol_y = max(2.0, 0.5 * max(a["size"], b["size"]))
            ey = abs(ay - b["y"])
            if ey > tol_y:
                continue
            ex = abs(ax - b["x0"])
            overlap = min(a["x1"] + dx, b["x1"]) - max(ax, b["x0"])
            if ex > max(3.0, a["size"]) and overlap <= 0:
                continue
            cand.append((2 * ey + ex, i, j))
    cand.sort()
    used_a, used_b, pairs = set(), set(), []
    for _, i, j in cand:
        if i in used_a or j in used_b:
            continue
        used_a.add(i)
        used_b.add(j)
        pairs.append((i, j))
    return pairs


def _glyphs(ln: dict) -> Optional[int]:
    """Glyph count from `[n glyphs]` placeholders (undecoded lines)."""
    nums = re.findall(r"\[(\d+) glyphs?\]", ln["text"])
    return sum(int(n) for n in nums) if nums else None


def _token_diff(a: str, b: str) -> list:
    ta, tb = a.split(), b.split()
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(a=ta, b=tb, autojunk=False).get_opcodes():
        if op != "equal":
            out.append({"a": " ".join(ta[i1:i2]), "b": " ".join(tb[j1:j2])})
    return out


def _brief(ln: dict, dx: float = 0.0, dy: float = 0.0) -> dict:
    return {"text": ln["text"], "x0": round(ln["x0"] + dx, 2), "x1": round(ln["x1"] + dx, 2),
            "y": round(ln["y"] + dy, 2), "size": round(ln["size"], 2), "fonts": ln["fonts"],
            "runs": ln["runs"]}


def _text_index(lines) -> dict:
    """paint sequence → position among this page's visible text runs."""
    seqs = sorted(o for ln in lines for o in ln["orders"])
    return {s: k for k, s in enumerate(seqs)}


def _paint_gaps(line, index) -> int:
    """How many OTHER text runs are painted between this line's first and
    last run (0 = painted in one go)."""
    pos = sorted(index[o] for o in line["orders"])
    return (pos[-1] - pos[0] + 1) - len(pos)


def _alignment_breaks(lines_a, lines_b, pairs, dx: float) -> list:
    """A line that sits on a column edge in one file and leaves it in the other.

    A "column edge" is a left (x0) or right (x1) edge shared by two or more
    matched lines in the reference file whose text group ON that edge varies
    in width (amounts of different lengths, all ending at x=400): only then is
    the edge an alignment rather than equal-length text.
    In the examined file, the member that is off the column while the others
    stay on it is reported (with two members, the one that moved relative to
    the page offset). Typical hit: a right-aligned amount retyped longer from
    its old left edge, so its right edge pokes out of the column.
    """
    b_of_a = dict(pairs)
    a_of_b = {j: i for i, j in pairs}
    out, seen = [], set()

    def tol(ln):
        return max(1.0, 0.1 * ln["size"])

    sides = (
        # (reference, examined, ref→examined, examined name, ref→examined shift)
        (lines_b, lines_a, a_of_b, "a", -dx),
        (lines_a, lines_b, b_of_a, "b", dx),
    )
    for ref, exam, to_exam, name, shift in sides:
        for edge in ("x0", "x1"):
            # Right edges are estimates: only trust them with real widths.
            members_all = sorted(
                k for k in to_exam
                if edge == "x0" or (ref[k].get("widths_known", True)
                                    and exam[to_exam[k]].get("widths_known", True)))
            clusters: list = []
            for s_ in sorted(members_all, key=lambda k: ref[k][edge]):
                if clusters and abs(ref[s_][edge] - ref[clusters[-1][-1]][edge]) <= tol(ref[s_]):
                    clusters[-1].append(s_)
                else:
                    clusters.append([s_])
            for cl in clusters:
                if len(cl) < 2:
                    continue
                # The group ON this edge (value for a right column, label
                # for a left one) must vary in width, or the shared edge is
                # just equal-length text, not alignment.
                seg = "first_segment" if edge == "x0" else "last_segment"
                widths = [ref[k][seg][1] - ref[k][seg][0] for k in cl]
                if max(widths) - min(widths) <= 2 * max(tol(ref[k]) for k in cl):
                    continue
                ex = {k: exam[to_exam[k]] for k in cl}
                for k in cl:
                    others = [ex[o][edge] for o in cl if o != k]
                    m = statistics.median(others)
                    t = tol(ex[k])
                    if abs(ex[k][edge] - m) <= t:
                        continue
                    if len(cl) == 2:
                        # No majority: blame the one that moved against the
                        # page offset (ties: neither).
                        o = cl[0] if cl[1] == k else cl[1]
                        dev_k = abs(ex[k][edge] - (ref[k][edge] + shift))
                        dev_o = abs(ex[o][edge] - (ref[o][edge] + shift))
                        if dev_k <= dev_o:
                            continue
                    elif any(abs(v - m) > t for v in others):
                        continue                  # the column itself is broken up
                    key = (name, to_exam[k], edge)
                    if key in seen:
                        continue
                    seen.add(key)
                    anchor = next(ex[o] for o in cl if o != k)
                    out.append({
                        "type": "alignment_break",
                        "file": name,
                        "edge": "left" if edge == "x0" else "right",
                        "line": ex[k]["text"],
                        "aligned_with": anchor["text"],
                        "gap_pt": round(abs(ex[k][edge] - m), 2),
                    })
    return out


def _compare_page(page_no, lines_a, lines_b, places_a, places_b, imgs_a, imgs_b,
                  boxes_a, boxes_b) -> dict:
    off = _estimate_offset(lines_a, lines_b, places_a, places_b)
    dx, dy = off["dx"], off["dy"]
    pairs = _pair_lines(lines_a, lines_b, dx, dy)

    same, changed, unverifiable = [], [], []
    for i, j in pairs:
        a, b = lines_a[i], lines_b[j]
        if not (a.get("decoded", True) and b.get("decoded", True)):
            unverifiable.append((i, j))
        elif a["text"] == b["text"]:
            same.append((i, j))
        else:
            changed.append((i, j))

    paired_a = {i for i, _ in pairs}
    paired_b = {j for _, j in pairs}
    rest_a = [i for i in range(len(lines_a)) if i not in paired_a]
    rest_b = [j for j in range(len(lines_b)) if j not in paired_b]

    moved = []
    for i in list(rest_a):
        if not lines_a[i].get("decoded", True):
            continue
        for j in rest_b:
            if lines_b[j].get("decoded", True) and lines_a[i]["text"] == lines_b[j]["text"]:
                moved.append({"text": lines_a[i]["text"],
                              "a": _brief(lines_a[i], dx, dy), "b": _brief(lines_b[j]),
                              "delta": [round(lines_b[j]["x0"] - lines_a[i]["x0"] - dx, 2),
                                        round(lines_b[j]["y"] - lines_a[i]["y"] - dy, 2)]})
                rest_a.remove(i)
                rest_b.remove(j)
                break

    anomalies: list = []

    # Changed fields: geometry/typography of the field itself.
    changed_out = []
    for i, j in changed:
        a, b = lines_a[i], lines_b[j]
        flags = []
        if a["fonts"] != b["fonts"]:
            flags.append("font")
        if len(a["sizes"]) != len(b["sizes"]) or any(
                abs(x - y) > 0.1 for x, y in zip(a["sizes"], b["sizes"])):
            flags.append("size")
        if abs(a["y"] + dy - b["y"]) > 0.5:
            flags.append("baseline")
        entry = {"a": _brief(a, dx, dy), "b": _brief(b), "diff": _token_diff(a["text"], b["text"]),
                 "flags": flags}
        changed_out.append(entry)
        values = {
            "font": (a["fonts"], b["fonts"]),
            "size": ([round(v, 2) for v in a["sizes"]], [round(v, 2) for v in b["sizes"]]),
            "baseline": (round(a["y"] + dy, 2), round(b["y"], 2)),  # A in B's frame
        }
        for fl in flags:
            anomalies.append({"type": f"changed_field_{fl}", "a": a["text"], "b": b["text"],
                              "a_value": values[fl][0], "b_value": values[fl][1]})

    # Same text, but sitting somewhere else than the page offset predicts.
    displaced = []
    for i, j in same:
        a, b = lines_a[i], lines_b[j]
        ex, ey = b["x0"] - (a["x0"] + dx), b["y"] - (a["y"] + dy)
        if abs(ex) > 1.0 or abs(ey) > 1.0:
            displaced.append({"text": a["text"], "delta": [round(ex, 2), round(ey, 2)]})

    anomalies += _alignment_breaks(lines_a, lines_b, pairs, dx)

    # Run segmentation: a line built differently while the others agree.
    if len(pairs) >= 3:
        equal = [lines_a[i]["runs"] == lines_b[j]["runs"] for i, j in pairs]
        if sum(equal) / len(equal) >= 0.7:
            for (i, j), eq in zip(pairs, equal):
                if not eq:
                    anomalies.append({"type": "segmentation", "a": lines_a[i]["text"],
                                      "b": lines_b[j]["text"],
                                      "runs_a": lines_a[i]["runs"], "runs_b": lines_b[j]["runs"]})

    # Split painting: a line whose runs are not painted one after another
    # (other text in between) where the reference paints it in one go — the
    # signature of a value appended to the stream after the page was built.
    text_index_a = _text_index(lines_a)
    text_index_b = _text_index(lines_b)
    for i, j in pairs:
        ga = _paint_gaps(lines_a[i], text_index_a)
        gb = _paint_gaps(lines_b[j], text_index_b)
        if ga and not gb:
            anomalies.append({"type": "split_painting", "file": "a", "line": lines_a[i]["text"],
                              "interleaved_runs": ga})
        elif gb and not ga:
            anomalies.append({"type": "split_painting", "file": "b", "line": lines_b[j]["text"],
                              "interleaved_runs": gb})

    # Paint order: a whole line drawn at another point of the stream. For
    # each line, count the other lines whose before/after relation flips
    # between the files; a line moved in the stream flips with many, each of
    # those flips only with it. Two lines that just swap are a tie: ambiguous,
    # not reported.
    if len(pairs) >= 3:
        last_a = {i: max(lines_a[i]["orders"]) for i, _ in pairs}
        last_b = {j: max(lines_b[j]["orders"]) for _, j in pairs}
        flips = []
        for i, j in pairs:
            n = sum(1 for k, m in pairs if k != i and
                    (last_a[i] < last_a[k]) != (last_b[j] < last_b[m]))
            flips.append(n)
        med = statistics.median(flips)
        for (i, j), n in zip(pairs, flips):
            if n >= 2 and n > 2 * med:
                anomalies.append({"type": "paint_order", "a": lines_a[i]["text"],
                                  "b": lines_b[j]["text"], "out_of_order_with": n})

    # Images: pair by on-page size, then by position.
    dims_a = {(i["page"], i["name"]): i for i in imgs_a}
    dims_b = {(i["page"], i["name"]): i for i in imgs_b}
    img_pairs, left_b = [], list(places_b)
    only_img_a = []
    for pa in places_a:
        wa, ha = pa["bbox"][2] - pa["bbox"][0], pa["bbox"][3] - pa["bbox"][1]
        best = None
        for pb in left_b:
            wb, hb = pb["bbox"][2] - pb["bbox"][0], pb["bbox"][3] - pb["bbox"][1]
            if abs(wa - wb) > 1.0 or abs(ha - hb) > 1.0:
                continue
            dist = abs(pa["bbox"][0] + dx - pb["bbox"][0]) + abs(pa["bbox"][1] + dy - pb["bbox"][1])
            if best is None or dist < best[0]:
                best = (dist, pb)
        if best is None:
            only_img_a.append(pa)
            continue
        pb = best[1]
        left_b.remove(pb)
        ia, ib = dims_a.get((pa["page"], pa["name"]), {}), dims_b.get((pb["page"], pb["name"]), {})
        img_pairs.append({
            "a": pa["name"], "b": pb["name"],
            "on_page_size": [round(wa, 2), round(ha, 2)],
            "position_delta": [round(pb["bbox"][0] - pa["bbox"][0] - dx, 2),
                               round(pb["bbox"][1] - pa["bbox"][1] - dy, 2)],
            "pixels_a": [ia.get("width"), ia.get("height")],
            "pixels_b": [ib.get("width"), ib.get("height")],
            "encoding_a": "/".join(ia.get("filters") or []) or None,
            "encoding_b": "/".join(ib.get("filters") or []) or None,
            "transparent_a": pa["transparent"], "transparent_b": pb["transparent"],
        })

    size_delta = None
    if boxes_a and boxes_b and boxes_a.get("width") and boxes_b.get("width"):
        size_delta = [round(boxes_b["width"] - boxes_a["width"], 2),
                      round(boxes_b["height"] - boxes_a["height"], 2)]

    return {
        "page": page_no,
        "boxes_a": boxes_a, "boxes_b": boxes_b, "page_size_delta": size_delta,
        "offset": off,
        "lines_a": len(lines_a), "lines_b": len(lines_b),
        "same": len(same),
        "changed": changed_out,
        # Paired by position, but text not decodable on at least one side:
        # neither "same" nor "changed" can be claimed.
        "unverifiable": [
            {"a": _brief(lines_a[i], dx, dy), "b": _brief(lines_b[j]),
             "glyphs_a": _glyphs(lines_a[i]), "glyphs_b": _glyphs(lines_b[j])}
            for i, j in unverifiable
        ],
        "displaced": displaced,
        "moved": moved,
        "only_a": [_brief(lines_a[i], dx, dy) for i in rest_a],
        "only_b": [_brief(lines_b[j]) for j in rest_b],
        "images": img_pairs,
        "images_only_a": [p["name"] for p in only_img_a],
        "images_only_b": [p["name"] for p in left_b],
        "anomalies": anomalies,
    }


def compare_pdfs(a: Union[str, Path], b: Union[str, Path]) -> dict:
    """
    Compare file `a` against file `b` (typically: `a` = the document under
    examination, `b` = a known-genuine document of the same issuer).

    Returns `{"a", "b", "fingerprint": {"a", "b", "differences"}, "pages":
    [...], "summary"}`; see the module docstring for what each part means.
    Pages are paired by index.
    """
    fp_a, fp_b = pdf_fingerprint(a), pdf_fingerprint(b)
    differences = [{"trait": k, "a": fp_a[k], "b": fp_b[k]}
                   for k in fp_a if fp_a[k] != fp_b.get(k)]

    runs_a, runs_b = read_pdf_text_runs(a), read_pdf_text_runs(b)
    lines_a, lines_b = group_text_lines(runs_a), group_text_lines(runs_b)
    places_a, places_b = read_pdf_image_placements(a), read_pdf_image_placements(b)
    imgs_a, imgs_b = read_pdf_content(a)["images"], read_pdf_content(b)["images"]
    boxes_a, boxes_b = read_pdf_page_boxes(a), read_pdf_page_boxes(b)

    pages = []
    for p in range(1, max(len(PdfReader(str(a)).pages), len(PdfReader(str(b)).pages)) + 1):
        pages.append(_compare_page(
            p,
            [ln for ln in lines_a if ln["page"] == p],
            [ln for ln in lines_b if ln["page"] == p],
            [x for x in places_a if x["page"] == p],
            [x for x in places_b if x["page"] == p],
            imgs_a, imgs_b,
            boxes_a[p - 1] if p <= len(boxes_a) else None,
            boxes_b[p - 1] if p <= len(boxes_b) else None,
        ))

    total_a = sum(pg["lines_a"] for pg in pages)
    matched = sum(pg["same"] + len(pg["changed"]) + len(pg["unverifiable"]) for pg in pages)
    return {
        "a": str(a), "b": str(b),
        "fingerprint": {"a": fp_a, "b": fp_b, "differences": differences},
        "pages": pages,
        "summary": {
            "same_generator": not differences or all(
                d["trait"] in ("pages", "runs_per_line") for d in differences),
            "layout_match": round(matched / total_a, 2) if total_a else None,
            "labels_in_common": sum(pg["same"] for pg in pages),
            "changed_fields": sum(len(pg["changed"]) for pg in pages),
            "unverifiable_lines": sum(len(pg["unverifiable"]) for pg in pages),
            "anomalies": sum(len(pg["anomalies"]) for pg in pages),
        },
    }
