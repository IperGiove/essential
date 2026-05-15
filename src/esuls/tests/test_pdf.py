"""
Tests for the pdf subpackage: metadata read/edit, inspect surfaces,
checker verdicts, and the `esuls-pdf edit` CLI command.

PDFs are produced on the fly with pypdf so no fixture files are needed.
"""
import json
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from click.testing import CliRunner
from pypdf import PdfWriter

from esuls.pdf.checker import check_pdf
from esuls.pdf.cli import app
from esuls.pdf.inspect import (
    parse_pdf_date,
    read_pdf_dynamic,
    read_pdf_revisions,
    read_pdf_summary,
)
from esuls.pdf.metadata import (
    edit_pdf_metadata,
    find_non_standard_metadata,
    read_pdf_metadata,
    remove_pdf_metadata,
    replace_pdf_metadata,
    update_pdf_metadata,
)


# ─── helpers ──────────────────────────────────────────────────────────────

def _make_pdf(path: Path, metadata: dict | None = None) -> Path:
    """Write a 1-page PDF with the given Info dict."""
    w = PdfWriter()
    w.add_blank_page(width=72, height=72)
    if metadata:
        w.add_metadata(metadata)
    with open(path, "wb") as fh:
        w.write(fh)
    return path


def _pdf_date(dt: datetime) -> str:
    """Format a datetime as a PDF date string."""
    return dt.strftime("D:%Y%m%d%H%M%S+00'00'")


# ─── metadata round-trip ──────────────────────────────────────────────────

def test_read_pdf_metadata(tmp_path: Path):
    src = _make_pdf(tmp_path / "in.pdf", {"/Title": "T", "/Author": "A"})
    info = read_pdf_metadata(src)
    assert info["/Title"] == "T"
    assert info["/Author"] == "A"
    print("  [PASS] read_pdf_metadata round-trip")


def test_update_pdf_metadata_preserves_other_keys(tmp_path: Path):
    src = _make_pdf(tmp_path / "in.pdf", {"/Title": "T", "/Author": "A"})
    out = update_pdf_metadata(
        src, {"/Title": "NEW"},
        output_path=tmp_path / "out.pdf",
        update_mod_date=False,
    )
    info = read_pdf_metadata(out)
    assert info["/Title"] == "NEW"
    assert info["/Author"] == "A"
    assert "/ModDate" not in info
    print("  [PASS] update_pdf_metadata preserves untouched keys")


def test_replace_pdf_metadata_blows_away_existing(tmp_path: Path):
    src = _make_pdf(tmp_path / "in.pdf", {"/Title": "T", "/Author": "A"})
    out = replace_pdf_metadata(
        src, {"/Title": "ONLY"},
        output_path=tmp_path / "out.pdf",
        update_mod_date=False,
    )
    info = read_pdf_metadata(out)
    assert info.get("/Title") == "ONLY"
    assert "/Author" not in info
    print("  [PASS] replace_pdf_metadata replaces entire dict")


def test_remove_pdf_metadata_drops_keys(tmp_path: Path):
    src = _make_pdf(tmp_path / "in.pdf", {"/Title": "T", "/Author": "A"})
    out = remove_pdf_metadata(
        src, ["/Author"],
        output_path=tmp_path / "out.pdf",
        update_mod_date=False,
    )
    info = read_pdf_metadata(out)
    assert info["/Title"] == "T"
    assert "/Author" not in info
    print("  [PASS] remove_pdf_metadata drops keys")


def test_find_non_standard_metadata():
    info = {
        "/Title": "T", "/Author": "A",
        "/MyCustomKey": "x", "/AnotherCustom": "y",
    }
    extras = find_non_standard_metadata(info)
    assert extras == {"/MyCustomKey", "/AnotherCustom"}
    assert find_non_standard_metadata(None) == set()
    assert find_non_standard_metadata({}) == set()
    print("  [PASS] find_non_standard_metadata identifies extras")


# ─── edit_pdf_metadata (the new unified write) ────────────────────────────

