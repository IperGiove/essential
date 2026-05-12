"""
esuls - Utility library for async database operations, HTTP requests, and parallel execution
"""
from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    __version__ = _pkg_version("esuls")
except PackageNotFoundError:
    # Source checkout without an installed distribution (e.g. running from
    # a clone before `pip install -e .`). Keep imports working.
    __version__ = "0.0.0+unknown"

# Import all utilities
from .utils import run_parallel, load_config, generate_example_files
from .db_cli import AsyncDB, BaseModel
from .request_cli import AsyncRequest, make_request, make_request_cffi, make_request_playwright, Response
from .download_icon import download_icon
from .pdf import (
    Finding,
    PdfReport,
    STANDARD_METADATA_KEYS,
    check_metadata_coherence,
    check_pdf,
    edit_pdf_metadata,
    find_non_standard_metadata,
    read_pdf_dynamic,
    read_pdf_metadata,
    read_pdf_revisions,
    read_pdf_summary,
    read_pdf_xmp,
    remove_pdf_metadata,
    replace_pdf_metadata,
    update_pdf_metadata,
)


__all__ = [
    '__version__',
    'run_parallel',
    'load_config',
    'generate_example_files',
    'AsyncDB',
    'BaseModel',
    'AsyncRequest',
    'make_request',
    'make_request_cffi',
    'make_request_playwright',
    'Response',
    'download_icon',
    # pdf
    'read_pdf_metadata',
    'read_pdf_xmp',
    'read_pdf_summary',
    'read_pdf_revisions',
    'read_pdf_dynamic',
    'check_metadata_coherence',
    'check_pdf',
    'Finding',
    'PdfReport',
    'find_non_standard_metadata',
    'STANDARD_METADATA_KEYS',
    'edit_pdf_metadata',
    'update_pdf_metadata',
    'replace_pdf_metadata',
    'remove_pdf_metadata',
]
