"""
Advanced AsyncDB tests covering:
  - Serialization of every type the schema can hold (set, bytes, datetime,
    enum, Decimal, UUID, nested list/dict, custom class rejection).
  - Deserialization including the schema-drift recovery branches
    (Optional[bytes], str-with-bytes-value, ast.literal_eval fallback).
  - The new `executemany`-based atomic save_batch (skip_errors=False).
  - WHERE-clause operators: gt/lt/gte/lte/neq/like/in/eq.
  - ORDER BY (asc, desc, multi-column).
  - LIMIT/OFFSET edge cases.
  - on_conflict semantics: id (default), natural key, composite tuple.
  - Transaction rollback on exception (write path) vs read-only no-op.
  - exists() shortcut, count() with filters.
  - update_fields/delete behaviour on missing rows.
  - Schema migration: ALTER TABLE ADD COLUMN on existing data.
  - BaseModel.to_dict / from_dict bytes roundtrip via _B64_PREFIX.

The suite is laid out as standalone `async def test_*` functions that
each create their own tempdir + AsyncDB — no shared state, ordering does
not matter.
"""
import asyncio
import enum
import json
import sqlite3
import tempfile
import uuid as uuid_mod
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Optional, Set

import sqlalchemy.exc as sa_exc
from sqlalchemy import text
from sqlalchemy.dialects import sqlite as sqlite_dialect

from esuls.db_cli import (
    AsyncDB,
    BaseModel,
    _B64_PREFIX,
    _is_bytes_field,
    _json_encode_default as _json_default,  # renamed in the refactor
)


# ────────────────────────────────────────────────────────────────────────
# Schemas used across tests
# ────────────────────────────────────────────────────────────────────────

class Color(enum.Enum):
    RED = "red"
    BLUE = "blue"


class Priority(enum.IntEnum):
    LOW = 1
    HIGH = 10


@dataclass
class WithSet(BaseModel):
    tags: set = field(default_factory=set)


@dataclass
class WithOptBytes(BaseModel):
    blob: Optional[bytes] = None


@dataclass
class WithOptStr(BaseModel):
    note: Optional[str] = None


@dataclass
class WithEverything(BaseModel):
    """Kitchen-sink schema exercising every supported type."""
    name: str = ""
    count: int = 0
    flag: bool = False
    weight: float = 0.0
    payload: bytes = b""
    when: Optional[datetime] = None
    color: Optional[Color] = None
    priority: Optional[Priority] = None
    tags: list = field(default_factory=list)
    extra: dict = field(default_factory=dict)


@dataclass
class WithNaturalKey(BaseModel):
    external_id: str = field(default="", metadata={"unique": True})
    payload: str = ""


@dataclass
class WithIndexed(BaseModel):
    category: str = field(default="", metadata={"index": True})
    name: str = ""


# ────────────────────────────────────────────────────────────────────────
# 1. _json_default — exhaustive type coverage
# ────────────────────────────────────────────────────────────────────────

def test_json_default_datetime():
    """datetime → ISO 8601 (roundtrips through `datetime.fromisoformat`)."""
    dt = datetime(2026, 5, 14, 12, 30, 45, 678901)
    encoded = _json_default(dt)
    assert encoded == "2026-05-14T12:30:45.678901"
    assert datetime.fromisoformat(encoded) == dt


def test_json_default_date():
    """date → ISO 8601 (no time component)."""
    d = date(2026, 5, 14)
    assert _json_default(d) == "2026-05-14"


def test_json_default_bytes_b64():
    """bytes → _B64_PREFIX + base64 (recoverable by `from_dict`)."""
    import base64
    payload = b"\x00\x01\xff\xfe"
    encoded = _json_default(payload)
    assert encoded.startswith(_B64_PREFIX)
    assert base64.b64decode(encoded[len(_B64_PREFIX):]) == payload


def test_json_default_enum_uses_value():
    """Plain Enum → .value (not name)."""
    assert _json_default(Color.RED) == "red"
    # IntEnum behaviour: .value is an int, JSON-serialisable directly,
    # but _json_default still has to handle it (the caller might pass it).
    assert _json_default(Priority.LOW) == 1


def test_json_default_decimal_preserves_precision():
    """Decimal → string preserves arbitrary precision (float would lose it)."""
    d = Decimal("3.141592653589793238462643383279")
    encoded = _json_default(d)
    assert encoded == "3.141592653589793238462643383279"
    # str() round-trips
    assert Decimal(encoded) == d