def test_edit_pdf_metadata_applies_updates_and_drops(tmp_path: Path):
    src = _make_pdf(tmp_path / "in.pdf",
                    {"/Title": "T", "/Author": "A", "/Subject": "S"})
    out = edit_pdf_metadata(
        src,
        updates={"/Title": "NEW", "/Keywords": "k1,k2"},
        drops=["/Author"],
        output_path=tmp_path / "out.pdf",
    )
    info = read_pdf_metadata(out)
    assert info["/Title"] == "NEW"
    assert info["/Keywords"] == "k1,k2"
    assert info["/Subject"] == "S"   # untouched
    assert "/Author" not in info
    print("  [PASS] edit_pdf_metadata applies updates+drops in one pass")


def test_edit_pdf_metadata_drops_win_over_updates(tmp_path: Path):
    """If a key is in both updates and drops, drops win (key is removed)."""
    src = _make_pdf(tmp_path / "in.pdf", {"/Title": "T"})
    out = edit_pdf_metadata(
        src,
        updates={"/Title": "NEW", "/Keywords": "k"},
        drops=["/Title"],
        output_path=tmp_path / "out.pdf",
        update_mod_date=False,
    )
    info = read_pdf_metadata(out)
    assert "/Title" not in info
    assert info.get("/Keywords") == "k"
    print("  [PASS] edit_pdf_metadata: drops win over updates")


def test_edit_pdf_metadata_single_moddate_stamp(tmp_path: Path):
    """Regression test: only ONE /ModDate bump per edit call (not two)."""
    src = _make_pdf(tmp_path / "in.pdf", {"/Title": "T", "/Author": "A"})
    out = edit_pdf_metadata(
        src,
        updates={"/Title": "NEW"},
        drops=["/Author"],
        output_path=tmp_path / "out.pdf",
    )
    info = read_pdf_metadata(out)
    mod1 = info["/ModDate"]
    assert mod1.startswith("D:")

    # Edit again after a measurable wait — should bump to a strictly later
    # ModDate (proves the stamp is applied per call, not duplicated/skipped).
    time.sleep(1.1)
    out2 = edit_pdf_metadata(
        out,
        updates={"/Subject": "S"},
        output_path=tmp_path / "out2.pdf",
    )
    mod2 = read_pdf_metadata(out2)["/ModDate"]
    assert mod2 != mod1, "ModDate should bump on a fresh edit"
    print("  [PASS] edit_pdf_metadata bumps /ModDate exactly once per call")


def test_edit_pdf_metadata_no_mod_date_when_disabled(tmp_path: Path):
    src = _make_pdf(tmp_path / "in.pdf", {"/Title": "T"})
    out = edit_pdf_metadata(
        src,
        updates={"/Title": "X"},
        output_path=tmp_path / "out.pdf",
        update_mod_date=False,
    )
    info = read_pdf_metadata(out)
    assert "/ModDate" not in info
    print("  [PASS] edit_pdf_metadata respects update_mod_date=False")


def test_edit_pdf_metadata_caller_moddate_wins(tmp_path: Path):
    src = _make_pdf(tmp_path / "in.pdf", {"/Title": "T"})
    explicit = "D:20200101000000+00'00'"
    out = edit_pdf_metadata(
        src,
        updates={"/Title": "X", "/ModDate": explicit},
        output_path=tmp_path / "out.pdf",
    )
    info = read_pdf_metadata(out)
    assert info["/ModDate"] == explicit
    print("  [PASS] edit_pdf_metadata: caller-supplied /ModDate is preserved")


def test_edit_pdf_metadata_drops_moddate(tmp_path: Path):
    src = _make_pdf(tmp_path / "in.pdf",
                    {"/Title": "T", "/ModDate": "D:20200101000000+00'00'"})
    out = edit_pdf_metadata(
        src,
        drops=["/ModDate"],
        output_path=tmp_path / "out.pdf",
    )
    info = read_pdf_metadata(out)
    assert "/ModDate" not in info, (
        "auto-bump must not re-add a /ModDate that the caller dropped"
    )
    print("  [PASS] edit_pdf_metadata: drops /ModDate cleanly")


def test_edit_pdf_metadata_inplace(tmp_path: Path):
    src = _make_pdf(tmp_path / "in.pdf", {"/Title": "T", "/Author": "A"})
    out = edit_pdf_metadata(
        src,
        updates={"/Title": "X"},
        drops=["/Author"],
        inplace=True,
    )
    assert out == src
    info = read_pdf_metadata(src)
    assert info["/Title"] == "X"
    assert "/Author" not in info
    print("  [PASS] edit_pdf_metadata inplace overwrites source")


