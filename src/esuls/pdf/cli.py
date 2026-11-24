import json
import logging
import sys

import click

from .checker import PdfReport, check_pdf
from .content import read_pdf_content, read_pdf_revision_content
from .compare import compare_pdfs
from .layout import read_pdf_image_placements, read_pdf_page_boxes, read_pdf_text_lines
from .inspect import read_pdf_dynamic, read_pdf_revisions, read_pdf_summary
from .metadata import (
    _normalize_key,
    edit_pdf_metadata,
    find_non_standard_metadata,
    read_pdf_metadata,
    read_pdf_xmp,
)


@click.group()
@click.version_option()
def app():
    """esuls-pdf — read, inspect, edit, and verify PDF documents."""


# ───────────────────────────── check ──────────────────────────────────────

@app.command("check")
@click.argument("files", nargs=-1, required=True,
                type=click.Path(exists=True, dir_okay=False))
@click.option("--json", "as_json", is_flag=True,
              help="Emit JSON instead of human report.")
@click.option("--profile", default="default", show_default=True,
              help="Reserved for future per-issuer profiles.")
@click.option("--trust-root", "trust_roots", multiple=True,
              type=click.Path(exists=True, dir_okay=False),
              help="PEM/DER certificate(s) to trust for signature validation "
                   "(repeatable). Without it no signer is trusted.")
@click.option("--no-verify-signatures", is_flag=True,
              help="Skip cryptographic validation of embedded signatures.")
def check_cmd(files, as_json, profile, trust_roots, no_verify_signatures):
    """Verify PDF legitimacy. Exit: 0=clean, 1=suspicious, 2=tampered, 3=error.

    Signed files are validated cryptographically when the `pdf-sign` extra
    (pyHanko) is installed: integrity, signer key, trust chain, and changes
    made after signing.
    """
    # pyHanko logs every validation failure with a traceback; on the CLI the
    # outcome is already in the report. Handlers the caller set up still work.
    for name in ("pyhanko", "pyhanko_certvalidator"):
        logging.getLogger(name).addHandler(logging.NullHandler())
        logging.getLogger(name).propagate = False

    verdict_rank = {"clean": 0, "suspicious": 1, "tampered": 2}
    worst = "clean"
    reports = []
    errored = False

    for f in files:
        try:
            report = check_pdf(
                f, profile=profile,
                verify_signatures=not no_verify_signatures,
                trust_roots=list(trust_roots) or None,
            )
        except Exception as e:
            errored = True
            click.echo(f"❌ {f}: read error — {e}", err=True)
            continue
        reports.append(report)
        if verdict_rank[report.verdict] > verdict_rank[worst]:
            worst = report.verdict

    if as_json:
        click.echo(json.dumps([r.to_dict() for r in reports], indent=2, default=str))
    else:
        for r in reports:
            _render_check(r)

    if errored:
        sys.exit(3)
    sys.exit(verdict_rank[worst])


# ───────────────────────────── inspect ────────────────────────────────────

@app.command("inspect")
@click.argument("files", nargs=-1, required=True,
                type=click.Path(exists=True, dir_okay=False))
@click.option("--full", is_flag=True,
              help="Also print revisions, dynamic content, sha256.")
def inspect_cmd(files, full):
    """Print metadata (Info dict + XMP), and optionally the full forensic surface."""
    for f in files:
        _render_inspect(f, full=full)


# ───────────────────────────── compare ────────────────────────────────────

@app.command("compare")
@click.argument("suspect", type=click.Path(exists=True, dir_okay=False))
@click.argument("reference", type=click.Path(exists=True, dir_okay=False))
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of a report.")
def compare_cmd(suspect, reference, as_json):
    """Compare SUSPECT against REFERENCE (a genuine document of the same issuer).

    Reports container differences (different generator or settings), the
    layout match (offset, labels in common, fields that differ) and the
    anomalies that point to an edit: broken alignments, lines split or
    painted differently from the rest, fields in another font/size.
    """
    result = compare_pdfs(suspect, reference)
    if as_json:
        click.echo(json.dumps(result, indent=2, default=str))
        return
    _render_compare(result)


