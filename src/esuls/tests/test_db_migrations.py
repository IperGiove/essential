"""
Tests for the file-based migrations support (tier 2 — PR D).

Migration discovery, ordering validation, idempotent application, and
the leap-frog rule on fresh DBs are all covered here. A migration is a
plain Python module `NNN_description.py` exposing module-level
``version: int``, ``description: str``, ``async def upgrade(conn)``, and
optionally ``async def downgrade(conn)``.
"""
import asyncio
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import text

from esuls.db_cli import (
    AsyncDB, BaseModel, _discover_migrations, _engines_by_path, _initialized_dbs,
)


@dataclass
class _Row(BaseModel):
    name: str = ""


def _write_migration(
    migrations_dir: Path, version: int, body: str, *,
    description: str = "test migration",
) -> Path:
    """Helper: drop a migration file with the right shape."""
    migrations_dir.mkdir(parents=True, exist_ok=True)
    fp = migrations_dir / f"{version:03d}_test_v{version}.py"
    fp.write_text(
        f"from sqlalchemy import text\n"
        f"version = {version}\n"
        f"description = {description!r}\n"
        f"async def upgrade(conn):\n"
        f"{body}\n"
    )
    return fp


# ────────────────────────────────────────────────────────────────────────
# 1. Discovery / validation
# ────────────────────────────────────────────────────────────────────────

def test_discover_empty_directory(tmp_path):
    mdir = tmp_path / "migrations"
    mdir.mkdir()
    assert _discover_migrations(mdir) == []


def test_discover_returns_sorted_by_version(tmp_path):
    mdir = tmp_path / "migrations"
    _write_migration(mdir, 2, "    pass")
    _write_migration(mdir, 1, "    pass")
    _write_migration(mdir, 3, "    pass")
    migs = _discover_migrations(mdir)
    assert [m.version for m in migs] == [1, 2, 3]


def test_discover_rejects_gaps(tmp_path):
    mdir = tmp_path / "migrations"
    _write_migration(mdir, 1, "    pass")
    _write_migration(mdir, 3, "    pass")  # gap: 2 is missing
    with pytest.raises(ValueError, match="version gap"):
        _discover_migrations(mdir)


def test_discover_rejects_duplicates(tmp_path):
    mdir = tmp_path / "migrations"
    # Two different files claiming the same version.
    (mdir).mkdir()
    (mdir / "001_a.py").write_text(
        "version = 1\ndescription = 'a'\nasync def upgrade(conn): pass\n"
    )
    (mdir / "001_b.py").write_text(
        "version = 1\ndescription = 'b'\nasync def upgrade(conn): pass\n"
    )
    with pytest.raises(ValueError, match="duplicate migration version"):
        _discover_migrations(mdir)


def test_discover_rejects_must_start_at_1(tmp_path):
    mdir = tmp_path / "migrations"
    _write_migration(mdir, 2, "    pass")
    with pytest.raises(ValueError, match="must start at version 1"):
        _discover_migrations(mdir)


def test_discover_rejects_missing_upgrade(tmp_path):
    mdir = tmp_path / "migrations"
    mdir.mkdir()
    (mdir / "001_bad.py").write_text("version = 1\ndescription = 'x'\n")
    with pytest.raises(ValueError, match="missing .* upgrade"):
        _discover_migrations(mdir)


def test_discover_ignores_non_matching_files(tmp_path):
    """Files not matching `NNN_*.py` are silently ignored."""
    mdir = tmp_path / "migrations"
    mdir.mkdir()
    _write_migration(mdir, 1, "    pass")
    # These are NOT migrations and should be ignored:
    (mdir / "README.md").write_text("docs")
    (mdir / "helpers.py").write_text("def x(): pass\n")  # no NNN_ prefix
    (mdir / "__init__.py").write_text("")
    migs = _discover_migrations(mdir)
    assert [m.version for m in migs] == [1]


# ────────────────────────────────────────────────────────────────────────
# 2. Application: fresh DB leap-frogs user_version
# ────────────────────────────────────────────────────────────────────────

async def test_fresh_db_leapfrogs_user_version(tmp_path):
    """A fresh DB sets user_version to the max migration but doesn't run them.

    The dataclass-driven `create_all` is already at the post-migration
    schema, so re-running historical migrations would either fail or
    duplicate work.
    """
    db_path = tmp_path / "app.db"
    mdir = tmp_path / "migrations"
    _write_migration(mdir, 1, "    raise RuntimeError('should not run on fresh DB')")
    _write_migration(mdir, 2, "    raise RuntimeError('should not run on fresh DB')")

    db = AsyncDB(db_path, "items", _Row)
    try:
        await db.save(_Row(name="x"))
        async with db.transaction(read_only=True) as conn:
            uv = (await conn.execute(text("PRAGMA user_version"))).scalar()
        assert uv == 2, f"expected user_version=2, got {uv}"
    finally:
        await db.close()


# ────────────────────────────────────────────────────────────────────────
# 3. Application: pending migrations run on an existing DB
# ────────────────────────────────────────────────────────────────────────