# ─── inspect surfaces ─────────────────────────────────────────────────────

def test_parse_pdf_date():
    assert parse_pdf_date(None) is None
    assert parse_pdf_date("") is None

    # Offsets are now applied and normalised to naive UTC (was: stripped).
    # +02:00 means the local clock is ahead of UTC, so UTC = local - 2h.
    d = parse_pdf_date("D:20240115093000+02'00'")
    assert d == datetime(2024, 1, 15, 7, 30, 0)

    # -05:00 (e.g. New York) → UTC = local + 5h
    d_neg = parse_pdf_date("D:20240115093000-05'00'")
    assert d_neg == datetime(2024, 1, 15, 14, 30, 0)

    # Explicit Z (UTC) → unchanged
    d_z = parse_pdf_date("D:20240115093000Z")
    assert d_z == datetime(2024, 1, 15, 9, 30, 0)

    # without D: prefix and no tz → naive, interpreted as already-UTC
    d2 = parse_pdf_date("20240115093000")
    assert d2 == datetime(2024, 1, 15, 9, 30, 0)

    # date-only fallback
    d3 = parse_pdf_date("D:20240115")
    assert d3 == datetime(2024, 1, 15)

    assert parse_pdf_date("not-a-date") is None
    print("  [PASS] parse_pdf_date handles all PDF-date variants (UTC-normalised)")


def test_read_pdf_summary(tmp_path: Path):
    src = _make_pdf(tmp_path / "in.pdf", {"/Title": "T"})
    s = read_pdf_summary(src)
    assert s["pages"] == 1
    assert s["file_size"] > 0
    assert len(s["sha256"]) == 64
    assert s["pdf_version"] is not None
    assert s["encrypted"] is False
    assert s["signatures"] == 0
    print("  [PASS] read_pdf_summary returns plausible identity")


def test_read_pdf_revisions_clean(tmp_path: Path):
    src = _make_pdf(tmp_path / "in.pdf", {"/Title": "T"})
    r = read_pdf_revisions(src)
    assert r["eof_count"] >= 1
    assert r["trailing_bytes_after_eof"] <= 4
    print("  [PASS] read_pdf_revisions on clean PDF")


def test_read_pdf_revisions_with_appended_junk(tmp_path: Path):
    src = _make_pdf(tmp_path / "in.pdf", {"/Title": "T"})
    with open(src, "ab") as fh:
        fh.write(b"junk appended after EOF\n")
    r = read_pdf_revisions(src)
    assert r["trailing_bytes_after_eof"] > 4
    print("  [PASS] read_pdf_revisions detects bytes after %%EOF")


def test_read_pdf_dynamic_clean(tmp_path: Path):
    src = _make_pdf(tmp_path / "in.pdf", {"/Title": "T"})
    d = read_pdf_dynamic(src)
    assert d["has_acroform"] is False
    assert d["has_openaction"] is False
    assert d["has_additional_actions"] is False
    assert d["embedded_files"] == 0
    assert d["javascript_actions"] == 0
    print("  [PASS] read_pdf_dynamic on clean PDF")


# ─── checker verdicts ─────────────────────────────────────────────────────

def test_check_pdf_clean(tmp_path: Path):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    src = _make_pdf(tmp_path / "in.pdf", {
        "/Title": "T",
        "/CreationDate": _pdf_date(now - timedelta(hours=1)),
        "/ModDate": _pdf_date(now),
    })
    report = check_pdf(src)
    critical = [f for f in report.findings if f.severity == "critical"]
    assert not critical, [f.code for f in critical]
    assert report.verdict in ("clean", "suspicious")
    print(f"  [PASS] check_pdf clean → {report.verdict}")


def test_check_pdf_mod_before_creation(tmp_path: Path):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    src = _make_pdf(tmp_path / "in.pdf", {
        "/CreationDate": _pdf_date(now),
        "/ModDate": _pdf_date(now - timedelta(hours=1)),  # mod precedes creation
    })
    report = check_pdf(src)
    codes = {f.code for f in report.findings}
    assert "dates.mod_before_creation" in codes, codes
    assert report.verdict == "tampered"
    print("  [PASS] check_pdf flags /ModDate before /CreationDate")