def _render_compare(r: dict) -> None:
    click.echo(f"\n📄 A (suspect):   {r['a']}\n📄 B (reference): {r['b']}")

    _section("Fingerprint")
    diffs = r["fingerprint"]["differences"]
    if not diffs:
        click.echo("  ✅ same container traits — consistent with the same generator")
    for d in diffs:
        click.echo(f"  ≠ {d['trait']}: A={d['a']!r}  B={d['b']!r}")

    for pg in r["pages"]:
        _section(f"Page {pg['page']}")
        ba, bb = pg["boxes_a"] or {}, pg["boxes_b"] or {}
        click.echo(f"  size: A {ba.get('width')}×{ba.get('height')} ({ba.get('paper') or '?'})"
                   f" · B {bb.get('width')}×{bb.get('height')} ({bb.get('paper') or '?'})")
        off = pg["offset"]
        click.echo(f"  offset A→B: dx={off['dx']:+g} dy={off['dy']:+g} pt "
                   f"({off['anchors']} anchors, spread {off['spread']})")
        click.echo(f"  lines: A {pg['lines_a']} · B {pg['lines_b']} · "
                   f"in common {pg['same']} · changed {len(pg['changed'])}"
                   + (f" · unverifiable {len(pg['unverifiable'])}" if pg["unverifiable"] else ""))
        if pg["unverifiable"]:
            click.echo("  ⚠️  text not decodable on these lines — same/changed cannot be told:")
            for u in pg["unverifiable"]:
                ga, gb = u["glyphs_a"], u["glyphs_b"]
                cnt = f"  glyphs A {ga} · B {gb}{'  ≠' if ga != gb else ''}" if ga or gb else ""
                click.echo(f"    ? y={u['b']['y']:g}: A {u['a']['text']!r} · B {u['b']['text']!r}{cnt}")
        if pg["changed"]:
            click.echo("  fields that differ:")
            for c in pg["changed"]:
                diff = "; ".join(f"{d['a'] or '∅'} → {d['b'] or '∅'}" for d in c["diff"])
                fl = f"  [{', '.join(c['flags'])}]" if c["flags"] else ""
                click.echo(f"    · {diff}{fl}")
        for m in pg["moved"]:
            click.echo(f"  ↔ moved: {m['text']!r} by {m['delta']}")
        for d in pg["displaced"]:
            click.echo(f"  ↔ displaced: {d['text']!r} by {d['delta']} pt")
        for ln in pg["only_a"]:
            click.echo(f"  − only in A: {ln['text']!r} @ ({ln['x0']:g}, {ln['y']:g})")
        for ln in pg["only_b"]:
            click.echo(f"  + only in B: {ln['text']!r} @ ({ln['x0']:g}, {ln['y']:g})")
        for im in pg["images"]:
            px = "" if im["pixels_a"] == im["pixels_b"] else \
                f"  pixels A {im['pixels_a'][0]}×{im['pixels_a'][1]} vs B {im['pixels_b'][0]}×{im['pixels_b'][1]}"
            enc = "" if im["encoding_a"] == im["encoding_b"] else \
                f"  encoding A {im['encoding_a']} vs B {im['encoding_b']}"
            click.echo(f"  🖼  {im['a']} ↔ {im['b']} {im['on_page_size'][0]:g}×{im['on_page_size'][1]:g} pt{px}{enc}")
        for n in pg["images_only_a"]:
            click.echo(f"  🖼  only in A: {n}")
        for n in pg["images_only_b"]:
            click.echo(f"  🖼  only in B: {n}")
        if pg["anomalies"]:
            click.echo("  anomalies:")
        for a in pg["anomalies"]:
            click.echo(f"    🚩 {_anomaly_text(a)}")

    s = r["summary"]
    _section("Summary")
    click.echo(f"  same generator: {'yes' if s['same_generator'] else 'NO'} · "
               f"layout match {s['layout_match']} · labels in common {s['labels_in_common']} · "
               f"fields that differ {s['changed_fields']} · anomalies {s['anomalies']}"
               + (f" · unverifiable lines {s['unverifiable_lines']}" if s["unverifiable_lines"] else ""))


