"""
Tests for specific fixes applied to AsyncDB.
Covers: limit/offset, save_batch reset, enum deserialization,
NOT NULL schema logic, identifier validation, connection pooling.
"""
import asyncio
import enum
from dataclasses import dataclass, field
from typing import Optional

import sqlite3
from sqlalchemy import event, text

from esuls.db_cli import (
    AsyncDB,
    BaseModel,
    _db_state_by_loop,
    _is_sqlite_busy,
    _is_stale_connection,
    _validate_identifier,
)


# --- Test models ---

class Color(enum.Enum):
    RED = "red"
    GREEN = "green"
    BLUE = "blue"


class Priority(enum.IntEnum):
    LOW = 1
    MEDIUM = 2
    HIGH = 3


class Status(str, enum.Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"


@dataclass
class EnumItem(BaseModel):
    color: Optional[Color] = None
    priority: Optional[Priority] = None
    status: Optional[Status] = None


@dataclass
class DefaultsItem(BaseModel):
    name: str = ""
    count: int = 0
    flag: bool = False
    score: float = 0.0
    required_field: str = field(default="", metadata={"required": False})


# TestItem is shared across test_db_concurrent and test_db_fixes — see
# tests/_common.py. Imported below to avoid R0801 (duplicate-code).
from esuls.tests._common import TestItem  # noqa: E402  — kept after schema models


# --- Tests ---

async def test_find_limit_offset(temp_db):
    """Test limit and offset parameters on find()."""
    db = AsyncDB(temp_db, "items", TestItem)

    try:
        # Insert 20 items with predictable ordering
        for i in range(20):
            await db.save(TestItem(name=f"item_{i:02d}", value=i))

        # Test limit
        results = await db.find(order_by="value", limit=5)
        assert len(results) == 5
        assert results[0].value == 0
        assert results[4].value == 4

        # Test offset
        results = await db.find(order_by="value", limit=5, offset=10)
        assert len(results) == 5
        assert results[0].value == 10
        assert results[4].value == 14

        # Test offset beyond data
        results = await db.find(order_by="value", limit=5, offset=18)
        assert len(results) == 2
        assert results[0].value == 18

        # Test limit without offset
        results = await db.find(order_by="value", limit=3)
        assert len(results) == 3

        # Test offset without limit (should return from offset to end)
        results = await db.find(order_by="value", offset=17)
        assert len(results) == 3

        # Test no limit/offset (all results)
        results = await db.find()
        assert len(results) == 20

        print("✓ find() limit/offset works correctly")
    finally:
        await db.close()


async def test_save_batch_count_reset(temp_db):
    """Test that saved_count resets properly on retry in save_batch()."""
    db = AsyncDB(temp_db, "items", TestItem)

    try:
        items = [TestItem(name=f"batch_{i}", value=i) for i in range(10)]
        count = await db.save_batch(items)
        assert count == 10, f"Expected 10, got {count}"

        # Save again (upsert) — count should still be exactly 10
        count2 = await db.save_batch(items)
        assert count2 == 10, f"Expected 10 on re-save, got {count2}"

        print("✓ save_batch() count is accurate")
    finally:
        await db.close()


async def test_enum_deserialization_subclasses(temp_db):
    """Test enum deserialization with IntEnum, StrEnum, and regular Enum."""
    db = AsyncDB(temp_db, "enums", EnumItem)

    try:
        # Save with all enum types
        item = EnumItem(
            color=Color.GREEN,
            priority=Priority.HIGH,
            status=Status.ACTIVE,
        )
        await db.save(item)

        # Read back
        results = await db.find()
        assert len(results) == 1
        loaded = results[0]

        assert loaded.color == Color.GREEN, f"Expected Color.GREEN, got {loaded.color!r}"
        assert loaded.priority == Priority.HIGH, f"Expected Priority.HIGH, got {loaded.priority!r}"
        assert loaded.status == Status.ACTIVE, f"Expected Status.ACTIVE, got {loaded.status!r}"

        # Test None values
        item2 = EnumItem()
        await db.save(item2)
        results = await db.find()
        none_items = [r for r in results if r.color is None]
        assert len(none_items) == 1
        assert none_items[0].priority is None
        assert none_items[0].status is None

        print("✓ Enum deserialization works for Enum, IntEnum, StrEnum")
    finally:
        await db.close()


async def test_enum_subclass_column_types(temp_db):
    """IntEnum → INTEGER, StrEnum → TEXT, regular Enum → TEXT.

    Regression: pre-fix the schema-init's type map matched `enum.EnumType`
    (the metaclass), which never matches a concrete enum class, so all
    enum fields fell through to the default TEXT branch. IntEnum should
    map to INTEGER for proper column typing and index efficiency.
    """
    db = AsyncDB(temp_db, "enum_cols", EnumItem)
    try:
        # Trigger schema creation.
        await db.save(EnumItem(color=Color.RED, priority=Priority.HIGH,
                               status=Status.ACTIVE))

        async with db.transaction(read_only=True) as conn:
            result = await conn.execute(text("PRAGMA table_info(enum_cols)"))
            rows = result.fetchall()

        col_types = {row[1]: row[2] for row in rows}
        assert col_types.get("priority") == "INTEGER", col_types
        assert col_types.get("color") == "TEXT", col_types
        assert col_types.get("status") == "TEXT", col_types
        # Sanity check: round-trip still works after the column-type fix.
        results = await db.find()
        assert len(results) == 1
        assert results[0].priority == Priority.HIGH
        assert results[0].color == Color.RED
        assert results[0].status == Status.ACTIVE
        print("✓ enum subclasses map to correct SQLite column types")
    finally:
        await db.close()


async def test_int_and_bool_column_types(temp_db):
    """bool/int/float/str map to the SQLA-rendered declared types.

    Note: SQLite is dynamically typed; the *declared* type only sets
    type affinity. Boolean → BOOLEAN (NUMERIC affinity), Float → FLOAT
    (REAL affinity). Functionally equivalent to the prior INTEGER/REAL
    declarations.
    """
    db = AsyncDB(temp_db, "defaults_cols", DefaultsItem)
    try:
        await db.save(DefaultsItem())

        async with db.transaction(read_only=True) as conn:
            result = await conn.execute(text("PRAGMA table_info(defaults_cols)"))
            rows = result.fetchall()

        col_types = {row[1]: row[2] for row in rows}
        assert col_types.get("count") == "INTEGER", col_types
        assert col_types.get("flag") == "BOOLEAN", col_types
        assert col_types.get("score") == "FLOAT", col_types
        assert col_types.get("name") == "TEXT", col_types
        print("✓ int/bool/float/str map to correct SQLite column types")
    finally:
        await db.close()


async def test_not_null_with_falsy_defaults(temp_db):
    """Test that fields with falsy defaults (0, '', False) don't get NOT NULL."""
    db = AsyncDB(temp_db, "defaults_test", DefaultsItem)

    try:
        # If the schema was created correctly, saving an item with default values should work
        item = DefaultsItem()
        result = await db.save(item)
        assert result is True

        # Verify roundtrip
        items = await db.find()
        assert len(items) == 1
        loaded = items[0]
        assert loaded.name == ""
        assert loaded.count == 0
        assert loaded.flag is False

        print("✓ NOT NULL logic correctly handles falsy defaults")
    finally:
        await db.close()


async def test_is_sqlite_busy_helper():
    """_is_sqlite_busy classifies retryable SQLite errors via errorcode and string."""
    # Real SQLite OperationalError carries sqlite_errorcode.
    busy = sqlite3.OperationalError("database is locked")
    busy.sqlite_errorcode = sqlite3.SQLITE_BUSY
    locked = sqlite3.OperationalError("database table is locked")
    locked.sqlite_errorcode = sqlite3.SQLITE_LOCKED

    assert _is_sqlite_busy(busy) is True
    assert _is_sqlite_busy(locked) is True

    # Wrapped or stripped errors fall back to string match.
    plain = Exception("database is locked")  # no errorcode attribute
    assert _is_sqlite_busy(plain) is True
    plain2 = Exception("database is busy retrying")
    assert _is_sqlite_busy(plain2) is True

    # Unrelated errors must NOT be classified as retryable.
    assert _is_sqlite_busy(ValueError("nope")) is False
    assert _is_sqlite_busy(Exception("syntax error")) is False

    # Other sqlite codes are not retryable.
    misuse = sqlite3.OperationalError("misuse")
    misuse.sqlite_errorcode = sqlite3.SQLITE_MISUSE
    assert _is_sqlite_busy(misuse) is False
    print("✓ _is_sqlite_busy classifies BUSY/LOCKED via code + string fallback")


async def test_is_stale_connection_helper():
    """_is_stale_connection matches aiosqlite-layer dead-connection errors."""
    assert _is_stale_connection(Exception("Connection is closed")) is True
    assert _is_stale_connection(Exception("no active connection")) is True
    assert _is_stale_connection(Exception("CLOSED")) is True  # case-insensitive

    # Unrelated errors must NOT match.
    assert _is_stale_connection(Exception("database is locked")) is False
    assert _is_stale_connection(Exception("syntax error")) is False
    print("✓ _is_stale_connection matches closed/no-active-connection")


async def test_identifier_validation():
    """Test that invalid SQL identifiers are rejected."""
    # Valid identifiers
    assert _validate_identifier("items") == "items"
    assert _validate_identifier("my_table") == "my_table"
    assert _validate_identifier("_private") == "_private"
    assert _validate_identifier("Table123") == "Table123"

    # Invalid identifiers
    invalid_names = [
        "Robert'; DROP TABLE students;--",
        "my table",
        "123abc",
        "my-table",
        "",
        "table.name",
        "col(umn)",
    ]
    for name in invalid_names:
        try:
            _validate_identifier(name)
            raise AssertionError(f"Should have rejected: {name!r}")
        except ValueError:
            pass  # Expected

    print("✓ Identifier validation rejects injection attempts")


async def test_identifier_validation_in_constructor(temp_db):
    """Test that AsyncDB constructor rejects invalid table names."""
    try:
        AsyncDB(temp_db, "valid_table; DROP TABLE x", TestItem)
        raise AssertionError("Should have rejected invalid table name")
    except ValueError:
        pass

    print("✓ Constructor rejects invalid table names")


async def test_connection_pooling(temp_db):
    """Engines are cached per (loop, db_path) and reused across operations.

    Replaces the old `db._connection` identity check. The SQLA writer/reader
    engine pair lives in the per-loop registry; it must persist across
    operations and be reset after `close()`.
    """
    db = AsyncDB(temp_db, "items", TestItem)
    key = str(temp_db.resolve())

    # First operation creates the engine pair
    await db.save(TestItem(name="first", value=1))
    state = _db_state_by_loop[asyncio.get_running_loop()]
    pair1 = state["engines"][key]
    assert pair1 is not None

    # Subsequent operations reuse the same engines
    await db.find()
    assert state["engines"][key] is pair1, "engines should be reused"

    await db.count()
    assert state["engines"][key] is pair1

    await db.save(TestItem(name="second", value=2))
    assert state["engines"][key] is pair1

    # Explicit close clears the cache
    await db.close()
    assert key not in state["engines"]

    # Next operation creates a fresh engine pair
    await db.find()
    pair2 = state["engines"][key]
    assert pair2 is not None and pair2 is not pair1, "fresh engines after close"

    await db.close()
    print("✓ Engine pool reuses (writer, reader) across ops and resets on close")


async def test_close_idempotent(temp_db):
    """Test that close() can be called multiple times safely."""
    db = AsyncDB(temp_db, "items", TestItem)
    await db.save(TestItem(name="test", value=1))

    await db.close()
    await db.close()  # Should not raise
    await db.close()  # Should not raise

    # Should still work after close
    items = await db.find()
    assert len(items) == 1

    await db.close()
    print("✓ close() is idempotent")


async def test_exists(temp_db):
    """Test exists() returns bool without fetching full records."""
    db = AsyncDB(temp_db, "items", TestItem)

    try:
        assert await db.exists(name="nope") is False

        await db.save(TestItem(name="hello", value=1))
        assert await db.exists(name="hello") is True
        assert await db.exists(name="nope") is False
        assert await db.exists(value=1) is True
        assert await db.exists(value=999) is False

        print("✓ exists() works correctly")
    finally:
        await db.close()


async def test_delete_many(temp_db):
    """Test delete_many() deletes matching records and returns count."""
    db = AsyncDB(temp_db, "items", TestItem)

    try:
        for i in range(10):
            await db.save(TestItem(name="group_a" if i < 6 else "group_b", value=i))

        # Delete group_a
        deleted = await db.delete_many(name="group_a")
        assert deleted == 6, f"Expected 6 deleted, got {deleted}"

        remaining = await db.count()
        assert remaining == 4, f"Expected 4 remaining, got {remaining}"

        # Delete non-existent
        deleted = await db.delete_many(name="group_c")
        assert deleted == 0

        # Must raise on empty filters
        try:
            await db.delete_many()
            raise AssertionError("Should have raised ValueError")
        except ValueError:
            pass

        print("✓ delete_many() works correctly")
    finally:
        await db.close()


async def test_update_fields(temp_db):
    """Test update_fields() updates specific fields without full fetch."""
    db = AsyncDB(temp_db, "items", TestItem)

    try:
        item = TestItem(name="original", value=10)
        await db.save(item)

        # Update single field
        result = await db.update_fields(item.id, name="updated")
        assert result is True

        loaded = await db.get_by_id(item.id)
        assert loaded.name == "updated"
        assert loaded.value == 10  # unchanged

        # Update multiple fields
        result = await db.update_fields(item.id, name="final", value=99)
        assert result is True
        loaded = await db.get_by_id(item.id)
        assert loaded.name == "final"
        assert loaded.value == 99

        # Non-existent ID
        result = await db.update_fields("nonexistent-id", name="x")
        assert result is False

        # Empty fields
        result = await db.update_fields(item.id)
        assert result is False

        print("✓ update_fields() works correctly")
    finally:
        await db.close()


async def test_context_manager(temp_db):
    """Async context manager disposes engines on __aexit__."""
    key = str(temp_db.resolve())
    async with AsyncDB(temp_db, "items", TestItem) as db:
        await db.save(TestItem(name="ctx", value=42))
        items = await db.find()
        assert len(items) == 1
        assert items[0].name == "ctx"

    # After __aexit__, the engine pair is no longer in the registry.
    state = _db_state_by_loop[asyncio.get_running_loop()]
    assert key not in state["engines"]

    # Should still work when reopened
    async with AsyncDB(temp_db, "items", TestItem) as db:
        items = await db.find()
        assert len(items) == 1

    print("✓ Context manager works correctly")


async def test_read_paths_skip_commit(temp_db):
    """Read-only operations must NOT issue COMMIT.

    With SQLA the reader engine is opened via `engine.connect()` (no BEGIN),
    while the writer uses `engine.begin()` (autocommit on success). We
    register a `commit` event listener on the reader's underlying sync
    engine and assert it never fires from read paths.
    """
    db = AsyncDB(temp_db, "items", TestItem)
    try:
        # Force engines open with one initial save.
        await db.save(TestItem(name="seed", value=1))

        writer, reader = await db._ensure_engines()
        reader_commits = 0
        writer_commits = 0

        @event.listens_for(reader.sync_engine, "commit")
        def _on_reader_commit(_conn):
            nonlocal reader_commits
            reader_commits += 1

        @event.listens_for(writer.sync_engine, "commit")
        def _on_writer_commit(_conn):
            nonlocal writer_commits
            writer_commits += 1

        # Read paths — reader engine must not commit.
        await db.find()
        await db.find(name="seed")
        await db.count()
        await db.count(name="seed")
        await db.get_by_id("nonexistent")
        await db.exists(name="seed")
        await db.fetch_all()
        assert reader_commits == 0, (
            f"reader committed {reader_commits} times — expected 0"
        )

        # Write paths — writer engine must commit.
        await db.save(TestItem(name="another", value=2))
        assert writer_commits >= 1, (
            f"writer didn't commit on save (count={writer_commits})"
        )
        commits_after_save = writer_commits

        await db.delete_many(name="seed")
        assert writer_commits > commits_after_save, (
            "writer didn't commit on delete_many"
        )

        print("✓ reader engine never commits; writer commits on writes")
    finally:
        await db.close()


async def test_item_to_row_preserves_explicit_falsy_id(temp_db):
    """Falsy-but-non-None ids must be preserved, not silently replaced.

    `_item_to_row` (formerly `_prepare_item`) only auto-generates a UUID
    when id is explicitly None; empty-string / 0 / False stay untouched.
    """
    db = AsyncDB(temp_db, "items", TestItem)
    try:
        item_empty = TestItem(id="", name="empty_id", value=1)
        row = db._item_to_row(item_empty)
        assert row["id"] == "", f"expected '', got {row['id']!r}"

        await db.save(item_empty)
        results = await db.find(name="empty_id")
        assert len(results) == 1
        assert results[0].id == "", (
            f"id was silently replaced: {results[0].id!r}"
        )
        print("✓ _item_to_row preserves explicit empty-string id")
    finally:
        await db.close()


async def test_item_to_row_generates_id_when_none(temp_db):
    """When id is explicitly None, a fresh UUID is generated."""
    db = AsyncDB(temp_db, "items", TestItem)
    try:
        item = TestItem(name="auto_id", value=2)
        item.id = None  # type: ignore[assignment]
        row = db._item_to_row(item)
        generated = row["id"]
        assert isinstance(generated, str)
        assert len(generated) == 36 and generated.count("-") == 4, (
            f"unexpected id format: {generated!r}"
        )
        print("✓ _item_to_row generates UUID when id is None")
    finally:
        await db.close()


async def test_upsert_sql_shape_and_end_to_end(temp_db):
    """The compiled upsert has the expected ON CONFLICT shape, and
    save/get_by_id/save_batch all work end-to-end.

    Replaces the old `_prepare_item_dedup` test that inspected raw SQL.
    """
    from sqlalchemy.dialects import sqlite as sqlite_dialect

    db = AsyncDB(temp_db, "items", TestItem)
    try:
        # Inspect the compiled SQL for the default-conflict-target upsert.
        stmt = db._build_upsert(("id",)).values(
            **db._item_to_row(TestItem(name="test", value=5))
        )
        compiled = str(stmt.compile(dialect=sqlite_dialect.dialect()))
        assert "INSERT INTO items" in compiled
        assert "ON CONFLICT (id) DO UPDATE" in compiled
        # created_at must NOT be in the UPDATE SET clause.
        set_clause = compiled.split("DO UPDATE")[1]
        assert "created_at" not in set_clause
        assert "updated_at" in set_clause
        assert "name" in set_clause

        # End-to-end save still works.
        item = TestItem(name="test", value=5)
        await db.save(item)
        loaded = await db.get_by_id(item.id)
        assert loaded.name == "test" and loaded.value == 5

        # And batch save.
        items = [TestItem(name=f"batch_{i}", value=i) for i in range(5)]
        count = await db.save_batch(items)
        assert count == 5

        print("✓ upsert SQL shape and end-to-end save work")
    finally:
        await db.close()


