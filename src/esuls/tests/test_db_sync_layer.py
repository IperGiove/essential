"""The guarantees that came with the synchronous execution layer.

Three properties, each one previously either broken or unstated:

* concurrent `transaction()` blocks serialise instead of sharing a connection;
* a bounded read runs inline while an unbounded one is off-loaded to a thread;
* a caller that still `await`s `conn.execute(...)` keeps working.
"""
import asyncio
import threading
from dataclasses import dataclass, field

import pytest
from sqlalchemy import text

from esuls.db_cli import AsyncDB, BaseModel


@dataclass
class _Row(BaseModel):
    name: str = field(default=None, metadata={"index": True})
    other: str = field(default=None)
    n: int = field(default=0)


@pytest.fixture
async def db(tmp_path):
    d = AsyncDB(tmp_path / "sync_layer.db", "rows", _Row)
    yield d
    await d.close()


# ---- transaction(): one writer at a time --------------------------------


async def test_concurrent_transactions_do_not_share_a_connection(db):
    """The bug this layer was built to close.

    Two overlapping write transactions used to land on the SAME pooled
    connection, so the second one's BEGIN raised "cannot start a transaction
    within a transaction" — and when the timing let both through, one's
    rollback erased the other's committed rows. They must queue instead.
    """
    order = []

    async def writer(tag: str, hold: float):
        async with db.transaction() as conn:
            order.append(f"{tag}-in")
            await conn.execute(
                text("INSERT INTO rows (id, name, n) VALUES (:i, :s, 1)"),
                {"i": tag, "s": tag},
            )
            await asyncio.sleep(hold)     # an await INSIDE the transaction
            order.append(f"{tag}-out")

    await asyncio.gather(writer("a", 0.05), writer("b", 0.0))

    # Never interleaved: one transaction closes before the next opens.
    assert order in (
        ["a-in", "a-out", "b-in", "b-out"],
        ["b-in", "b-out", "a-in", "a-out"],
    ), order
    assert await db.count() == 2


async def test_a_rollback_cannot_erase_another_transactions_writes(db):
    """The silent half of the same bug: 36 callers told 'ok', 30 rows written."""
    async def committer():
        async with db.transaction() as conn:
            await conn.execute(
                text("INSERT INTO rows (id, name, n) VALUES ('keep', 'keep', 1)")
            )
            await asyncio.sleep(0.05)

    async def aborter():
        await asyncio.sleep(0.01)
        with pytest.raises(RuntimeError):
            async with db.transaction() as conn:
                await conn.execute(
                    text("INSERT INTO rows (id, name, n) VALUES ('gone', 'gone', 1)")
                )
                raise RuntimeError("boom")

    await asyncio.gather(committer(), aborter())
    names = sorted(r.name for r in await db.find())
    assert names == ["keep"]


async def test_single_writes_queue_behind_an_open_transaction(db):
    """A plain `save()` must not walk into a transaction someone else holds."""
    async def holder():
        async with db.transaction() as conn:
            await conn.execute(
                text("INSERT INTO rows (id, name, n) VALUES ('t', 't', 1)")
            )
            await asyncio.sleep(0.05)

    async def saver():
        await asyncio.sleep(0.01)
        await db.save(_Row(name="s"), skip_errors=False)

    await asyncio.gather(holder(), saver())
    assert await db.count() == 2


# ---- where the work runs ------------------------------------------------


async def test_bounded_reads_run_on_the_event_loop(db):
    """A point read is ~4 microseconds: a thread hop would cost more than the
    query. It must execute inline, on the loop's own thread."""
    await db.save(_Row(name="a"), skip_errors=False)
    row = (await db.find(name="a"))[0]

    seen = {}
    loop_thread = threading.get_ident()

    real_read = db._read

    async def spy(fn, *, bounded):
        def wrapped(conn):
            seen["thread"] = threading.get_ident()
            return fn(conn)
        return await real_read(wrapped, bounded=bounded)

    db._read = spy
    try:
        await db.get_by_id(row.id)
        assert seen["thread"] == loop_thread          # by primary key
        await db.find(name="a")
        assert seen["thread"] == loop_thread          # by an indexed column
        await db.find(limit=10)
        assert seen["thread"] == loop_thread          # bounded by LIMIT
    finally:
        db._read = real_read


