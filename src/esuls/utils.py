"""
General utilities — no external dependencies at import time.

The omegaconf-based helpers (load_config, generate_example_files) lazy-
import their dependency, so this module stays light for users that only
need run_parallel.
"""
import asyncio
from pathlib import Path
from typing import Awaitable, List, TypeVar

T = TypeVar("T")


async def run_parallel(
    *coroutines: Awaitable[T],
    limit: int = 20,
) -> List[T]:
    """Run parallel coroutines with semaphore limit, preserving order"""

    semaphore = asyncio.Semaphore(limit)

    async def limited_coroutine(coro: Awaitable[T]) -> T:
        async with semaphore:
            return await coro

    return await asyncio.gather(*[limited_coroutine(coro) for coro in coroutines])


# ---------------------------------------------------------------------------
# OmegaConf-based config loader.
#
# omegaconf is intentionally NOT declared in esuls' install_requires so that
# consumers who only use the async / HTTP / DB / PDF utilities don't pull it
# in transitively. The two functions below import it lazily and raise a clear
# ImportError if it isn't available.
#
# This loader is meant for projects that:
#   1. keep one or more `config/` directories with *.yaml files
#   2. want to merge those YAMLs into a single OmegaConf DictConfig at boot
#   3. optionally commit *.local.example.yaml stubs (shape-only, no secrets)
#      alongside developer-local *.local.yaml so the team can read the
#      config schema without leaking real values.
# ---------------------------------------------------------------------------


def _yaml_sources(config_dir: Path) -> "list[Path]":
    """Return the *.yaml files in `config_dir` that should be merged into
    the runtime config. Excludes *.local.example.yaml — those describe
    the SHAPE of a *.local.yaml, not real values."""
    return sorted(
        p for p in config_dir.glob("*.yaml")
        if "local.example" not in p.stem
    )


def load_config(config_dir: Path):
    """Merge all *.yaml files in `config_dir` into a single OmegaConf
    DictConfig. Excludes *.local.example.yaml stubs.

    Sets ``cfg.path`` to the parent of `config_dir` (the package root
    that owns the config) so callers can resolve relative paths.

    Raises:
        ImportError: if omegaconf isn't installed. The message includes
            the install hint (``pip install omegaconf``).
        OmegaConfBaseException: re-raised as-is if any YAML is malformed
            — at boot you want this to fail loudly, not silently.

    Example::

        from pathlib import Path
        from esuls.utils import load_config

        CONFIG_DIR = Path(__file__).resolve().parent
        cfg = load_config(CONFIG_DIR)
    """
    try:
        from omegaconf import OmegaConf
    except ImportError as e:
        raise ImportError(
            "esuls.utils.load_config requires omegaconf "
            "(`pip install omegaconf`)."
        ) from e

    cfg = OmegaConf.merge(*[OmegaConf.load(p) for p in _yaml_sources(config_dir)])
    cfg.path = config_dir.parent
    return cfg


def generate_example_files(config_dir: Path) -> None:
    """Write *.local.example.yaml stubs alongside every *.local.yaml in
    `config_dir`. Each stub mirrors the source's nested structure but
    replaces leaf values with ``str(type(value))``, producing a "shape"
    file safe to commit (no real secrets).

    This is a **developer aid**, not a runtime requirement. It is
    deliberately tolerant:

    * If the directory isn't writable (production container with a
      non-root user that owns only specific paths, read-only mount,
      etc.), the OSError is caught and the file is silently skipped.
      Logged at debug level via loguru if available, stdlib logging
      otherwise.
    * If omegaconf isn't installed, raises ImportError with the hint.

    The example files are expected to be committed alongside the source
    so production never needs to (re)generate them — running this at
    boot in dev keeps the stubs in sync as the *.local.yaml shape
    evolves; running it in prod is a no-op.
    """
    try:
        from omegaconf import DictConfig, OmegaConf
    except ImportError as e:
        raise ImportError(
            "esuls.utils.generate_example_files requires omegaconf "
            "(`pip install omegaconf`)."
        ) from e

    try:
        from loguru import logger
    except ImportError:
        import logging
        logger = logging.getLogger(__name__)

    def _rewrite_leaves(data):
        """Replace each leaf value with its type name string. Recursive
        on DictConfig nodes; leaves are anything else."""
        if isinstance(data, DictConfig):
            for key in data:
                data[key] = _rewrite_leaves(data[key])
            return data
        return f"{type(data)}"

    for yaml_file in _yaml_sources(config_dir):
        if not yaml_file.name.endswith(".local.yaml"):
            continue
        example_file = yaml_file.with_name(yaml_file.stem + ".example.yaml")
        try:
            example_cfg = _rewrite_leaves(OmegaConf.load(yaml_file))
            OmegaConf.save(example_cfg, example_file)
        except OSError as e:
            logger.debug(
                f"generate_example_files: skipping {yaml_file.name} ({e})"
            )
