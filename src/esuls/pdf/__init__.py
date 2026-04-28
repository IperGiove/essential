"""PDF metadata, inspection and legitimacy checking.

Three layers:
- `metadata`: read/write Info dict and XMP
- `inspect`:  pure introspection (summary, revisions, dynamic content)
- `checker`:  high-level legitimacy verdict (Finding, PdfReport, check_pdf)

The `cli` module wires them into an `esuls-pdf` console script with three
subcommands: `check`, `inspect`, `edit`.
"""

from .checker import (
    Finding,
    PdfReport,
    Severity,
    Verdict,
    check_metadata_coherence,
    check_pdf,
)
from .inspect import (
    parse_pdf_date,
    read_pdf_dynamic,
    read_pdf_revisions,
    read_pdf_summary,
)
from .metadata import (
    STANDARD_METADATA_KEYS,
    find_non_standard_metadata,
    read_pdf_metadata,
    read_pdf_xmp,
    remove_pdf_metadata,
    replace_pdf_metadata,
    update_pdf_metadata,
)

__all__ = [
    # metadata
    "STANDARD_METADATA_KEYS",
    "find_non_standard_metadata",
    "read_pdf_metadata",
    "read_pdf_xmp",
    "remove_pdf_metadata",
    "replace_pdf_metadata",
    "update_pdf_metadata",
    # inspect
    "parse_pdf_date",
    "read_pdf_dynamic",
    "read_pdf_revisions",
    "read_pdf_summary",
    # checker
    "Finding",
    "PdfReport",
    "Severity",
    "Verdict",
    "check_metadata_coherence",
    "check_pdf",
]
