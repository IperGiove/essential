"""Tests for index retrofit on an ALREADY-EXISTING table.

SQLAlchemy's `metadata.create_all(checkfirst=True)` emits nothing for a table it
did not create — indexes included. So adding `metadata={"index": True}` or
`{"unique": True}` to a dataclass whose table already existed used to add the
column and silently never enforce the constraint: the declaration read as
enforced while duplicates kept being accepted. `_ensure_indexes` closes that.
"""
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from esuls.db_cli import AsyncDB, IdModel


@dataclass
class Plain(IdModel):
    """v1 of the schema — no index declared."""
    email: Optional[str] = field(default=None)


@dataclass
class Indexed(IdModel):
    """v2 — the same table, now declaring index + unique on `email`."""
    email: Optional[str] = field(default=None, metadata={"index": True, "unique": True})


def _indexes(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='t'"
            )
        }
    finally:
        conn.close()


async def _make_v1(path: Path, *, rows: list[tuple[str, str]]):
    db = AsyncDB(path, "t", Plain)
    for rid, email in rows:
        await db.save(Plain(id=rid, email=email))
    await db.close()


async def test_declared_indexes_are_created_on_an_existing_table(temp_db: Path):
    await _make_v1(temp_db, rows=[("a", "x@y.z")])

    db = AsyncDB(temp_db, "t", Indexed)
    await db.get_by_id("a")  # triggers schema init against the existing table
    try:
        assert {"idx_t_email", "idx_t_email_unique"} <= _indexes(temp_db)
    finally:
        await db.close()


async def test_unique_is_actually_enforced_after_the_retrofit(temp_db: Path):
    """The point of the whole change: the declaration must bind. `save()`
    swallows the IntegrityError and skips the row (its documented behaviour), so
    enforcement is observed by the duplicate NOT landing — before this fix it
    was inserted happily."""
    await _make_v1(temp_db, rows=[("a", "x@y.z")])

    db = AsyncDB(temp_db, "t", Indexed)
    await db.get_by_id("a")
    try:
        await db.save(Indexed(id="b", email="x@y.z"))
        assert await db.get_by_id("b") is None
        assert len(await db.find(email="x@y.z")) == 1
    finally:
        await db.close()


async def test_existing_duplicates_skip_the_unique_index_instead_of_failing_boot(temp_db: Path):
    """A table that accumulated duplicates BECAUSE the constraint was missing
    must not make schema init — i.e. startup — raise. Deduplicating is a human
    decision; the non-unique indexes still get created."""
    await _make_v1(temp_db, rows=[("a", "x@y.z"), ("b", "x@y.z")])

    db = AsyncDB(temp_db, "t", Indexed)
    await db.get_by_id("a")  # must not raise
    try:
        idx = _indexes(temp_db)
        assert "idx_t_email" in idx           # plain index still created
        assert "idx_t_email_unique" not in idx  # unique skipped, loudly logged
        assert len(await db.find(email="x@y.z")) == 2
    finally:
        await db.close()


async def test_many_nulls_do_not_count_as_duplicates(temp_db: Path):
    """NULLs are distinct in SQL, so a UNIQUE index tolerates any number of them
    — that is what makes an optional-but-unique column possible at all. Counting
    them as a conflict would refuse an index SQLite builds happily, and it is
    the COMMON case: such a column is mostly NULL before the feature ships."""
    await _make_v1(temp_db, rows=[("a", None), ("b", None), ("c", "x@y.z")])

    db = AsyncDB(temp_db, "t", Indexed)
    await db.get_by_id("a")
    try:
        assert "idx_t_email_unique" in _indexes(temp_db)
        # ...and the index still permits further NULLs afterwards.
        await db.save(Indexed(id="d", email=None))
        assert await db.get_by_id("d") is not None
    finally:
        await db.close()


async def test_unique_together_is_NOT_retrofitted_onto_an_existing_table(temp_db: Path):
    """Documents a REMAINING gap, so nobody assumes index retrofit covers it.

    `__unique_together__` compiles to a table-level `UniqueConstraint`, not an
    `Index`, and SQLite cannot ALTER-add a table constraint — so on a
    pre-existing table it stays unenforced, exactly as before. Adding one to a
    live model still needs a migration (or a hand-written composite
    `CREATE UNIQUE INDEX`). Only per-field `metadata={"unique": True}` is
    retrofitted.
    """
    @dataclass
    class Pair(IdModel):
        a: Optional[str] = field(default=None)
        b: Optional[str] = field(default=None)

    @dataclass
    class PairUnique(IdModel):
        __unique_together__ = (("a", "b"),)
        a: Optional[str] = field(default=None)
        b: Optional[str] = field(default=None)

    assert AsyncDB(temp_db, "t", PairUnique)._table.indexes == set()

    db1 = AsyncDB(temp_db, "t", Pair)
    await db1.save(Pair(id="x", a="same", b="same"))
    await db1.close()

    db2 = AsyncDB(temp_db, "t", PairUnique)
    await db2.get_by_id("x")  # must not raise
    try:
        await db2.save(PairUnique(id="y", a="same", b="same"))
        assert len(await db2.find()) == 2  # still accepted — the gap
    finally:
        await db2.close()


async def test_retrofit_is_idempotent_across_reopens(temp_db: Path):
    await _make_v1(temp_db, rows=[("a", "x@y.z")])
    for _ in range(3):
        db = AsyncDB(temp_db, "t", Indexed)
        await db.get_by_id("a")
        await db.close()
    assert {"idx_t_email", "idx_t_email_unique"} <= _indexes(temp_db)


async def test_a_freshly_created_table_still_gets_its_indexes(temp_db: Path):
    """The create_all path (new table) must be unaffected by the change."""
    db = AsyncDB(temp_db, "t", Indexed)
    await db.save(Indexed(id="a", email="x@y.z"))
    try:
        assert {"idx_t_email", "idx_t_email_unique"} <= _indexes(temp_db)
    finally:
        await db.close()