def _anomaly_text(a: dict) -> str:
    t = a["type"]
    if t == "alignment_break":
        return (f"{a['file'].upper()}: {a['line']!r} no longer {a['edge']}-aligned with "
                f"{a['aligned_with']!r} (off by {a['gap_pt']} pt)")
    if t == "segmentation":
        return f"{a['a']!r} built from {a['runs_a']} run(s) in A vs {a['runs_b']} in B"
    if t == "split_painting":
        return (f"{a['file'].upper()}: {a['line']!r} painted in two moments "
                f"({a['interleaved_runs']} other run(s) in between)")
    if t == "paint_order":
        return (f"{a['a']!r} painted at a different point of the stream "
                f"(out of order with {a['out_of_order_with']} other lines)")
    if t.startswith("changed_field_"):
        what = t.removeprefix("changed_field_")
        return f"field {a['a']!r} vs {a['b']!r}: different {what} (A {a['a_value']} · B {a['b_value']})"
    return str(a)


# ───────────────────────────── edit ───────────────────────────────────────

@app.command("edit")
@click.argument("file", type=click.Path(exists=True, dir_okay=False))
@click.option("--set", "set_pairs", multiple=True, metavar="KEY=VALUE",
              help="Set/update a metadata key (repeatable).")
@click.option("--unset", "unset_keys", multiple=True, metavar="KEY",
              help="Remove a metadata key (repeatable).")
@click.option("--output", type=click.Path(dir_okay=False),
              help="Output path. Mutex with --inplace.")
@click.option("--inplace", is_flag=True,
              help="Overwrite the original file. Mutex with --output.")
@click.option("--keep-xmp", is_flag=True,
              help="Keep the original XMP packet (may diverge from Info dict).")
@click.option("--no-mod-date", is_flag=True,
              help="Do not auto-update /ModDate to now.")
def edit_cmd(file, set_pairs, unset_keys, output, inplace,
             keep_xmp, no_mod_date):
    """Set or unset metadata keys on a PDF (Info dict)."""
    if not (set_pairs or unset_keys):
        raise click.UsageError("nothing to do — pass --set and/or --unset.")
    if output and inplace:
        raise click.UsageError("--output and --inplace are mutually exclusive.")

    updates = {}
    for pair in set_pairs:
        if "=" not in pair:
            raise click.UsageError(f"--set expects KEY=VALUE, got: {pair!r}")
        k, v = pair.split("=", 1)
        updates[_normalize_key(k)] = v
    drops = [_normalize_key(k) for k in unset_keys]

    out = edit_pdf_metadata(
        file,
        updates=updates or None,
        drops=drops or None,
        output_path=output if not inplace else None,
        inplace=inplace,
        update_mod_date=not no_mod_date,
        keep_xmp=keep_xmp,
    )

    click.echo(f"✅ {file} → {out}")


# ───────────────────────────── render helpers ─────────────────────────────

def _section(title: str) -> None:
    click.echo(f"\n── {title} " + "─" * max(0, 58 - len(title)))


_VERDICT_LABEL = {
    "clean": "✅ CLEAN",
    "suspicious": "⚠️  SUSPICIOUS",
    "tampered": "🚨 TAMPERED",
}


def _summary_line(s: dict) -> str:
    flags = []
    if s.get("encrypted"):
        flags.append("encrypted")
    if s.get("signatures"):
        flags.append(f"{s['signatures']} signature(s)")
    if s.get("linearized"):
        flags.append("linearized")
    flags_str = " · ".join(flags) if flags else "no encryption / no signatures"
    return (
        f"{s.get('pages')} page(s) · {s.get('file_size')} bytes · "
        f"PDF {s.get('pdf_version')} · {flags_str}"
    )