def test_json_default_uuid_canonical():
    """UUID → canonical 8-4-4-4-12 string."""
    u = uuid_mod.UUID("12345678-1234-5678-1234-567812345678")
    assert _json_default(u) == "12345678-1234-5678-1234-567812345678"


def test_json_default_set_to_list():
    """set/frozenset → JSON list (loses set semantics, by design)."""
    s = {1, 2, 3}
    encoded = _json_default(s)
    assert isinstance(encoded, list) and sorted(encoded) == [1, 2, 3]
    encoded_fs = _json_default(frozenset(["a", "b"]))
    assert isinstance(encoded_fs, list) and sorted(encoded_fs) == ["a", "b"]


def test_json_default_unknown_type_raises():
    """Custom class with no handler → TypeError (no silent str() fallback)."""
    class _Custom:
        pass
    try:
        _json_default(_Custom())
    except TypeError as e:
        assert "_Custom" in str(e) and "not JSON serialisable" in str(e)
    else:
        raise AssertionError("expected TypeError")


# ────────────────────────────────────────────────────────────────────────
# 2. _is_bytes_field — type detection
# ────────────────────────────────────────────────────────────────────────

def test_is_bytes_field_variants():
    assert _is_bytes_field(bytes) is True
    assert _is_bytes_field(Optional[bytes]) is True
    # Union with bytes alongside other types still counts
    from typing import Union as U
    assert _is_bytes_field(U[bytes, str, None]) is True
    # Plain str and Optional[str] do not
    assert _is_bytes_field(str) is False
    assert _is_bytes_field(Optional[str]) is False
    # No type hint at all
    assert _is_bytes_field(None) is False


# ────────────────────────────────────────────────────────────────────────
# 3. AsyncDB._serialize_value — set roundtrip (D1 fix)
# ────────────────────────────────────────────────────────────────────────

async def test_set_field_roundtrip():
    """A `set` field is serialised to JSON array and read back as list.

    Reflects the documented contract: set → JSON has no native set, so
    we lose the set-ness in the DB. The caller's read path receives a
    list and must re-wrap if they want a set.
    """
    with tempfile.TemporaryDirectory() as d:
        db = AsyncDB(Path(d) / "x.db", "items", WithSet)
        try:
            await db.save(WithSet(tags={"a", "b", "c"}))
            rows = await db.fetch_all()
            assert len(rows) == 1
            # SQLite stored a JSON list; deserializer json.loads back to list.
            # Order is non-deterministic for set; compare as sorted.
            assert sorted(rows[0].tags) == ["a", "b", "c"]
        finally:
            await db.close()


# ────────────────────────────────────────────────────────────────────────
# 4. AsyncDB._deserialize_value — schema-drift recovery (D2, D3)
# ────────────────────────────────────────────────────────────────────────

async def test_optional_bytes_normal_roundtrip():
    """Normal path: Optional[bytes] → BLOB → bytes."""
    with tempfile.TemporaryDirectory() as d:
        db = AsyncDB(Path(d) / "x.db", "items", WithOptBytes)
        try:
            await db.save(WithOptBytes(blob=b"\x00\x01\xff"))
            await db.save(WithOptBytes(blob=None))
            rows = await db.find(order_by="created_at")
            blobs = [r.blob for r in rows]
            assert b"\x00\x01\xff" in blobs and None in blobs
        finally:
            await db.close()


# NOTE: the prior `_deserialize_value` had schema-drift recovery for
# Optional[bytes] columns holding str payloads (via ast.literal_eval).
# That path silently re-encoded random strings as bytes and was a
# data-corruption footgun. The refactor uses TypeDecorators with no
# recovery — out-of-band writes that bypass the decorator are stored
# and read as-is. The two obsolete tests (`recovers_from_str_literal`
# and `non_literal_raises_typeerror`) have been removed; see
# `test_db_sqla.test_no_silent_schema_drift_recovery` for the new
# regression that locks the "pass through, do not invent bytes" contract.


async def test_optional_str_normal_roundtrip():
    """Optional[str] normal path: str preserved, None preserved."""
    with tempfile.TemporaryDirectory() as d:
        db = AsyncDB(Path(d) / "x.db", "items", WithOptStr)
        try:
            await db.save(WithOptStr(note="hello"))
            await db.save(WithOptStr(note=None))
            rows = await db.find(order_by="created_at")
            assert {r.note for r in rows} == {"hello", None}
        finally:
            await db.close()


