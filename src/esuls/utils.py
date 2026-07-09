"""
General utilities — no external dependencies at import time.

The omegaconf-based helpers (load_config, generate_example_files) lazy-
import their dependency, so this module stays light for users that only
need run_parallel.
"""
import asyncio
from pathlib import Path
from typing import Awaitable, List, TypeVar, Union

T = TypeVar("T")


async def run_parallel(
    *coroutines: Awaitable[T],
    limit: int = 20,
    return_exceptions: bool = True,
) -> List[Union[T, BaseException]]:
    """Run parallel coroutines with a semaphore limit, preserving order.

    `return_exceptions` defaults to True so a single failure does not leave
    its siblings running as orphan tasks. With raw `asyncio.gather` and
    `return_exceptions=False`, the first raised exception propagates
    immediately to the caller but the other in-flight coroutines keep
    running in the background to natural completion — wasting CPU/IO and
    holding resources (sockets, file handles, DB cursors) the caller
    thinks were already torn down.

    Callers that need fail-fast propagation pass `return_exceptions=False`
    explicitly and accept the orphan-task trade-off. Callers that want the
    classic "wait for everyone, surface failures in the result" semantics
    iterate the returned list and check `isinstance(r, BaseException)`.
    """

    semaphore = asyncio.Semaphore(limit)

    async def limited_coroutine(coro: Awaitable[T]) -> T:
        async with semaphore:
            return await coro

    results = await asyncio.gather(
        *[limited_coroutine(coro) for coro in coroutines],
        return_exceptions=return_exceptions,
    )

    # `return_exceptions=True` captures EVERY BaseException, including
    # KeyboardInterrupt and SystemExit. Bundling those into the result
    # list would silently swallow Ctrl-C / sys.exit() and let the
    # process keep running. Re-raise them so they propagate as the
    # user expects; ordinary Exception subclasses (the actual point
    # of return_exceptions=True) stay in the list.
    if return_exceptions:
        for r in results:
            if isinstance(r, (KeyboardInterrupt, SystemExit)):
                raise r
    return results


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
    """Return the *.yaml files in `config_dir` in merge order (earlier files
    merged first, so later files win). Excludes *.local.example.yaml — those
    describe the SHAPE of a *.local.yaml, not real values.

    `*.local.yaml` files are ordered LAST so they merge last and therefore
    override the committed defaults: local values (secrets + per-environment
    overrides) win over the version-controlled `config.yaml`. Within each
    group — non-local first, then local — files keep alphabetical order.
    """
    return sorted(
        (p for p in config_dir.glob("*.yaml") if "local.example" not in p.stem),
        # False (non-local) sorts before True (local), so committed files are
        # merged first and *.local.yaml last — and last-merged wins in OmegaConf.
        key=lambda p: (p.name.endswith(".local.yaml"), p.name),
    )


def load_config(config_dir: Path):
    """Merge all *.yaml files in `config_dir` into a single OmegaConf
    DictConfig. Excludes *.local.example.yaml stubs.

    Precedence: ``*.local.yaml`` files are merged last and override the
    committed defaults — ``config.local.yaml`` beats ``config.yaml`` on any
    shared key. See :func:`_yaml_sources`.

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
            "esuls.utils.load_config requires omegaconf — install the config "
            "feature with `pip install 'esuls[config]'`."
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
            "esuls.utils.generate_example_files requires omegaconf — install the "
            "config feature with `pip install 'esuls[config]'`."
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
