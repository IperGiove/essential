"""
Tests for specific fixes applied to AsyncDB.
Covers: limit/offset, save_batch reset, enum deserialization,
NOT NULL schema logic, identifier validation, connection pooling.
"""
import asyncio
import enum
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from esuls.db_cli import AsyncDB, BaseModel, _validate_identifier


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


@dataclass
class TestItem(BaseModel):
    name: str = ""
    value: int = 0


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
    """Test that persistent connection is reused across operations."""
    db = AsyncDB(temp_db, "items", TestItem)

    # First operation creates the connection
    await db.save(TestItem(name="first", value=1))
    conn1 = db._connection
    assert conn1 is not None

    # Subsequent operations reuse it
    await db.find()
    conn2 = db._connection
    assert conn2 is conn1, "Connection should be reused"

    await db.count()
    conn3 = db._connection
    assert conn3 is conn1, "Connection should still be reused"

    await db.save(TestItem(name="second", value=2))
    conn4 = db._connection
    assert conn4 is conn1, "Connection should persist across saves"

    # Explicit close
    await db.close()
    assert db._connection is None

    # Next operation creates a new connection
    await db.find()
    conn5 = db._connection
    assert conn5 is not None
    assert conn5 is not conn1, "Should be a new connection after close"

    await db.close()
    print("✓ Connection pooling reuses connections correctly")


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
    """Test async context manager for automatic cleanup."""
    async with AsyncDB(temp_db, "items", TestItem) as db:
        await db.save(TestItem(name="ctx", value=42))
        items = await db.find()
        assert len(items) == 1
        assert items[0].name == "ctx"

    # Connection should be closed after exiting context
    assert db._connection is None

    # Should still work when reopened
    async with AsyncDB(temp_db, "items", TestItem) as db:
        items = await db.find()
        assert len(items) == 1

    print("✓ Context manager works correctly")


async def test_prepare_item_dedup(temp_db):
    """Test that _prepare_item produces correct SQL and values for save."""
    db = AsyncDB(temp_db, "items", TestItem)

    try:
        item = TestItem(name="test", value=5)
        sql, values = db._prepare_item(item)
        assert "ON CONFLICT(id) DO UPDATE SET" in sql
        assert "test" in values
        assert 5 in values

        # Verify save still works end-to-end
        await db.save(item)
        loaded = await db.get_by_id(item.id)
        assert loaded.name == "test"
        assert loaded.value == 5

        # Verify batch save still works
        items = [TestItem(name=f"batch_{i}", value=i) for i in range(5)]
        count = await db.save_batch(items)
        assert count == 5

        print("✓ _prepare_item() deduplication works correctly")
    finally:
        await db.close()


if __name__ == "__main__":
    async def run_all_tests():
        with tempfile.TemporaryDirectory() as tmpdir:
            print("\n" + "=" * 60)
            print("ASYNCDB FIX VERIFICATION TESTS")
            print("=" * 60)

            test_num = 0

            async def run_test(name, coro):
                nonlocal test_num
                test_num += 1
                # Use a fresh db for each test
                db_path = Path(tmpdir) / f"test_fix_{test_num}.db"
                print(f"\n[Test {test_num}] {name}...")
                await coro(db_path)

            async def run_test_no_db(name, coro):
                nonlocal test_num
                test_num += 1
                print(f"\n[Test {test_num}] {name}...")
                await coro()

            await run_test("Limit/offset in find()", test_find_limit_offset)
            await run_test("save_batch count reset", test_save_batch_count_reset)
            await run_test("Enum subclass deserialization", test_enum_deserialization_subclasses)
            await run_test("NOT NULL with falsy defaults", test_not_null_with_falsy_defaults)
            await run_test_no_db("Identifier validation", test_identifier_validation)
            await run_test("Constructor identifier validation", test_identifier_validation_in_constructor)
            await run_test("Connection pooling", test_connection_pooling)
            await run_test("Close idempotent", test_close_idempotent)
            await run_test("exists()", test_exists)
            await run_test("delete_many()", test_delete_many)
            await run_test("update_fields()", test_update_fields)
            await run_test("Context manager", test_context_manager)
            await run_test("_prepare_item dedup", test_prepare_item_dedup)

            print("\n" + "=" * 60)
            print("ALL TESTS PASSED!")
            print("=" * 60)

    asyncio.run(run_all_tests())