# NOTE: the prior code also raised TypeError when bytes was returned for
# an Optional[str] column. That defensive check was tied to the str-bytes
# guesswork in _deserialize_value, which the TypeDecorator refactor
# eliminates. See `test_db_sqla.test_no_silent_schema_drift_recovery`.


# ────────────────────────────────────────────────────────────────────────
# 5. save_batch — executemany path (P1)
# ────────────────────────────────────────────────────────────────────────

async def test_save_batch_atomic_executemany():
    """save_batch(skip_errors=False) uses executemany — one round-trip.

    We can't easily observe round-trip count without instrumenting
    aiosqlite, but we verify behaviour: all rows persist after success,
    none persist if any single row violates a constraint.
    """
    with tempfile.TemporaryDirectory() as d:
        db = AsyncDB(Path(d) / "x.db", "items", WithNaturalKey)
        try:
            batch = [WithNaturalKey(external_id=f"ext_{i}", payload=str(i))
                     for i in range(50)]
            n = await db.save_batch(batch)
            assert n == 50
            assert await db.count() == 50
        finally:
            await db.close()


async def test_save_batch_atomic_rollback_on_failure():
    """With skip_errors=False, a constraint failure rolls back the WHOLE
    batch (executemany is atomic). Pre-fix the sequential loop committed
    everything up to the failing item — now we either get all or none.
    """
    with tempfile.TemporaryDirectory() as d:
        db = AsyncDB(Path(d) / "x.db", "items", WithNaturalKey)
        try:
            await db.save(WithNaturalKey(external_id="dup", payload="seed"))

            # Build a batch that's mostly fine but has one item that
            # violates UNIQUE on the natural key (and the conflict target
            # is 'id' — the default — so the natural-key UNIQUE *does*
            # cause an error rather than triggering DO UPDATE).
            batch = [
                WithNaturalKey(external_id="a", payload="1"),
                WithNaturalKey(external_id="b", payload="2"),
                WithNaturalKey(external_id="dup", payload="collision"),
                WithNaturalKey(external_id="d", payload="4"),
            ]
            try:
                await db.save_batch(batch)
                raise AssertionError("expected UNIQUE constraint failure")
            except (sqlite3.IntegrityError, sa_exc.IntegrityError):
                pass

            # Only the seeded row should remain — none of the batch
            # committed (atomic rollback).
            rows = await db.fetch_all()
            assert len(rows) == 1
            assert rows[0].external_id == "dup" and rows[0].payload == "seed"
        finally:
            await db.close()


async def test_save_each_granular():
    """save_each() uses the per-item loop and logs warnings for bad items;
    valid items persist. (Formerly save_batch(skip_errors=True).)"""
    with tempfile.TemporaryDirectory() as d:
        db = AsyncDB(Path(d) / "x.db", "items", WithNaturalKey)
        try:
            await db.save(WithNaturalKey(external_id="dup", payload="seed"))
            # Pass a wrong-type object alongside valid ones.
            class _Other(BaseModel):
                pass
            batch = [
                WithNaturalKey(external_id="a", payload="1"),
                _Other(),  # ← skipped (wrong type)
                WithNaturalKey(external_id="b", payload="2"),
            ]
            n = await db.save_each(batch)
            # Two items inserted, wrong-type silently dropped
            assert n == 2
            assert await db.count() == 3  # 1 seed + 2 valid
        finally:
            await db.close()


async def test_save_batch_empty():
    """Empty input → 0 saved, no-op."""
    with tempfile.TemporaryDirectory() as d:
        db = AsyncDB(Path(d) / "x.db", "items", WithNaturalKey)
        try:
            assert await db.save_batch([]) == 0
            assert await db.save_each([]) == 0
        finally:
            await db.close()


async def test_save_batch_executemany_large_batch():
    """Large batch (1000 items) commits in one shot. Smoke-test that
    executemany doesn't blow up on size."""
    with tempfile.TemporaryDirectory() as d:
        db = AsyncDB(Path(d) / "x.db", "items", WithNaturalKey)
        try:
            batch = [WithNaturalKey(external_id=f"e{i}", payload="x")
                     for i in range(1000)]
            n = await db.save_batch(batch)
            assert n == 1000
            assert await db.count() == 1000
        finally:
            await db.close()


# ────────────────────────────────────────────────────────────────────────
# 6. WHERE operators — full matrix
# ────────────────────────────────────────────────────────────────────────

