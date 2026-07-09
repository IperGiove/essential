"""Guard the lazy-facade / per-feature-extra contract introduced in 0.4.0.

The whole point of splitting the heavy transports (playwright/curl_cffi/…) into
optional extras is that `import esuls` — the DB/config path used by SSR apps —
stays light and the compiled (Nuitka) binary never bundles the ~170 MB scraping
stack. These tests fail loudly if a future edit reintroduces an eager import of
an optional dependency at the top of the package.

The core-import check runs in a SUBPROCESS so it observes a clean interpreter
with an empty sys.modules, not one already populated by the rest of the suite.
"""
import subprocess
import sys
import textwrap

import esuls


def _run(code: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        capture_output=True,
        text=True,
    )


def test_import_esuls_does_not_pull_the_heavy_stack():
    """`import esuls` must not eagerly import any optional transport, even when
    the extras ARE installed (as they are under `--all-extras` in CI)."""
    proc = _run(
        """
        import sys
        import esuls

        # eager core is present without touching any optional surface
        assert esuls.AsyncDB is not None
        assert esuls.run_parallel is not None

        leaked = [m for m in ("playwright", "playwright_stealth", "curl_cffi", "pypdf")
                  if m in sys.modules]
        assert not leaked, f"import esuls eagerly pulled optional deps: {leaked}"
        print("OK")
        """
    )
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout


def test_core_symbols_are_importable_from_the_top_level():
    # Eager core (db_cli + utils) — no optional dependency required.
    from esuls import (  # noqa: F401
        AsyncDB, BaseModel, IntIdModel, TimestampedModel,
        discover_migrations, generate_example_files, load_config,
        run_parallel, utcnow,
    )


def test_lazy_symbols_resolve_when_their_extra_is_installed():
    # Under `--all-extras` every optional dep is present, so accessing a lazy
    # symbol must transparently resolve through __getattr__.
    assert esuls.make_request is not None            # http
    assert esuls.make_request_playwright is not None  # scraping
    assert esuls.read_pdf_metadata is not None        # pdf
    assert esuls.download_icon is not None            # icons


def test_lazy_access_caches_on_the_module():
    # First access goes through __getattr__ and writes the value into globals();
    # the attribute must then exist as a plain module global.
    _ = esuls.make_request
    assert "make_request" in vars(esuls)


def test_unknown_attribute_raises_attribute_error():
    try:
        esuls.does_not_exist  # noqa: B018
    except AttributeError:
        pass
    else:
        raise AssertionError("expected AttributeError for an unknown attribute")


def test_public_api_is_advertised_in_dir():
    listing = dir(esuls)
    for name in ("AsyncDB", "make_request", "read_pdf_metadata", "download_icon"):
        assert name in listing