def _render_check(report: PdfReport) -> None:
    label = _VERDICT_LABEL.get(report.verdict, report.verdict)
    click.echo(f"\n📄 {report.path}  →  {label}")
    click.echo(f"   {_summary_line(report.summary)}")
    for s in report.signatures or []:
        click.echo(f"   {_signature_line(s)}")

    if not report.findings:
        click.echo("   no tampering signals detected.")
        return

    icons = {"critical": "🚨", "warning": "⚠️ ", "info": "ℹ️ "}
    for sev in ("critical", "warning", "info"):
        for f in report.findings:
            if f.severity != sev:
                continue
            click.echo(f"   {icons[sev]} [{f.code}] {f.message}")


def _signature_line(s) -> str:
    if s.error:
        state = "❌ unreadable"
    elif not (s.intact and s.valid) or s.revoked or s.docmdp_ok is False:
        state = "❌ INVALID"
    elif s.trusted:
        state = "🔏 valid, trusted"
    else:
        state = "🔓 valid, untrusted"
    kind = "doc timestamp" if s.kind == "doc_timestamp" else "signature"
    when = f" @ {s.signing_time.isoformat()}" if s.signing_time else ""
    return f"{state} {kind} {s.field_name!r} by {s.signer or '?'}{when}"


def _render_inspect(pdf: str, *, full: bool) -> None:
    click.echo(f"\n📄 File: {pdf}")
    try:
        summary = read_pdf_summary(pdf)
    except Exception as e:
        click.echo(f"   ❌ Cannot read PDF: {e}")
        return

    click.echo(f"   {_summary_line(summary)}")

    info = None
    try:
        info = read_pdf_metadata(pdf)
    except Exception as e:
        click.echo(f"   ⚠️  Error reading Info dict: {e}")

    _section("Info dict")
    if info:
        for k, v in info.items():
            click.echo(f"  {k}: {v}")
    else:
        click.echo("  (none)")

    xmp = None
    try:
        xmp = read_pdf_xmp(pdf)
    except Exception as e:
        click.echo(f"\n  ⚠️  Error reading XMP: {e}")

    _section("XMP")
    if xmp:
        for k, v in xmp.items():
            click.echo(f"  {k}: {v}")
    else:
        click.echo("  (no XMP stream)")

    extra = find_non_standard_metadata(info)
    if extra:
        _section("Non-standard keys")
        for k in sorted(extra):
            click.echo(f"  ℹ️  {k}: {info[k]}")

    if not full:
        return

    _section("Page boxes")
    try:
        for b in read_pdf_page_boxes(pdf):
            paper = f" ({b['paper']})" if b["paper"] else ""
            rot = f" · rotate {b['rotate']}" if b["rotate"] else ""
            click.echo(f"  p.{b['page']} {b['width']}×{b['height']} pt{paper}{rot}")
            for key in ("mediabox", "cropbox", "bleedbox", "trimbox", "artbox"):
                v = b[key]
                click.echo(f"    {key[:-3].capitalize() + 'Box':9} "
                           f"{'[' + ', '.join(f'{x:g}' for x in v) + ']' if v else '—'}")
    except Exception as e:
        click.echo(f"  ⚠️  error: {e}")

    _section("Revisions / integrity")
    try:
        rev = read_pdf_revisions(pdf)
        click.echo(
            f"  %%EOF count: {rev['eof_count']}"
            f"{'  (incremental updates)' if rev['eof_count'] > 1 else ''}"
        )
        click.echo(f"  /Prev in trailer: {rev['has_prev_in_trailer']}")
        if rev["id_original"]:
            click.echo(f"  ID[0] (original): {rev['id_original']}")
            click.echo(f"  ID[1] (current):  {rev['id_current']}")
            click.echo(f"  ID match: {rev['id_match']}")
        else:
            click.echo("  /ID missing")
        click.echo(
            f"  bytes after last %%EOF: {rev['trailing_bytes_after_eof']}"
            f"{'  ⚠️ junk appended' if rev['trailing_bytes_after_eof'] > 4 else ''}"
        )
    except Exception as e:
        click.echo(f"  ⚠️  error: {e}")

    _section("Dynamic content")
    try:
        dyn = read_pdf_dynamic(pdf)
        click.echo(f"  /AcroForm:         {dyn['has_acroform']}")
        click.echo(f"  /OpenAction:       {dyn['has_openaction']}")
        click.echo(f"  /AA (auto-action): {dyn['has_additional_actions']}")
        click.echo(f"  embedded files:    {dyn['embedded_files']}")
        click.echo(f"  JavaScript actions:{dyn['javascript_actions']}")
    except Exception as e:
        click.echo(f"  ⚠️  error: {e}")

    try:
        content = read_pdf_content(pdf)
        content_revs = read_pdf_revision_content(pdf)
    except Exception as e:
        click.echo(f"\n  ⚠️  content inventory error: {e}")
    else:
        _section("Fonts (page content)")
        if not content["fonts"]:
            click.echo("  (none)")
        for f in content["fonts"]:
            emb = "embedded" if f["embedded"] else (
                "standard-14" if f["standard14"] else "NOT embedded")
            click.echo(f"  p.{f['page']} {f['name']}: {f['base_font']} "
                       f"[{f['subtype']}, {emb}]")

        _section("Images (page content)")
        if not content["images"]:
            click.echo("  (none)")
        for i in content["images"]:
            ed = f"  ⚠️ edited with {i['editor']}" if i["editor"] else ""
            click.echo(f"  p.{i['page']} {i['name']}: {i['width']}x{i['height']} "
                       f"{'/'.join(i['filters']) or 'raw'} {i['color_space'] or ''}{ed}")

        try:
            placements = read_pdf_image_placements(pdf)
        except Exception as e:
            placements = []
            click.echo(f"  ⚠️  placement error: {e}")
        if placements:
            _section("Image placement (paint order)")
            for p in placements:
                x0, y0, x1, y1 = p["bbox"]
                pf = p["page_fraction"]
                # Small images (logos, icons) would all print as "0%".
                frac = "" if pf is None else f" ({pf:.1%} of page)" if pf < 0.1 else f" ({pf:.0%} of page)"
                alpha = " alpha" if p["transparent"] else ""
                click.echo(f"  p.{p['page']} {p['name']}: [{x0:g},{y0:g} → {x1:g},{y1:g}]{frac}{alpha}")
                if p["covered_text"]:
                    click.echo(f"      🚨 covers earlier text: {p['covered_text']!r}")
                if p["text_on_top"]:
                    click.echo(f"      text drawn over it: {p['text_on_top']!r}")

        try:
            lines = read_pdf_text_lines(pdf)
        except Exception as e:
            lines = []
            click.echo(f"  ⚠️  text error: {e}")
        if lines:
            _section("Text lines (x0 → x1, baseline y, size, font, runs, paint order)")
            for ln in lines:
                click.echo(
                    f"  p.{ln['page']} [{ln['x0']:7.2f} → {ln['x1']:7.2f}] y={ln['y']:7.2f} "
                    f"{ln['size']:5.2f}pt {','.join(ln['fonts'])} ×{ln['runs']} #{ln['order']}  "
                    f"{ln['text']!r}"
                )

        if content_revs:
            _section("Added by later revisions")
            for r in content_revs:
                if r.get("error"):
                    click.echo(f"  rev {r['revision']}: ⚠️  {r['error']}")
                    continue
                parts = (
                    [f"+font {f['base_font']}" for f in r["added_fonts"]]
                    + [f"~font {f['previous_base_font']}→{f['base_font']}" for f in r["modified_fonts"]]
                    + [f"+image {i['width']}x{i['height']}" for i in r["added_images"]]
                )
                click.echo(f"  rev {r['revision']}: " + (", ".join(parts) or "no page fonts/images"))

    _section("Hash")
    click.echo(f"  sha256: {summary['sha256']}")


if __name__ == "__main__":
    app()