async def _seed_numeric_table(d):
    db = AsyncDB(Path(d) / "n.db", "items", WithEverything)
    for i in range(10):
        await db.save(WithEverything(name=f"n{i:02d}", count=i, weight=float(i) * 1.5))
    return db


async def test_where_eq():
    with tempfile.TemporaryDirectory() as d:
        db = await _seed_numeric_table(d)
        try:
            rows = await db.find(count=5)
            assert len(rows) == 1 and rows[0].count == 5
        finally:
            await db.close()


async def test_where_gt_gte_lt_lte():
    with tempfile.TemporaryDirectory() as d:
        db = await _seed_numeric_table(d)
        try:
            assert {r.count for r in await db.find(count__gt=7)} == {8, 9}
            assert {r.count for r in await db.find(count__gte=7)} == {7, 8, 9}
            assert {r.count for r in await db.find(count__lt=2)} == {0, 1}
            assert {r.count for r in await db.find(count__lte=2)} == {0, 1, 2}
        finally:
            await db.close()


async def test_where_neq():
    with tempfile.TemporaryDirectory() as d:
        db = await _seed_numeric_table(d)
        try:
            rows = await db.find(count__neq=5)
            assert 5 not in {r.count for r in rows}
            assert len(rows) == 9
        finally:
            await db.close()


async def test_where_like():
    with tempfile.TemporaryDirectory() as d:
        db = await _seed_numeric_table(d)
        try:
            rows = await db.find(name__like="n0%")  # n00..n09
            assert len(rows) == 10
            rows = await db.find(name__like="%5")
            assert {r.name for r in rows} == {"n05"}
        finally:
            await db.close()


async def test_where_in():
    with tempfile.TemporaryDirectory() as d:
        db = await _seed_numeric_table(d)
        try:
            rows = await db.find(count__in=[1, 3, 5])
            assert {r.count for r in rows} == {1, 3, 5}
        finally:
            await db.close()


async def test_where_in_empty_list_returns_nothing():
    """SQLite accepts `col IN ()` as an empty set → 0 results.

    The pre-fix audit flagged this as a potential SQL syntax error but
    SQLite explicitly tolerates the empty IN — this test pins that
    behaviour so future refactors keep it.
    """
    with tempfile.TemporaryDirectory() as d:
        db = await _seed_numeric_table(d)
        try:
            assert await db.find(count__in=[]) == []
        finally:
            await db.close()


async def test_where_combined_filters_and():
    """Multiple kwargs combine with AND."""
    with tempfile.TemporaryDirectory() as d:
        db = await _seed_numeric_table(d)
        try:
            rows = await db.find(count__gte=3, count__lte=5)
            assert {r.count for r in rows} == {3, 4, 5}
        finally:
            await db.close()


async def test_invalid_column_raises():
    """Unknown column in filters → ValueError, not silent."""
    with tempfile.TemporaryDirectory() as d:
        db = await _seed_numeric_table(d)
        try:
            try:
                await db.find(bogus_column=1)
                raise AssertionError("expected ValueError")
            except ValueError as e:
                assert "bogus_column" in str(e)
        finally:
            await db.close()


# ────────────────────────────────────────────────────────────────────────
# 7. ORDER BY / LIMIT / OFFSET
# ────────────────────────────────────────────────────────────────────────

async def test_order_by_descending():
    with tempfile.TemporaryDirectory() as d:
        db = await _seed_numeric_table(d)
        try:
            rows = await db.find(order_by="-count")
            assert [r.count for r in rows] == list(range(9, -1, -1))
        finally:
            await db.close()


async def test_order_by_multi_column():
    """Multiple order_by entries: name DESC, count ASC."""
    with tempfile.TemporaryDirectory() as d:
        db = AsyncDB(Path(d) / "m.db", "items", WithEverything)
        try:
            await db.save(WithEverything(name="a", count=2))
            await db.save(WithEverything(name="a", count=1))
            await db.save(WithEverything(name="b", count=0))
            rows = await db.find(order_by=["-name", "count"])
            assert [(r.name, r.count) for r in rows] == [
                ("b", 0), ("a", 1), ("a", 2),
            ]
        finally:
            await db.close()


async def test_limit_zero_returns_empty():
    """LIMIT 0 is a valid SQLite request, returns 0 rows."""
    with tempfile.TemporaryDirectory() as d:
        db = await _seed_numeric_table(d)
        try:
            assert await db.find(limit=0) == []
        finally:
            await db.close()