def test_check_pdf_creation_in_future(tmp_path: Path):
    far_future = datetime(2099, 1, 1)
    src = _make_pdf(tmp_path / "in.pdf", {
        "/CreationDate": _pdf_date(far_future),
        "/ModDate": _pdf_date(far_future),
    })
    report = check_pdf(src)
    codes = {f.code for f in report.findings}
    assert "dates.creation_in_future" in codes, codes
    assert report.verdict == "tampered"
    print("  [PASS] check_pdf flags future /CreationDate")


def test_check_pdf_junk_after_eof(tmp_path: Path):
    src = _make_pdf(tmp_path / "in.pdf", {"/Title": "T"})
    with open(src, "ab") as fh:
        fh.write(b"\nappended-junk-payload\n")
    report = check_pdf(src)
    codes = {f.code for f in report.findings}
    assert "structure.junk_after_eof" in codes, codes
    assert report.verdict == "tampered"
    print("  [PASS] check_pdf flags junk bytes after %%EOF")


def test_check_pdf_to_dict_serializable(tmp_path: Path):
    src = _make_pdf(tmp_path / "in.pdf", {"/Title": "T"})
    report = check_pdf(src)
    d = report.to_dict()
    blob = json.dumps(d, default=str)  # must round-trip via json
    assert "verdict" in d
    assert isinstance(blob, str) and len(blob) > 0
    print("  [PASS] PdfReport.to_dict() is JSON-serializable")


# ─── CLI ──────────────────────────────────────────────────────────────────

def test_cli_edit_set_and_unset(tmp_path: Path):
    src = _make_pdf(tmp_path / "in.pdf", {"/Title": "T", "/Author": "A"})
    out = tmp_path / "out.pdf"
    runner = CliRunner()
    r = runner.invoke(app, [
        "edit", str(src),
        "--set", "Title=NEW",
        "--unset", "Author",
        "--output", str(out),
    ])
    assert r.exit_code == 0, r.output
    assert out.exists()
    info = read_pdf_metadata(out)
    assert info["/Title"] == "NEW"
    assert "/Author" not in info
    assert "/ModDate" in info  # auto-bumped
    print("  [PASS] CLI edit --set + --unset writes once")


def test_cli_edit_inplace(tmp_path: Path):
    src = _make_pdf(tmp_path / "in.pdf", {"/Title": "T"})
    runner = CliRunner()
    r = runner.invoke(app, [
        "edit", str(src),
        "--set", "Title=X",
        "--inplace",
    ])
    assert r.exit_code == 0, r.output
    info = read_pdf_metadata(src)
    assert info["/Title"] == "X"
    print("  [PASS] CLI edit --inplace overwrites source")


def test_cli_edit_rejects_inplace_with_output(tmp_path: Path):
    src = _make_pdf(tmp_path / "in.pdf", {"/Title": "T"})
    runner = CliRunner()
    r = runner.invoke(app, [
        "edit", str(src),
        "--set", "Title=X",
        "--inplace",
        "--output", str(tmp_path / "out.pdf"),
    ])
    assert r.exit_code != 0
    assert "mutually exclusive" in r.output
    print("  [PASS] CLI edit rejects --inplace + --output")


def test_cli_edit_requires_action(tmp_path: Path):
    src = _make_pdf(tmp_path / "in.pdf", {"/Title": "T"})
    runner = CliRunner()
    r = runner.invoke(app, ["edit", str(src), "--inplace"])
    assert r.exit_code != 0
    assert "nothing to do" in r.output
    print("  [PASS] CLI edit requires --set or --unset")


def test_cli_check_clean_exits_zero(tmp_path: Path):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    src = _make_pdf(tmp_path / "in.pdf", {
        "/CreationDate": _pdf_date(now - timedelta(hours=1)),
        "/ModDate": _pdf_date(now),
    })
    runner = CliRunner()
    r = runner.invoke(app, ["check", str(src)])
    assert r.exit_code == 0, r.output
    print("  [PASS] CLI check exits 0 on clean PDF")


