"""
Shared pytest fixtures for the esuls test suite.

`asyncio_mode = "auto"` (in pyproject.toml) auto-marks `async def test_*`
functions, so no per-function decorators are needed. Function-scoped
event loops match the per-loop registry semantics relied upon by
`test_cross_loop.py` and the cross-loop assertions in `test_db_advanced.py`.
"""
from pathlib import Path

import pytest
import pytest_asyncio

from esuls.db_cli import AsyncDB
from esuls.tests._common import TestItem


@pytest.fixture
def temp_db(tmp_path: Path) -> Path:
    """Per-test isolated SQLite path. `tmp_path` is pytest's built-in."""
    return tmp_path / "test.db"


@pytest_asyncio.fixture
async def db(temp_db: Path):
    """An AsyncDB bound to a TestItem schema, closed automatically on teardown."""
    instance = AsyncDB(temp_db, "items", TestItem)
    try:
        yield instance
    finally:
        await instance.close()