async def test_offset_without_limit_uses_minus_one():
    """offset alone forces SQLite's `LIMIT -1 OFFSET N` (= all from N)."""
    with tempfile.TemporaryDirectory() as d:
        db = await _seed_numeric_table(d)
        try:
            rows = await db.find(order_by="count", offset=7)
            assert [r.count for r in rows] == [7, 8, 9]
        finally:
            await db.close()


# ────────────────────────────────────────────────────────────────────────
# 8. on_conflict semantics
# ────────────────────────────────────────────────────────────────────────

async def test_on_conflict_default_id_upserts():
    """No on_conflict → conflict on PRIMARY KEY id → save twice with same
    id updates instead of duplicating."""
    with tempfile.TemporaryDirectory() as d:
        db = AsyncDB(Path(d) / "x.db", "items", WithNaturalKey)
        try:
            item = WithNaturalKey(external_id="e", payload="v1")
            await db.save(item)
            item.payload = "v2"
            await db.save(item)
            rows = await db.fetch_all()
            assert len(rows) == 1 and rows[0].payload == "v2"
        finally:
            await db.close()


async def test_on_conflict_natural_key():
    """on_conflict='external_id': two saves with same external_id but
    different (auto-generated) ids → upsert by external_id, one row."""
    with tempfile.TemporaryDirectory() as d:
        db = AsyncDB(Path(d) / "x.db", "items", WithNaturalKey)
        try:
            await db.save(WithNaturalKey(external_id="same", payload="first"),
                          on_conflict='external_id')
            await db.save(WithNaturalKey(external_id="same", payload="second"),
                          on_conflict='external_id')
            rows = await db.fetch_all()
            assert len(rows) == 1 and rows[0].payload == "second"
        finally:
            await db.close()


async def test_on_conflict_unknown_column_raises():
    """on_conflict pointing to a non-PK/non-unique column → ValueError."""
    with tempfile.TemporaryDirectory() as d:
        db = AsyncDB(Path(d) / "x.db", "items", WithNaturalKey)
        try:
            try:
                await db.save(WithNaturalKey(external_id="e", payload="v"),
                              on_conflict='payload', skip_errors=False)
                raise AssertionError("expected ValueError")
            except ValueError as e:
                assert "payload" in str(e)
        finally:
            await db.close()


# ────────────────────────────────────────────────────────────────────────
# 9. Transaction rollback
# ────────────────────────────────────────────────────────────────────────

async def test_transaction_rolls_back_on_exception():
    """Write inside transaction() that raises → rollback (no commit)."""
    with tempfile.TemporaryDirectory() as d:
        db = AsyncDB(Path(d) / "x.db", "items", WithNaturalKey)
        try:
            await db.save(WithNaturalKey(external_id="seed", payload="0"))
            try:
                async with db.transaction() as conn:
                    await conn.execute(
                        text(
                            "INSERT INTO items (id, created_at, updated_at, "
                            "external_id, payload) VALUES "
                            "(:id, :ca, :ua, :eid, :payload)"
                        ),
                        {
                            "id": "new_id",
                            "ca": "2026-01-01",
                            "ua": "2026-01-01",
                            "eid": "new",
                            "payload": "1",
                        },
                    )
                    raise RuntimeError("force rollback")
            except RuntimeError:
                pass
            # The INSERT was rolled back — only the seed row remains.
            assert await db.count() == 1
        finally:
            await db.close()


async def test_transaction_read_only_skips_commit():
    """Read-only transaction never commits — `engine.connect()` issues no
    BEGIN, so SQLite never starts a txn for a pure SELECT sequence."""
    with tempfile.TemporaryDirectory() as d:
        db = AsyncDB(Path(d) / "x.db", "items", WithNaturalKey)
        try:
            await db.save(WithNaturalKey(external_id="a", payload="x"))
            async with db.transaction(read_only=True) as conn:
                result = await conn.execute(text("SELECT COUNT(*) FROM items"))
                row = result.fetchone()
                assert row[0] == 1
        finally:
            await db.close()


# ────────────────────────────────────────────────────────────────────────
# 10. exists / count / get_by_id / update_fields / delete
# ────────────────────────────────────────────────────────────────────────

async def test_exists_true_and_false():
    with tempfile.TemporaryDirectory() as d:
        db = AsyncDB(Path(d) / "x.db", "items", WithNaturalKey)
        try:
            await db.save(WithNaturalKey(external_id="e", payload="v"))
            assert await db.exists(external_id="e") is True
            assert await db.exists(external_id="missing") is False
        finally:
            await db.close()


