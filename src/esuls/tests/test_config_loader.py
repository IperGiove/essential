"""Tests for the OmegaConf config loader (`esuls.utils.load_config`).

omegaconf is an *optional* dependency (the loader imports it lazily), so the
whole module skips cleanly when it isn't installed.

The core guarantee under test: `*.local.yaml` files override the committed
`config.yaml` on shared keys — the precedence fixed in 0.2.5.
"""
from pathlib import Path

import pytest

pytest.importorskip("omegaconf")

from esuls.utils import _yaml_sources, load_config


def _write(config_dir: Path, name: str, body: str) -> None:
    (config_dir / name).write_text(body, encoding="utf-8")


def test_local_yaml_overrides_committed_defaults(tmp_path: Path):
    # Committed defaults...
    _write(tmp_path, "config.yaml", "SITE:\n  NAME: base\n  DOMAIN: localhost\n")
    # ...and the gitignored local file overriding one of them.
    _write(tmp_path, "config.local.yaml", "SITE:\n  DOMAIN: example.com\n")

    cfg = load_config(tmp_path)

    # Local WINS on the shared key...
    assert cfg.SITE.DOMAIN == "example.com"
    # ...while committed keys the local file doesn't touch survive the merge.
    assert cfg.SITE.NAME == "base"


def test_local_example_stub_never_contributes_values(tmp_path: Path):
    _write(tmp_path, "config.yaml", "SITE:\n  DOMAIN: localhost\n")
    # A shape-only *.local.example.yaml stub must be excluded from the merge.
    _write(tmp_path, "config.local.example.yaml", "SITE:\n  DOMAIN: SHOULD_NOT_WIN\n")

    cfg = load_config(tmp_path)

    assert cfg.SITE.DOMAIN == "localhost"


def test_yaml_sources_orders_local_last_despite_alphabetical_name(tmp_path: Path):
    # Names picked so a plain alphabetical sort would place the local file
    # FIRST (the pre-0.2.5 bug): 'config.local.yaml' < 'config.yaml'. The
    # precedence key must still order it last, and drop the example stub.
    for name in ("config.yaml", "config.local.yaml", "config.local.example.yaml"):
        _write(tmp_path, name, "K: v\n")

    names = [p.name for p in _yaml_sources(tmp_path)]

    assert names == ["config.yaml", "config.local.yaml"]


def test_non_local_files_keep_alphabetical_order(tmp_path: Path):
    # Multiple committed files stay alphabetical among themselves, ahead of
    # any local file.
    for name in ("database.yaml", "config.yaml", "config.local.yaml"):
        _write(tmp_path, name, "K: v\n")

    names = [p.name for p in _yaml_sources(tmp_path)]

    assert names == ["config.yaml", "database.yaml", "config.local.yaml"]
