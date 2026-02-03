"""
Test concurrent database access to verify retry logic works.
"""
import asyncio
import tempfile
import os
from dataclasses import dataclass, field
from pathlib import Path

from esuls.db_cli import AsyncDB, BaseModel


@dataclass
class TestItem(BaseModel):
    name: str = ""
    value: int = 0


async def test_concurrent_reads(temp_db):
    """Test many concurrent read operations."""
    db = AsyncDB(temp_db, "items", TestItem)

    # Save some test data first
    for i in range(10):
        await db.save(TestItem(name=f"item_{i}", value=i))

    # Run 100 concurrent reads
    async def read_all():
        return await db.find()

    tasks = [read_all() for _ in range(100)]
    results = await asyncio.gather(*tasks)

    # All reads should succeed and return same data
    assert all(len(r) == 10 for r in results)
    print(f"✓ 100 concurrent reads completed successfully")


async def test_concurrent_writes(temp_db):
    """Test many concurrent write operations."""
    db = AsyncDB(temp_db, "items", TestItem)

    # Run 50 concurrent writes
    async def write_item(i: int):
        return await db.save(TestItem(name=f"concurrent_{i}", value=i))

    tasks = [write_item(i) for i in range(50)]
    results = await asyncio.gather(*tasks)

    # All writes should succeed
    assert all(r is True for r in results)

    # Verify all items were saved
    items = await db.find()
    assert len(items) == 50
    print(f"✓ 50 concurrent writes completed successfully")


async def test_concurrent_mixed_operations(temp_db):
    """Test concurrent reads and writes together."""
    db = AsyncDB(temp_db, "items", TestItem)

    # Seed some data
    for i in range(5):
        await db.save(TestItem(name=f"seed_{i}", value=i))

    async def read_op():
        return await db.find()

    async def write_op(i: int):
        return await db.save(TestItem(name=f"mixed_{i}", value=i))

    async def count_op():
        return await db.count()

    # Mix of 100 reads, 50 writes, 50 counts - all concurrent
    tasks = []
    tasks.extend([read_op() for _ in range(100)])
    tasks.extend([write_op(i) for i in range(50)])
    tasks.extend([count_op() for _ in range(50)])

    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Check no exceptions
    exceptions = [r for r in results if isinstance(r, Exception)]
    if exceptions:
        print(f"✗ {len(exceptions)} exceptions occurred:")
        for e in exceptions[:5]:
            print(f"  - {type(e).__name__}: {e}")
        raise AssertionError(f"{len(exceptions)} operations failed")

    # Verify final state
    items = await db.find()
    assert len(items) == 55  # 5 seed + 50 writes
    print(f"✓ 200 concurrent mixed operations completed successfully")


async def test_stress_concurrent_access(temp_db):
    """Stress test with very high concurrency."""
    db = AsyncDB(temp_db, "items", TestItem)

    # Run 500 concurrent operations
    async def random_op(i: int):
        if i % 3 == 0:
            return await db.save(TestItem(name=f"stress_{i}", value=i))
        elif i % 3 == 1:
            return await db.find()
        else:
            return await db.count()

    tasks = [random_op(i) for i in range(500)]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Count successes and failures
    exceptions = [r for r in results if isinstance(r, Exception)]
    successes = len(results) - len(exceptions)

    print(f"Results: {successes} successes, {len(exceptions)} failures")

    if exceptions:
        print(f"Sample exceptions:")
        for e in exceptions[:3]:
            print(f"  - {type(e).__name__}: {e}")

    # Should have very few or no failures with retry logic
    assert len(exceptions) == 0, f"{len(exceptions)} operations failed"
    print(f"✓ 500 concurrent stress operations completed successfully")


if __name__ == "__main__":
    import sys

    async def run_all_tests():
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test_concurrent.db"

            print("\n" + "=" * 60)
            print("CONCURRENT DATABASE ACCESS TESTS")
            print("=" * 60)

            print("\n[Test 1] Concurrent reads...")
            await test_concurrent_reads(db_path)

            # New db for each test
            db_path2 = Path(tmpdir) / "test_concurrent2.db"
            print("\n[Test 2] Concurrent writes...")
            await test_concurrent_writes(db_path2)

            db_path3 = Path(tmpdir) / "test_concurrent3.db"
            print("\n[Test 3] Mixed operations...")
            await test_concurrent_mixed_operations(db_path3)

            db_path4 = Path(tmpdir) / "test_concurrent4.db"
            print("\n[Test 4] Stress test (500 ops)...")
            await test_stress_concurrent_access(db_path4)

            print("\n" + "=" * 60)
            print("ALL TESTS PASSED!")
            print("=" * 60)

    asyncio.run(run_all_tests())