async def test_count_with_filter():
    with tempfile.TemporaryDirectory() as d:
        db = await _seed_numeric_table(d)
        try:
            assert await db.count() == 10
            assert await db.count(count__gte=5) == 5
            assert await db.count(count=100) == 0
        finally:
            await db.close()


async def test_get_by_id_missing_returns_none():
    with tempfile.TemporaryDirectory() as d:
        db = AsyncDB(Path(d) / "x.db", "items", WithNaturalKey)
        try:
            assert await db.get_by_id("does-not-exist") is None
        finally:
            await db.close()


async def test_update_fields_missing_returns_false():
    with tempfile.TemporaryDirectory() as d:
        db = AsyncDB(Path(d) / "x.db", "items", WithNaturalKey)
        try:
            assert await db.update_fields("missing", payload="x") is False
        finally:
            await db.close()


async def test_update_fields_unknown_column_raises():
    with tempfile.TemporaryDirectory() as d:
        db = AsyncDB(Path(d) / "x.db", "items", WithNaturalKey)
        try:
            await db.save(WithNaturalKey(external_id="e", payload="v"))
            # Get the actual id of the saved row
            row = (await db.fetch_all())[0]
            try:
                await db.update_fields(row.id, bogus=1)
                raise AssertionError("expected ValueError")
            except ValueError as e:
                assert "bogus" in str(e)
        finally:
            await db.close()


async def test_delete_missing_returns_false():
    with tempfile.TemporaryDirectory() as d:
        db = AsyncDB(Path(d) / "x.db", "items", WithNaturalKey)
        try:
            assert await db.delete("missing") is False
        finally:
            await db.close()


async def test_delete_many_requires_filter():
    """delete_many() with no filters → ValueError (prevents accidental
    full-table wipe)."""
    with tempfile.TemporaryDirectory() as d:
        db = AsyncDB(Path(d) / "x.db", "items", WithNaturalKey)
        try:
            try:
                await db.delete_many()
                raise AssertionError("expected ValueError")
            except ValueError as e:
                assert "filter" in str(e).lower()
        finally:
            await db.close()


# ────────────────────────────────────────────────────────────────────────
# 11. Upsert SQL shape (compiled via SQLA, replaces _build_save_sql tests)
# ────────────────────────────────────────────────────────────────────────

async def test_upsert_compiled_sql_shape():
    """`_build_upsert` produces INSERT ... ON CONFLICT(id) DO UPDATE,
    with `created_at` excluded from the UPDATE SET clause so the
    original row's creation timestamp survives upserts.
    """
    with tempfile.TemporaryDirectory() as d:
        db = AsyncDB(Path(d) / "x.db", "items", WithNaturalKey)
        try:
            stmt = db._build_upsert(("id",))
            compiled = str(stmt.compile(dialect=sqlite_dialect.dialect()))
            assert "INSERT INTO items" in compiled
            assert "ON CONFLICT (id) DO UPDATE" in compiled
            # created_at preserved on UPDATE
            set_clause = compiled.split("DO UPDATE")[1]
            assert "created_at" not in set_clause
            assert "updated_at" in set_clause
        finally:
            await db.close()


async def test_upsert_compiled_sql_composite_conflict():
    """Composite conflict target produces ON CONFLICT (a, b) DO UPDATE."""
    @dataclass
    class Composite(BaseModel):
        a: str = field(default="", metadata={"unique": True})
        b: str = field(default="", metadata={"unique": True})

    with tempfile.TemporaryDirectory() as d:
        db = AsyncDB(Path(d) / "x.db", "items", Composite)
        try:
            stmt = db._build_upsert(("a", "b"))
            compiled = str(stmt.compile(dialect=sqlite_dialect.dialect()))
            assert "ON CONFLICT (a, b) DO UPDATE" in compiled
        finally:
            await db.close()


