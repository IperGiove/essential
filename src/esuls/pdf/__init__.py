"""PDF metadata, inspection and legitimacy checking.

Three layers:
- `metadata`: read/write Info dict and XMP
- `inspect`:  pure introspection (summary, revisions, dynamic content)
- `checker`:  high-level legitimacy verdict (Finding, PdfReport, check_pdf)
- `content`:  page fonts/images inventory and what each revision added
- `layout`:   where images land on the page and which text they paint over
- `signatures`: cryptographic signature validation via pyHanko (the
  optional `pdf-sign` extra; imported lazily, so this package works without it)

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
from .content import (
    STANDARD_14_FONTS,
    read_pdf_content,
    read_pdf_fonts,
    read_pdf_images,
    read_pdf_revision_content,
)
from .layout import read_pdf_image_placements
from .inspect import (
    parse_pdf_date,
    read_pdf_dynamic,
    read_pdf_revisions,
    read_pdf_summary,
)
from .signatures import (
    SignatureCheck,
    async_verify_pdf_signatures,
    verify_pdf_signatures,
)
from .metadata import (
    STANDARD_METADATA_KEYS,
    edit_pdf_metadata,
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
    "edit_pdf_metadata",
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
    # content
    "STANDARD_14_FONTS",
    "read_pdf_content",
    "read_pdf_fonts",
    "read_pdf_images",
    "read_pdf_revision_content",
    "read_pdf_image_placements",
    # checker
    "Finding",
    "PdfReport",
    "Severity",
    "Verdict",
    "check_metadata_coherence",
    "check_pdf",
    # signatures (needs the `pdf-sign` extra at call time)
    "SignatureCheck",
    "async_verify_pdf_signatures",
    "verify_pdf_signatures",
]