async def test_unbounded_reads_are_off_loaded_to_a_thread(db):
    """A scan can hold the loop for as long as the table is big, so it must not
    run on it. `other` is deliberately an unindexed column."""
    await db.save(_Row(name="a", other="x"), skip_errors=False)

    seen = {}
    loop_thread = threading.get_ident()
    real_read = db._read

    async def spy(fn, *, bounded):
        def wrapped(conn):
            seen["thread"] = threading.get_ident()
            return fn(conn)
        return await real_read(wrapped, bounded=bounded)

    db._read = spy
    try:
        await db.find(other="x")                      # unindexed filter
        assert seen["thread"] != loop_thread
        await db.fetch_all()                          # whole table
        assert seen["thread"] != loop_thread
    finally:
        db._read = real_read


def test_bounded_classifies_by_schema_not_by_data(db_path=None, tmp_path=None):
    """`_bounded` is a schema question, answered without touching the data."""
    d = AsyncDB("/tmp/_esuls_bounded_check.db", "rows", _Row)
    assert d._bounded({"id": "x"}, None) is True          # primary key
    assert d._bounded({"name": "x"}, None) is True        # declared index
    assert d._bounded({"name__like": "x%"}, None) is True  # op suffix stripped
    assert d._bounded({"other": "x"}, None) is False      # no index
    assert d._bounded({}, 10) is True                     # LIMIT
    assert d._bounded({}, None) is False                  # full table


# ---- the awaitable connection stays awaitable ---------------------------


async def test_transaction_connection_is_still_awaited(db):
    """Callers (and migration files in other repos) write
    `await conn.execute(...)`; that must keep working over a sync connection."""
    async with db.transaction() as conn:
        result = await conn.execute(text("SELECT 1"))
        assert result.scalar() == 1

    async with db.transaction(read_only=True) as conn:
        rows = (await conn.execute(text("SELECT COUNT(*) FROM rows"))).scalar()
        assert rows == 0


async def test_transaction_conn_still_drives_multi_table_writes(db, tmp_path):
    """`conn=` on the write methods keeps its all-or-nothing contract."""
    other = AsyncDB(db.db_path, "others", _Row)
    try:
        async with db.transaction() as conn:
            await db.save(_Row(name="a"), conn=conn, skip_errors=False)
            await other.save(_Row(name="b"), conn=conn, skip_errors=False)
        assert await db.count() == 1
        assert await other.count() == 1

        with pytest.raises(RuntimeError):
            async with db.transaction() as conn:
                await db.save(_Row(name="c"), conn=conn, skip_errors=False)
                raise RuntimeError("boom")
        assert await db.count() == 1          # rolled back with the transaction
    finally:
        await other.close()


# ---- col(): the write the database computes itself ----------------------


async def test_col_drives_an_atomic_increment(db):
    """`SET n = n + 1` must survive concurrency that read-modify-write loses."""
    await db.save(_Row(id="c", name="counter", n=0), skip_errors=False)
    n = db.col("n")

    await asyncio.gather(*[
        db.update_many({"n": n + 1}, id="c") for _ in range(50)
    ])
    assert (await db.get_by_id("c")).n == 50


async def test_col_compares_two_columns_in_a_filter(db):
    """The capacity guard: claim a seat only while there is one."""
    await db.save(_Row(id="c", name="course", n=0), skip_errors=False)
    # `other` stands in for a capacity column; 2 seats, 5 racing claims.
    await db.update_many({"other": "2"}, id="c")
    claimed = 0
    for _ in range(5):
        claimed += await db.update_many(
            {"n": db.col("n") + 1}, id="c", n__lt=db.col("other")
        )
    assert claimed == 2
    assert (await db.get_by_id("c")).n == 2


async def test_col_rejects_an_unknown_column(db):
    with pytest.raises(ValueError, match="Unknown column"):
        db.col("nope")