async def test_schema_with_sql_keyword_columns():
    """A15 fix: columns named after SQL reserved keywords (`when`,
    `order`, `group`, `select`, ...) must work end-to-end. Pre-fix the
    save would crash with `near "when": syntax error` because the
    identifier wasn't quoted in the INSERT/SELECT/WHERE statements.
    """
    @dataclass
    class WithKeywords(BaseModel):
        when: Optional[datetime] = None
        order: int = 0
        group: str = ""
        select: str = ""

    with tempfile.TemporaryDirectory() as d:
        db = AsyncDB(Path(d) / "x.db", "items", WithKeywords)
        try:
            now = datetime(2026, 1, 1, 12, 0, 0)
            await db.save(WithKeywords(when=now, order=5, group="a", select="x"))
            await db.save(WithKeywords(when=now, order=10, group="b", select="y"))

            # Read via every operator path to exercise quoting everywhere
            rows = await db.find(order__gt=3, group="b")
            assert len(rows) == 1 and rows[0].select == "y"

            rows = await db.find(order_by="-order")
            assert [r.order for r in rows] == [10, 5]

            assert await db.count(group="a") == 1
            assert await db.exists(select="y") is True

            # update_fields & delete with keyword columns
            r = (await db.find(group="a"))[0]
            assert await db.update_fields(r.id, order=99) is True
            assert (await db.get_by_id(r.id)).order == 99

            assert await db.delete_many(group="b") == 1
            assert await db.count() == 1
        finally:
            await db.close()


# Note: the prior `_build_save_sql` lru_cache test was removed — SQLA
# Core caches compiled statements via its own statement-cache mechanism
# keyed on the SQL construct's structure, not on AsyncDB identity.


# ────────────────────────────────────────────────────────────────────────
# 12. BaseModel.to_dict / from_dict — transport roundtrip
# ────────────────────────────────────────────────────────────────────────

def test_basemodel_bytes_roundtrip_via_dict():
    """BaseModel.to_dict encodes bytes with _B64_PREFIX; from_dict decodes."""
    item = WithOptBytes(blob=b"\xff\xfe\x00")
    d = item.to_dict()
    assert isinstance(d["blob"], str) and d["blob"].startswith(_B64_PREFIX)
    restored = WithOptBytes.from_dict(d)
    assert restored.blob == b"\xff\xfe\x00"


def test_basemodel_optional_bytes_none_roundtrip():
    """None survives to_dict/from_dict (no spurious b64 decode)."""
    item = WithOptBytes(blob=None)
    d = item.to_dict()
    assert d["blob"] is None
    restored = WithOptBytes.from_dict(d)
    assert restored.blob is None


def test_basemodel_from_dict_ignores_b64_prefix_on_non_bytes_fields():
    """A str field that happens to start with 'b64:' is NOT decoded —
    only fields whose declared type is bytes get the decode treatment."""
    d = {"id": "x", "created_at": "2026-01-01", "updated_at": "2026-01-01",
         "note": "b64:not-actually-bytes"}
    restored = WithOptStr.from_dict(d)
    assert restored.note == "b64:not-actually-bytes"


# ────────────────────────────────────────────────────────────────────────
# 13. Type mapping in _init_schema
# ────────────────────────────────────────────────────────────────────────

async def test_schema_column_types_match_python_types():
    """Verify each Python type maps to a SQLite column type with the right
    affinity. SQLA's renderings: bool→BOOLEAN (NUMERIC affinity),
    float→FLOAT (REAL affinity), datetime→DATETIME (NUMERIC affinity).
    All functionally equivalent to the pre-refactor INTEGER/REAL/TIMESTAMP
    declarations; SQLite is dynamically typed and goes by affinity, not
    declared name.
    """
    with tempfile.TemporaryDirectory() as d:
        db = AsyncDB(Path(d) / "x.db", "items", WithEverything)
        await db.save(WithEverything(name="t"))
        # Inspect the live schema via a raw sqlite3 connection — the
        # asserts target the *declared* SQL types in sqlite_master.
        c = sqlite3.connect(Path(d) / "x.db")
        info = {row[1]: row[2] for row in c.execute("PRAGMA table_info(items)")}
        c.close()
        await db.close()
        assert info["name"] == "TEXT"
        assert info["count"] == "INTEGER"
        assert info["flag"] == "BOOLEAN"
        assert info["weight"] == "FLOAT"
        assert info["payload"] == "BLOB"
        # datetime → custom UTC decorator → TEXT (was "DATETIME" before
        # the UTC fix; the decorator stores ISO-with-offset because SQLA's
        # stock DateTime(timezone=True) loses the offset on read).
        assert info["when"] == "TEXT"
        assert info["color"] == "TEXT"        # Enum → TEXT
        assert info["priority"] == "INTEGER"  # IntEnum → INTEGER
        assert info["tags"] == "TEXT"         # list → JSON-encoded TEXT
        assert info["extra"] == "TEXT"        # dict → JSON-encoded TEXT


# ────────────────────────────────────────────────────────────────────────
# 14. Schema migration (ALTER TABLE ADD COLUMN)
# ────────────────────────────────────────────────────────────────────────