def test_cli_check_tampered_exits_two(tmp_path: Path):
    src = _make_pdf(tmp_path / "in.pdf", {"/Title": "T"})
    with open(src, "ab") as fh:
        fh.write(b"\nappended-junk\n")
    runner = CliRunner()
    r = runner.invoke(app, ["check", str(src)])
    assert r.exit_code == 2, (r.exit_code, r.output)
    print("  [PASS] CLI check exits 2 on tampered PDF")


def test_cli_check_json(tmp_path: Path):
    src = _make_pdf(tmp_path / "in.pdf", {"/Title": "T"})
    runner = CliRunner()
    r = runner.invoke(app, ["check", str(src), "--json"])
    assert r.exit_code in (0, 1, 2), r.output
    payload = json.loads(r.output)
    assert isinstance(payload, list) and len(payload) == 1
    assert "verdict" in payload[0]
    print("  [PASS] CLI check --json emits structured output")


# ─── runner ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("PDF MODULE TESTS")
    print("=" * 60)

    metadata_tests = [
        ("read_pdf_metadata round-trip", test_read_pdf_metadata),
        ("update preserves other keys", test_update_pdf_metadata_preserves_other_keys),
        ("replace blows away dict", test_replace_pdf_metadata_blows_away_existing),
        ("remove drops keys", test_remove_pdf_metadata_drops_keys),
        ("find_non_standard_metadata", lambda _t: test_find_non_standard_metadata()),
        ("edit applies updates+drops", test_edit_pdf_metadata_applies_updates_and_drops),
        ("edit drops win over updates", test_edit_pdf_metadata_drops_win_over_updates),
        ("edit single ModDate stamp", test_edit_pdf_metadata_single_moddate_stamp),
        ("edit no mod_date when disabled", test_edit_pdf_metadata_no_mod_date_when_disabled),
        ("edit caller ModDate wins", test_edit_pdf_metadata_caller_moddate_wins),
        ("edit drops ModDate cleanly", test_edit_pdf_metadata_drops_moddate),
        ("edit inplace", test_edit_pdf_metadata_inplace),
    ]
    inspect_tests = [
        ("parse_pdf_date", lambda _t: test_parse_pdf_date()),
        ("read_pdf_summary", test_read_pdf_summary),
        ("read_pdf_revisions clean", test_read_pdf_revisions_clean),
        ("read_pdf_revisions junk", test_read_pdf_revisions_with_appended_junk),
        ("read_pdf_dynamic clean", test_read_pdf_dynamic_clean),
    ]
    checker_tests = [
        ("check_pdf clean", test_check_pdf_clean),
        ("check_pdf mod before creation", test_check_pdf_mod_before_creation),
        ("check_pdf creation in future", test_check_pdf_creation_in_future),
        ("check_pdf junk after EOF", test_check_pdf_junk_after_eof),
        ("check_pdf to_dict serializable", test_check_pdf_to_dict_serializable),
    ]
    cli_tests = [
        ("CLI edit set+unset", test_cli_edit_set_and_unset),
        ("CLI edit inplace", test_cli_edit_inplace),
        ("CLI edit rejects inplace+output", test_cli_edit_rejects_inplace_with_output),
        ("CLI edit requires action", test_cli_edit_requires_action),
        ("CLI check clean exit 0", test_cli_check_clean_exits_zero),
        ("CLI check tampered exit 2", test_cli_check_tampered_exits_two),
        ("CLI check --json", test_cli_check_json),
    ]

    all_tests = metadata_tests + inspect_tests + checker_tests + cli_tests
    passed = failed = 0
    with tempfile.TemporaryDirectory() as d:
        for i, (name, fn) in enumerate(all_tests):
            sub = Path(d) / f"t{i:02d}"
            sub.mkdir()
            try:
                fn(sub)
                passed += 1
            except Exception as e:
                print(f"  [FAIL] {name}: {type(e).__name__}: {e}")
                failed += 1

    print("\n" + "=" * 60)
    print(f"RESULTS: {passed} passed, {failed} failed")
    if failed == 0:
        print("ALL TESTS PASSED!")
    print("=" * 60)

    # Non-zero exit on any failure so CI catches regressions instead of
    # reading the "X failed" line and still calling it a green build.
    import sys
    sys.exit(1 if failed else 0)
