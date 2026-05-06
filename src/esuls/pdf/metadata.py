import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Set, Union

from pypdf import PdfReader, PdfWriter


STANDARD_METADATA_KEYS: Set[str] = {
    "/Title", "/Author", "/Subject", "/Creator", "/Producer",
    "/CreationDate", "/ModDate", "/Keywords",
}


_XMP_FIELDS = (
    # Dublin Core
    "dc_title", "dc_creator", "dc_subject", "dc_description",
    "dc_date", "dc_format", "dc_identifier", "dc_language",
    "dc_rights", "dc_publisher", "dc_contributor", "dc_source",
    "dc_relation", "dc_type", "dc_coverage",
    # PDF namespace
    "pdf_producer", "pdf_keywords", "pdf_pdfversion",
    # PDF/A
    "pdfaid_part", "pdfaid_conformance",
    # XMP basic
    "xmp_creator_tool", "xmp_create_date",
    "xmp_modify_date", "xmp_metadata_date",
    # XMP MM
    "xmpmm_document_id", "xmpmm_instance_id",
)


def read_pdf_metadata(pdf_path: Union[str, Path]) -> Optional[dict]:
    """Read the legacy Info dict. Returns dict[str, str] or None if absent."""
    reader = PdfReader(str(pdf_path))
    metadata = reader.metadata
    if metadata is None:
        return None
    return {key: str(value) for key, value in metadata.items()}


def find_non_standard_metadata(metadata: Optional[dict]) -> Set[str]:
    """Keys in `metadata` not in STANDARD_METADATA_KEYS."""
    if not metadata:
        return set()
    return set(metadata.keys()) - STANDARD_METADATA_KEYS


def read_pdf_xmp(pdf_path: Union[str, Path]) -> Optional[dict]:
    """
    Read the XMP metadata stream. Returns a dict of populated XMP fields,
    or None if the PDF has no XMP packet.
    """
    reader = PdfReader(str(pdf_path))
    xmp = reader.xmp_metadata
    if xmp is None:
        return None
    out = {}
    for name in _XMP_FIELDS:
        try:
            value = getattr(xmp, name, None)
        except Exception:
            value = None
        if value is None or value == [] or value == "" or value == {}:
            continue
        out[name] = value
    return out


def update_pdf_metadata(
    pdf_path: Union[str, Path],
    updates: dict,
    *,
    output_path: Union[str, Path, None] = None,
    inplace: bool = False,
    update_mod_date: bool = True,
    keep_xmp: bool = False,
) -> Path:
    """
    Merge `updates` into the existing Info dict. Existing keys not in
    `updates` are preserved. Returns the path of the written file.

    By default `/ModDate` is overwritten with the current UTC time (every
    edit bumps the modification timestamp) and the XMP stream is removed
    so the Info dict is the single source of truth. To keep the source
    `/ModDate`, pass `update_mod_date=False`; to preserve `/ModDate`
    exactly, pass it explicitly inside `updates`. Pass `keep_xmp=True` to
    preserve the original XMP packet (note: it may then disagree with the
    Info dict).
    """
    pdf_path = Path(pdf_path)
    out = _resolve_output(pdf_path, output_path, inplace)
    reader = PdfReader(str(pdf_path))

    norm_updates = _normalize_metadata(updates)
    final = {str(k): str(v) for k, v in (reader.metadata or {}).items()}
    final.update(norm_updates)
    if update_mod_date and "/ModDate" not in norm_updates:
        final["/ModDate"] = _pdf_now()

    return _write_with_metadata(
        reader, final,
        output_path=out, source_path=pdf_path, inplace=inplace,
        keep_xmp=keep_xmp,
    )


def replace_pdf_metadata(
    pdf_path: Union[str, Path],
    metadata: dict,
    *,
    output_path: Union[str, Path, None] = None,
    inplace: bool = False,
    update_mod_date: bool = True,
    keep_xmp: bool = False,
) -> Path:
    """Replace the entire Info dict with `metadata`. Same `/ModDate` and
    XMP defaults as `update_pdf_metadata`."""
    pdf_path = Path(pdf_path)
    out = _resolve_output(pdf_path, output_path, inplace)
    reader = PdfReader(str(pdf_path))

    final = _normalize_metadata(metadata)
    if update_mod_date and "/ModDate" not in final:
        final["/ModDate"] = _pdf_now()

    return _write_with_metadata(
        reader, final,
        output_path=out, source_path=pdf_path, inplace=inplace,
        keep_xmp=keep_xmp,
    )