@dataclass
class V1(BaseModel):
    name: str = ""


@dataclass
class V2(BaseModel):
    name: str = ""
    description: str = ""   # new field
    category: str = field(default="", metadata={"index": True})  # new + indexed


async def test_schema_migration_adds_columns_and_indexes():
    """Saving with V2 schema on a V1 database adds the missing columns
    and indexes without destroying existing data."""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "x.db"
        # Phase 1: V1 schema, insert some data
        db1 = AsyncDB(p, "items", V1)
        try:
            await db1.save(V1(name="alpha"))
            await db1.save(V1(name="beta"))
        finally:
            await db1.close()

        # Phase 2: reopen with V2 schema — should ALTER TABLE the new columns
        from esuls.db_cli import _db_state_by_loop
        for s in _db_state_by_loop.values():
            s["initialized"].clear()

        db2 = AsyncDB(p, "items", V2)
        try:
            rows = await db2.fetch_all()
            assert len(rows) == 2
            # Existing rows have empty defaults for the new columns
            names = {r.name for r in rows}
            assert names == {"alpha", "beta"}
            # And new saves can use the new columns
            await db2.save(V2(name="gamma", description="desc", category="C"))
            rows = await db2.find(category="C")
            assert len(rows) == 1 and rows[0].description == "desc"
        finally:
            await db2.close()


# ────────────────────────────────────────────────────────────────────────
# 15. Connection pooling per (db_path, loop)
# ────────────────────────────────────────────────────────────────────────

async def test_write_lock_shared_across_instances_same_path():
    """Two AsyncDB instances on the same db_path within the same loop
    must share the *same* write lock — otherwise the in-process write
    serialisation breaks and SQLite's busy_timeout has to bail us out."""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "shared.db"
        db_a = AsyncDB(p, "items", WithNaturalKey)
        db_b = AsyncDB(p, "items", WithNaturalKey)
        try:
            lock_a = await db_a._get_write_lock()
            lock_b = await db_b._get_write_lock()
            assert lock_a is lock_b
        finally:
            await db_a.close()
            await db_b.close()


async def test_write_lock_distinct_across_different_paths():
    """Two AsyncDB instances on different paths must NOT share a lock —
    otherwise writes to one db would needlessly block writes to the other."""
    with tempfile.TemporaryDirectory() as d:
        db_a = AsyncDB(Path(d) / "a.db", "items", WithNaturalKey)
        db_b = AsyncDB(Path(d) / "b.db", "items", WithNaturalKey)
        try:
            lock_a = await db_a._get_write_lock()
            lock_b = await db_b._get_write_lock()
            assert lock_a is not lock_b
        finally:
            await db_a.close()
            await db_b.close()


# ────────────────────────────────────────────────────────────────────────
# 16. WAL + busy_timeout + cache_size applied
# ────────────────────────────────────────────────────────────────────────

async def test_wal_pragmas_applied():
    """The connection is opened with the expected PRAGMA settings."""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "x.db"
        db = AsyncDB(p, "items", WithNaturalKey)
        try:
            await db.save(WithNaturalKey(external_id="x", payload="v"))
            async with db.transaction(read_only=True) as conn:
                row = (await conn.execute(text("PRAGMA journal_mode"))).fetchone()
                assert row[0].lower() == "wal"
                row = (await conn.execute(text("PRAGMA synchronous"))).fetchone()
                assert row[0] == 1  # NORMAL
                row = (await conn.execute(text("PRAGMA busy_timeout"))).fetchone()
                assert row[0] == 30000
        finally:
            await db.close()


# ────────────────────────────────────────────────────────────────────────
# 17. Context manager
# ────────────────────────────────────────────────────────────────────────

async def test_context_manager_closes_connection():
    """`async with AsyncDB(...) as db:` disposes the engines on exit."""
    from esuls.db_cli import _db_state_by_loop
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "x.db"
        key = str(p.resolve())
        async with AsyncDB(p, "items", WithNaturalKey) as db:
            await db.save(WithNaturalKey(external_id="x", payload="v"))
            state = _db_state_by_loop[asyncio.get_running_loop()]
            assert key in state["engines"]
        # After __aexit__, the engine pair has been removed.
        state = _db_state_by_loop[asyncio.get_running_loop()]
        assert key not in state["engines"]


# ────────────────────────────────────────────────────────────────────────
# Runner
# ────────────────────────────────────────────────────────────────────────

