import json
import sys

import click

from .checker import PdfReport, check_pdf
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
def check_cmd(files, as_json, profile):
    """Verify PDF legitimacy. Exit: 0=clean, 1=suspicious, 2=tampered, 3=error."""
    verdict_rank = {"clean": 0, "suspicious": 1, "tampered": 2}
    worst = "clean"
    reports = []
    errored = False

    for f in files:
        try:
            report = check_pdf(f, profile=profile)
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

    if not report.findings:
        click.echo("   no tampering signals detected.")
        return

    icons = {"critical": "🚨", "warning": "⚠️ ", "info": "ℹ️ "}
    for sev in ("critical", "warning", "info"):
        for f in report.findings:
            if f.severity != sev:
                continue
            click.echo(f"   {icons[sev]} [{f.code}] {f.message}")


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

    _section("Hash")
    click.echo(f"  sha256: {summary['sha256']}")


if __name__ == "__main__":
    app()