async def test_pending_migrations_applied(tmp_path):
    """Adding a new migration after first start runs it next time."""
    db_path = tmp_path / "app.db"
    mdir = tmp_path / "migrations"

    # Phase 1: empty migrations dir, fresh DB → user_version=0.
    mdir.mkdir()
    db = AsyncDB(db_path, "items", _Row)
    try:
        await db.save(_Row(name="seed"))
        async with db.transaction(read_only=True) as conn:
            assert (await conn.execute(text("PRAGMA user_version"))).scalar() == 0
    finally:
        await db.close()

    # Phase 2: add a migration that creates a side table. Re-opening
    # should run it (because the DB already exists, no leap-frog).
    _write_migration(mdir, 1,
                     "    await conn.execute(text(\"CREATE TABLE side ("
                     "k TEXT PRIMARY KEY, v TEXT)\"))\n"
                     "    await conn.execute(text(\"INSERT INTO side VALUES ("
                     "'hello', 'world')\"))",
                     description="create side table")

    # Force schema re-init in this loop.
    _initialized_dbs.clear()

    db2 = AsyncDB(db_path, "items", _Row)
    try:
        async with db2.transaction(read_only=True) as conn:
            assert (await conn.execute(text("PRAGMA user_version"))).scalar() == 1
            row = (await conn.execute(
                text("SELECT v FROM side WHERE k='hello'")
            )).scalar()
            assert row == "world"
    finally:
        await db2.close()


async def test_already_applied_migrations_are_skipped(tmp_path):
    """Migrations with version <= user_version are skipped on subsequent opens."""
    db_path = tmp_path / "app.db"
    mdir = tmp_path / "migrations"
    _write_migration(mdir, 1,
                     "    await conn.execute(text(\"CREATE TABLE m1 (id TEXT)\"))",
                     description="create m1")

    # Fresh DB leap-frogs to v=1; m1 is NOT created by the migration.
    db = AsyncDB(db_path, "items", _Row)
    try:
        await db.save(_Row(name="x"))
    finally:
        await db.close()

    # Re-open: user_version is already 1, migration must not re-run.
    # We prove non-execution by adding a *failing* migration at the same
    # version — if it ran, this would raise.
    (mdir / "001_test_v1.py").write_text(
        "from sqlalchemy import text\n"
        "version = 1\n"
        "description = 'must not run'\n"
        "async def upgrade(conn):\n"
        "    raise RuntimeError('this migration was already applied!')\n"
    )

    _initialized_dbs.clear()

    db2 = AsyncDB(db_path, "items", _Row)
    try:
        # Just opening + reading something forces _ensure_engines + migration
        # check. The mutated v1 must NOT be re-executed.
        assert await db2.count() == 1
    finally:
        await db2.close()


# ────────────────────────────────────────────────────────────────────────
# 4. Atomicity: a failing migration rolls back the whole init
# ────────────────────────────────────────────────────────────────────────

async def test_failing_migration_rolls_back(tmp_path):
    """A migration that raises rolls back its DDL + leaves user_version unchanged."""
    db_path = tmp_path / "app.db"
    mdir = tmp_path / "migrations"

    # Bootstrap with user_version=0 (no migrations on fresh DB).
    db = AsyncDB(db_path, "items", _Row)
    try:
        await db.save(_Row(name="seed"))
    finally:
        await db.close()

    # Add a migration that partially writes, then fails.
    _write_migration(mdir, 1,
                     "    await conn.execute(text(\"CREATE TABLE attempted (x INT)\"))\n"
                     "    raise RuntimeError('boom')",
                     description="boom")

    _initialized_dbs.clear()

    db2 = AsyncDB(db_path, "items", _Row)
    try:
        with pytest.raises(RuntimeError, match="boom"):
            await db2.count()  # forces _ensure_engines → migrations → failure
    finally:
        try:
            await db2.close()
        except Exception:
            pass

    # Verify rollback: user_version still 0, `attempted` table not present,
    # seed row still there.
    _initialized_dbs.clear()

    # Remove the failing migration so the next open succeeds.
    (mdir / "001_test_v1.py").unlink()

    db3 = AsyncDB(db_path, "items", _Row)
    try:
        async with db3.transaction(read_only=True) as conn:
            assert (await conn.execute(text("PRAGMA user_version"))).scalar() == 0
            tables = [r[0] for r in (await conn.execute(text(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ))).fetchall()]
            assert "attempted" not in tables
        assert await db3.count() == 1  # seed survived
    finally:
        await db3.close()


# ────────────────────────────────────────────────────────────────────────
# 5. No-op when there's no migrations/ directory
# ────────────────────────────────────────────────────────────────────────

async def test_no_migrations_dir_is_noop(tmp_path):
    """Without `migrations/` the system behaves exactly as pre-PR-D."""
    db_path = tmp_path / "app.db"
    # No migrations/ directory created.
    db = AsyncDB(db_path, "items", _Row)
    try:
        await db.save(_Row(name="x"))
        async with db.transaction(read_only=True) as conn:
            assert (await conn.execute(text("PRAGMA user_version"))).scalar() == 0
    finally:
        await db.close()