def edit_pdf_metadata(
    pdf_path: Union[str, Path],
    updates: Optional[dict] = None,
    drops: Optional[Iterable[str]] = None,
    *,
    output_path: Union[str, Path, None] = None,
    inplace: bool = False,
    update_mod_date: bool = True,
    keep_xmp: bool = False,
) -> Path:
    """
    Apply `updates` and `drops` to the Info dict in a single write pass.

    Use this instead of chaining `update_pdf_metadata` + `remove_pdf_metadata`
    when you need to both set and unset keys: chaining them would write the
    PDF twice and stamp `/ModDate` twice (the second timestamp clobbering
    the first).

    `drops` wins over `updates` if the same key appears in both. `/ModDate`
    is auto-bumped to now unless it appears in `updates`, is in `drops`, or
    `update_mod_date=False`.
    """
    pdf_path = Path(pdf_path)
    out = _resolve_output(pdf_path, output_path, inplace)
    reader = PdfReader(str(pdf_path))

    norm_updates = _normalize_metadata(updates) if updates else {}
    to_drop = {_normalize_key(k) for k in drops} if drops else set()

    final = {str(k): str(v) for k, v in (reader.metadata or {}).items()}
    final.update(norm_updates)
    final = {k: v for k, v in final.items() if k not in to_drop}

    if (update_mod_date
            and "/ModDate" not in norm_updates
            and "/ModDate" not in to_drop):
        final["/ModDate"] = _pdf_now()

    return _write_with_metadata(
        reader, final,
        output_path=out, source_path=pdf_path, inplace=inplace,
        keep_xmp=keep_xmp,
    )


def remove_pdf_metadata(
    pdf_path: Union[str, Path],
    keys: Iterable[str],
    *,
    output_path: Union[str, Path, None] = None,
    inplace: bool = False,
    update_mod_date: bool = True,
    keep_xmp: bool = False,
) -> Path:
    """Remove the given keys from the Info dict (missing keys are no-ops).
    `/ModDate` is auto-bumped to now unless explicitly removed via `keys`
    or disabled via `update_mod_date=False`."""
    pdf_path = Path(pdf_path)
    out = _resolve_output(pdf_path, output_path, inplace)
    reader = PdfReader(str(pdf_path))

    existing = {str(k): str(v) for k, v in (reader.metadata or {}).items()}
    to_drop = {_normalize_key(k) for k in keys}
    final = {k: v for k, v in existing.items() if k not in to_drop}
    if update_mod_date and "/ModDate" not in to_drop:
        final["/ModDate"] = _pdf_now()

    return _write_with_metadata(
        reader, final,
        output_path=out, source_path=pdf_path, inplace=inplace,
        keep_xmp=keep_xmp,
    )


def _normalize_key(key: str) -> str:
    key = str(key)
    return key if key.startswith("/") else f"/{key}"


def _normalize_metadata(metadata: dict) -> dict:
    return {_normalize_key(k): str(v) for k, v in metadata.items()}


def _resolve_output(
    pdf_path: Path,
    output_path: Union[str, Path, None],
    inplace: bool,
) -> Path:
    if inplace and output_path is not None:
        raise ValueError("Pass either `output_path` or `inplace=True`, not both.")
    if inplace:
        return pdf_path
    if output_path is not None:
        return Path(output_path)
    return pdf_path.with_name(f"{pdf_path.stem}.metadata{pdf_path.suffix}")


def _pdf_now() -> str:
    return datetime.now(timezone.utc).strftime("D:%Y%m%d%H%M%S+00'00'")


def _write_with_metadata(
    reader: PdfReader,
    final_metadata: dict,
    *,
    output_path: Path,
    source_path: Path,
    inplace: bool,
    keep_xmp: bool = False,
) -> Path:
    writer = PdfWriter(clone_from=reader)
    writer.metadata = final_metadata

    if not keep_xmp:
        root = writer.root_object
        if "/Metadata" in root:
            del root["/Metadata"]

    if inplace:
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{source_path.name}.", suffix=".tmp", dir=str(source_path.parent)
        )
        os.close(fd)
        tmp_path = Path(tmp_name)
        try:
            with open(tmp_path, "wb") as fh:
                writer.write(fh)
            os.replace(tmp_path, output_path)
        except Exception:
            if tmp_path.exists():
                tmp_path.unlink()
            raise
    else:
        with open(output_path, "wb") as fh:
            writer.write(fh)

    return output_path
