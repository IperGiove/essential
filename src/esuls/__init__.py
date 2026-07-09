"""
esuls - Utility library for async database operations, HTTP requests, and parallel execution.

The public API is a LAZY facade (PEP 562): the lightweight core — async helpers,
config loader, and the SQLAlchemy-backed DB layer — is imported eagerly, while
the heavier OPTIONAL surfaces (HTTP/scraping via httpx/curl_cffi/playwright, PDF
tooling, icon download) load only on first access. So ``import esuls`` (or
``from esuls import AsyncDB``) never drags in the ~170 MB scraping stack, and a
consumer installs only the features it uses::

    pip install esuls              # core: DB + async utils
    pip install 'esuls[config]'    # + OmegaConf config loader
    pip install 'esuls[http]'      # + make_request (httpx)
    pip install 'esuls[scraping]'  # + curl_cffi / playwright transports
    pip install 'esuls[pdf]'       # + PDF metadata/inspection + esuls-pdf CLI
    pip install 'esuls[icons]'     # + download_icon
    pip install 'esuls[all]'       # everything (the pre-0.4 default)

Accessing a feature whose extra isn't installed raises a clear ImportError that
names the extra to install.
"""
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    __version__ = _pkg_version("esuls")
except PackageNotFoundError:
    # Source checkout without an installed distribution (e.g. running from
    # a clone before `pip install -e .`). Keep imports working.
    __version__ = "0.0.0+unknown"

# ── eager core (no heavy third-party deps: stdlib + sqlalchemy/loguru) ────────
from .utils import run_parallel, load_config, generate_example_files
from .db_cli import (
    AsyncDB, BaseModel, IdModel, IntIdModel, TimestampedIntModel,
    TimestampedModel, discover_migrations, utcnow,
)

# ── lazy surfaces: public name -> (submodule, extra that provides it) ─────────
# __getattr__ imports the submodule on first access; if its optional dependency
# is missing, the ModuleNotFoundError is rewritten to name the extra to install.
# Adding a feature = one row here + one extra in pyproject.toml.
_LAZY = {
    # HTTP client (httpx transport)
    "AsyncRequest":            ("request_cli", "http"),
    "make_request":            ("request_cli", "http"),
    "Response":                ("request_cli", "http"),
    # Browser-grade scraping (curl_cffi / playwright — resolved at call time)
    "make_request_cffi":       ("request_cli", "scraping"),
    "make_request_playwright": ("request_cli", "scraping"),
    # Icon/avatar download + validation
    "download_icon":           ("download_icon", "icons"),
    # PDF metadata / inspection / legitimacy checking
    "Finding":                    ("pdf", "pdf"),
    "PdfReport":                  ("pdf", "pdf"),
    "STANDARD_METADATA_KEYS":     ("pdf", "pdf"),
    "check_metadata_coherence":   ("pdf", "pdf"),
    "check_pdf":                  ("pdf", "pdf"),
    "edit_pdf_metadata":          ("pdf", "pdf"),
    "find_non_standard_metadata": ("pdf", "pdf"),
    "read_pdf_dynamic":           ("pdf", "pdf"),
    "read_pdf_metadata":          ("pdf", "pdf"),
    "read_pdf_revisions":         ("pdf", "pdf"),
    "read_pdf_summary":           ("pdf", "pdf"),
    "read_pdf_xmp":               ("pdf", "pdf"),
    "remove_pdf_metadata":        ("pdf", "pdf"),
    "replace_pdf_metadata":       ("pdf", "pdf"),
    "update_pdf_metadata":        ("pdf", "pdf"),
}


def __getattr__(name: str):
    """PEP 562 lazy loader for the optional-feature surfaces."""
    try:
        module, extra = _LAZY[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    try:
        mod = import_module(f".{module}", __name__)
    except ModuleNotFoundError as e:
        # The submodule imports fine unless its optional dependency is absent —
        # turn the cryptic 'No module named X' into an actionable hint.
        raise ModuleNotFoundError(
            f"esuls.{name} requires the optional '{extra}' feature — install it "
            f"with `pip install 'esuls[{extra}]'`."
        ) from e
    value = getattr(mod, name)
    globals()[name] = value          # cache: subsequent access skips __getattr__
    return value


def __dir__():
    return sorted(__all__)


__all__ = [
    '__version__',
    # core — async + config
    'run_parallel', 'load_config', 'generate_example_files',
    # core — db
    'AsyncDB', 'BaseModel', 'IdModel', 'IntIdModel', 'TimestampedModel',
    'TimestampedIntModel', 'discover_migrations', 'utcnow',
    # http / scraping (lazy)
    'AsyncRequest', 'make_request', 'make_request_cffi', 'make_request_playwright', 'Response',
    # icons (lazy)
    'download_icon',
    # pdf (lazy)
    'read_pdf_metadata', 'read_pdf_xmp', 'read_pdf_summary', 'read_pdf_revisions',
    'read_pdf_dynamic', 'check_metadata_coherence', 'check_pdf', 'Finding', 'PdfReport',
    'find_non_standard_metadata', 'STANDARD_METADATA_KEYS', 'edit_pdf_metadata',
    'update_pdf_metadata', 'replace_pdf_metadata', 'remove_pdf_metadata',
]
