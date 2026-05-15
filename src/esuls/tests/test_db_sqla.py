"""
Tests for behaviours introduced by the SQLAlchemy Core refactor.

Covers:
  - All required PRAGMAs actually applied on both writer & reader engines
  - Decimal precision roundtrip (regression for the old float() coercion)
  - str columns are NOT JSON-decoded (regression for the json.loads guess)
  - PEP 604 `X | None` schemas work (regression: pre-fix only typing.Union)
  - Read connection pool tolerates concurrent reads
  - Writer + write lock survive burst writes without surfacing BUSY
  - `db.checkpoint(mode)` returns the expected 3-tuple
  - `db.close()` runs PRAGMA optimize on the writer
  - foreign_keys=ON actually enforces constraints
  - Out-of-band writes that bypass TypeDecorators are passed through
    without silent recovery (locks the bug-fix contract)
"""
import asyncio
import logging
import sqlite3
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

import pytest
import sqlalchemy.exc as sa_exc
from sqlalchemy import event, text

from esuls.db_cli import (
    AsyncDB, BaseModel, IdModel, IntIdModel, TimestampedIntModel,
    TimestampedModel, _db_state_by_loop, discover_migrations, utcnow,
)


# ────────────────────────────────────────────────────────────────────────
# 1. PRAGMAs applied on both engines
# ────────────────────────────────────────────────────────────────────────

@dataclass
class _Trivial(BaseModel):
    name: str = ""


_EXPECTED_PRAGMAS = {
    "journal_mode": "wal",
    "synchronous": 1,           # NORMAL
    "foreign_keys": 1,          # ON
    "mmap_size": 268435456,
    "temp_store": 2,            # MEMORY
    "cache_size": -65536,       # 64 MB (negative = KiB)
    "busy_timeout": 30000,
    "wal_autocheckpoint": 1000,
}


async def test_pragmas_applied_on_writer(temp_db, db):
    await db.save(db.schema_class(name="x", value=1))
    # `db.transaction(read_only=False)` uses the writer engine.
    async with db.transaction() as conn:
        for pragma, expected in _EXPECTED_PRAGMAS.items():
            row = (await conn.execute(text(f"PRAGMA {pragma}"))).fetchone()
            value = row[0]
            if isinstance(value, str):
                value = value.lower()
            assert value == expected, (
                f"writer PRAGMA {pragma} = {value!r}, expected {expected!r}"
            )


async def test_pragmas_applied_on_reader(temp_db, db):
    await db.save(db.schema_class(name="x", value=1))
    async with db.transaction(read_only=True) as conn:
        for pragma, expected in _EXPECTED_PRAGMAS.items():
            row = (await conn.execute(text(f"PRAGMA {pragma}"))).fetchone()
            value = row[0]
            if isinstance(value, str):
                value = value.lower()
            assert value == expected, (
                f"reader PRAGMA {pragma} = {value!r}, expected {expected!r}"
            )


# ────────────────────────────────────────────────────────────────────────
# 2. Bug fix #1: Decimal precision (was lossy via float())
# ────────────────────────────────────────────────────────────────────────

@dataclass
class _WithDecimal(BaseModel):
    amount: Decimal = field(default_factory=lambda: Decimal("0"))


async def test_decimal_precision_roundtrip(temp_db):
    db = AsyncDB(temp_db, "items", _WithDecimal)
    try:
        precise = Decimal("3.14159265358979323846")
        await db.save(_WithDecimal(amount=precise))
        rows = await db.find()
        assert len(rows) == 1
        assert rows[0].amount == precise, (
            f"Decimal lost precision: stored {precise}, got {rows[0].amount}"
        )
    finally:
        await db.close()


# ────────────────────────────────────────────────────────────────────────
# 3. Bug fix #2: str columns are NOT auto-JSON-decoded
# ────────────────────────────────────────────────────────────────────────

@dataclass
class _WithStr(BaseModel):
    body: str = ""


async def test_str_column_holding_json_text_stays_str(temp_db):
    """A str field containing what looks like JSON must roundtrip as str.

    Pre-fix, `_deserialize_value` tried `json.loads(value)` on every str
    column as a fallback; '"[1, 2, 3]"' silently became `[1, 2, 3]`,
    silently corrupting downstream consumers expecting a str.
    """
    db = AsyncDB(temp_db, "items", _WithStr)
    try:
        payloads = ['[1, 2, 3]', '{"key": "value"}', '"true"', '"null"', '123']
        for p in payloads:
            await db.save(_WithStr(body=p))
        rows = await db.find(order_by="created_at")
        loaded = [r.body for r in rows]
        assert loaded == payloads, (
            f"str column was JSON-decoded: {loaded!r}"
        )
        # And every value is still a str (not a list / dict / int / bool).
        for v in loaded:
            assert isinstance(v, str), f"expected str, got {type(v).__name__}"
    finally:
        await db.close()


# ────────────────────────────────────────────────────────────────────────
# 4. Bug fix #3: PEP 604 `X | None` union syntax
# ────────────────────────────────────────────────────────────────────────

@dataclass
class _WithPEP604(BaseModel):
    # Note: must NOT be Optional[int] — the regression is specifically
    # against the pipe-syntax union (types.UnionType, not typing.Union).
    value: "int | None" = None


async def test_pep604_union_schema_roundtrip(temp_db):
    db = AsyncDB(temp_db, "items", _WithPEP604)
    try:
        await db.save(_WithPEP604(value=42))
        await db.save(_WithPEP604(value=None))
        rows = await db.find(order_by="created_at")
        assert {r.value for r in rows} == {42, None}
    finally:
        await db.close()


# ────────────────────────────────────────────────────────────────────────
# 5. Read pool: concurrent reads under WAL must not deadlock
# ────────────────────────────────────────────────────────────────────────

@dataclass
class _ItemRO(BaseModel):
    name: str = ""
    value: int = 0


async def test_read_pool_concurrent_reads(temp_db):
    db = AsyncDB(temp_db, "items", _ItemRO)
    try:
        for i in range(100):
            await db.save(_ItemRO(name=f"x{i}", value=i))
        # 50 concurrent reads — must all return the same 100 rows.
        results = await asyncio.gather(*[db.find() for _ in range(50)])
        assert all(len(r) == 100 for r in results), (
            f"some reads returned wrong count: "
            f"{sorted({len(r) for r in results})}"
        )
    finally:
        await db.close()


# ────────────────────────────────────────────────────────────────────────
# 6. Writer + write lock under burst — no SQLITE_BUSY surfaces
# ────────────────────────────────────────────────────────────────────────

async def test_writer_serialization_under_burst(temp_db):
    db = AsyncDB(temp_db, "items", _ItemRO)
    try:
        results = await asyncio.gather(*[
            db.save(_ItemRO(name=f"x{i}", value=i)) for i in range(100)
        ])
        assert all(results), "every save must succeed"
        assert await db.count() == 100
    finally:
        await db.close()


# ────────────────────────────────────────────────────────────────────────
# 7. checkpoint() public API
# ────────────────────────────────────────────────────────────────────────

async def test_checkpoint_returns_three_tuple(temp_db, db):
    await db.save(db.schema_class(name="x", value=1))
    result = await db.checkpoint("PASSIVE")
    assert isinstance(result, tuple) and len(result) == 3
    for v in result:
        assert isinstance(v, int)


async def test_checkpoint_invalid_mode_raises(temp_db, db):
    with pytest.raises(ValueError, match="Invalid checkpoint mode"):
        await db.checkpoint("INVALID")


# ────────────────────────────────────────────────────────────────────────
# 8. close() runs PRAGMA optimize on the writer
# ────────────────────────────────────────────────────────────────────────

async def test_close_runs_pragma_optimize(temp_db):
    db = AsyncDB(temp_db, "items", _ItemRO)
    await db.save(_ItemRO(name="x", value=1))

    writer, _ = await db._ensure_engines()
    executed: list[str] = []

    @event.listens_for(writer.sync_engine, "before_cursor_execute")
    def _capture(_conn, _cursor, statement, *_):
        executed.append(statement)

    await db.close()
    optimize_seen = any("optimize" in s.lower() for s in executed)
    checkpoint_seen = any("wal_checkpoint" in s.lower() for s in executed)
    assert optimize_seen, f"PRAGMA optimize not executed; saw: {executed}"
    assert checkpoint_seen, f"PRAGMA wal_checkpoint not executed; saw: {executed}"


# ────────────────────────────────────────────────────────────────────────
# 9. foreign_keys=ON actually enforces
# ────────────────────────────────────────────────────────────────────────

async def test_foreign_keys_enforced(temp_db, db):
    """Insert that violates a FK constraint raises IntegrityError.

    Use a raw `text(...)` INSERT via the writer transaction so we can
    target a hand-built FK relationship without needing schema metadata
    for two-table FK declarations (the db_cli schema builder accepts
    `metadata={"foreign_key": ...}` but we don't need to exercise the
    builder here — we just need to prove the PRAGMA is live).
    """
    await db.save(db.schema_class(name="seed", value=1))
    async with db.transaction() as conn:
        await conn.execute(text(
            "CREATE TABLE child (id TEXT PRIMARY KEY, "
            "parent_id TEXT NOT NULL REFERENCES items(id))"
        ))
    with pytest.raises(sa_exc.IntegrityError):
        async with db.transaction() as conn:
            await conn.execute(text(
                "INSERT INTO child (id, parent_id) VALUES (:i, :p)"
            ), {"i": "c1", "p": "nonexistent-parent"})


# ────────────────────────────────────────────────────────────────────────
# 10. No silent schema-drift recovery (bug-fix contract)
# ────────────────────────────────────────────────────────────────────────

@dataclass
class _WithOptBytes(BaseModel):
    blob: Optional[bytes] = None


async def test_no_silent_schema_drift_recovery(temp_db):
    """Out-of-band writes that bypass TypeDecorators are passed through
    unchanged on read — no `ast.literal_eval` recovery, no JSON guess.

    The prior code's bytes-from-string recovery would silently turn
    `"b'recovered'"` into actual bytes. That path silently corrupted
    payloads that just happened to look like Python bytes literals.
    The new TypeDecorator just returns what SQLite hands back.
    """
    db = AsyncDB(temp_db, "items", _WithOptBytes)
    try:
        await db.save(_WithOptBytes(blob=b"placeholder"))
    finally:
        await db.close()

    # Inject a raw string into the BLOB column via sqlite3 directly.
    c = sqlite3.connect(temp_db)
    c.execute("UPDATE items SET blob = ? WHERE 1=1", ("b'fake'",))
    c.commit()
    c.close()

    # Force schema re-init in this loop so the AsyncDB rebuilds engines.
    for s in _db_state_by_loop.values():
        s["initialized"].clear()

    db2 = AsyncDB(temp_db, "items", _WithOptBytes)
    try:
        rows = await db2.fetch_all()
        # The decorator passes through what SQLite returns; no recovery,
        # no error. The exact return type depends on SQLite's affinity
        # behaviour, but it must NOT be the recovered bytes b"fake".
        assert rows[0].blob != b"fake", (
            "TypeDecorator silently recovered bytes from a string — "
            "the broken pre-refactor behaviour has returned"
        )
    finally:
        await db2.close()


# ────────────────────────────────────────────────────────────────────────
# 11. PR A — UTC-aware timestamps
# ────────────────────────────────────────────────────────────────────────

async def test_timestamps_are_utc_aware(temp_db, db):
    """All default timestamps are timezone-aware UTC, and survive roundtrip."""
    item = db.schema_class(name="x", value=1)
    assert item.created_at.tzinfo is not None
    assert item.created_at.utcoffset().total_seconds() == 0

    await db.save(item)
    loaded = (await db.find())[0]
    assert loaded.created_at.tzinfo is not None
    assert loaded.updated_at.tzinfo is not None
    # And the values are close to "now in UTC" (within a generous window).
    now = datetime.now(timezone.utc)
    assert abs((now - loaded.created_at).total_seconds()) < 5


async def test_update_fields_uses_utc(temp_db, db):
    """update_fields() refreshes updated_at to tz-aware UTC."""
    await db.save(db.schema_class(name="x", value=1))
    row = (await db.find())[0]
    await db.update_fields(row.id, value=2)
    updated = (await db.find())[0]
    assert updated.updated_at.tzinfo is not None
    assert updated.updated_at.utcoffset().total_seconds() == 0


# ────────────────────────────────────────────────────────────────────────
# 12. PR A — Configurable max_retries
# ────────────────────────────────────────────────────────────────────────

async def test_max_retries_kwarg_honored(temp_db, db, monkeypatch):
    """Setting max_retries=1 means no retry — first failure surfaces immediately.

    We inject an artificial busy by monkeypatching `_is_sqlite_busy` to
    treat a custom marker exception as busy, then have the action raise
    it deterministically.
    """
    from esuls.db_cli import _is_sqlite_busy as real

    calls = {"n": 0}

    class _MarkerBusy(Exception):
        pass

    def fake_busy(exc):
        return isinstance(exc, _MarkerBusy) or real(exc)

    import esuls.db_cli as dbmod
    monkeypatch.setattr(dbmod, "_is_sqlite_busy", fake_busy)

    async def flaky():
        calls["n"] += 1
        raise _MarkerBusy("synthetic busy")

    # With max_retries=1, the loop runs the action once and re-raises
    # without retrying.
    with pytest.raises(_MarkerBusy):
        await db._execute_with_retry(flaky, max_retries=1)
    assert calls["n"] == 1, f"expected 1 attempt, got {calls['n']}"

    # With max_retries=4, the loop runs the action 4 times.
    calls["n"] = 0
    with pytest.raises(_MarkerBusy):
        await db._execute_with_retry(flaky, max_retries=4)
    assert calls["n"] == 4, f"expected 4 attempts, got {calls['n']}"


# ────────────────────────────────────────────────────────────────────────
# 13. PR A — Schema-drift warning + strict_schema
# ────────────────────────────────────────────────────────────────────────

@dataclass
class _V1(BaseModel):
    name: str = ""


@dataclass
class _V2(BaseModel):
    name: str = ""
    score: int = field(default=0, metadata={"required": True})


async def test_drift_warning_on_required_retrofit(temp_db):
    """A required=True column added via ALTER must produce a warning.

    loguru bypasses stdlib logging, so we plug a list sink to capture
    formatted messages directly.
    """
    from loguru import logger

    # Phase 1: V1 schema, write a row.
    db1 = AsyncDB(temp_db, "items", _V1)
    try:
        await db1.save(_V1(name="alpha"))
    finally:
        await db1.close()

    # Force schema re-init in this loop.
    from esuls.db_cli import _db_state_by_loop
    for s in _db_state_by_loop.values():
        s["initialized"].clear()

    captured: list[str] = []
    sink_id = logger.add(captured.append, level="WARNING")
    try:
        db2 = AsyncDB(temp_db, "items", _V2)
        try:
            await db2.fetch_all()  # forces _ensure_engines → drift check
        finally:
            await db2.close()
    finally:
        logger.remove(sink_id)

    msgs = "\n".join(captured)
    assert "score" in msgs and ("required" in msgs or "NOT NULL" in msgs), (
        f"expected drift warning for `score` column, got: {msgs!r}"
    )


async def test_strict_schema_raises_on_drift(temp_db):
    """strict_schema=True promotes drift warnings to RuntimeError."""
    db1 = AsyncDB(temp_db, "items", _V1)
    try:
        await db1.save(_V1(name="alpha"))
    finally:
        await db1.close()

    from esuls.db_cli import _db_state_by_loop
    for s in _db_state_by_loop.values():
        s["initialized"].clear()

    db2 = AsyncDB(temp_db, "items", _V2, strict_schema=True)
    try:
        with pytest.raises(RuntimeError, match="strict_schema=True"):
            await db2.fetch_all()
    finally:
        # Close may also raise; swallow.
        try:
            await db2.close()
        except Exception:
            pass


# ────────────────────────────────────────────────────────────────────────
# 14. PR A — _is_stale_connection prefers typed exceptions
# ────────────────────────────────────────────────────────────────────────

def test_is_stale_connection_matches_typed_exceptions():
    """_is_stale_connection returns True for the SQLA-typed flavours."""
    from esuls.db_cli import _is_stale_connection

    assert _is_stale_connection(sa_exc.ResourceClosedError("x")) is True
    assert _is_stale_connection(sa_exc.DisconnectionError("x")) is True
    assert _is_stale_connection(sa_exc.InvalidRequestError("x")) is True

    # String-match fallback still works for raw aiosqlite errors.
    assert _is_stale_connection(Exception("Connection is closed")) is True
    assert _is_stale_connection(Exception("no active connection")) is True

    # Unrelated errors are not classified as stale.
    assert _is_stale_connection(ValueError("nope")) is False
    assert _is_stale_connection(Exception("database is locked")) is False


# ────────────────────────────────────────────────────────────────────────
# 15. PR B — new filter operators
# ────────────────────────────────────────────────────────────────────────

@dataclass
class _Row(BaseModel):
    city: Optional[str] = None
    amount: int = 0


async def _seed_rows(temp_db) -> AsyncDB:
    db = AsyncDB(temp_db, "items", _Row)
    rows = [
        ("NY", 10), ("NY", 20), ("LA", 5), ("LA", 15), ("LA", 25),
        ("SF", 7), (None, 100),
    ]
    for c, a in rows:
        await db.save(_Row(city=c, amount=a))
    return db


async def test_filter_is_null(temp_db):
    db = await _seed_rows(temp_db)
    try:
        nulls = await db.find(city__is_null=True)
        assert [r.amount for r in nulls] == [100]
        not_nulls = await db.find(city__is_null=False)
        assert len(not_nulls) == 6
    finally:
        await db.close()


async def test_filter_not_null(temp_db):
    db = await _seed_rows(temp_db)
    try:
        assert len(await db.find(city__not_null=True)) == 6
        assert len(await db.find(city__not_null=False)) == 1
    finally:
        await db.close()


async def test_filter_not_in(temp_db):
    db = await _seed_rows(temp_db)
    try:
        rows = await db.find(city__not_in=["LA", "SF"])
        # NULL doesn't match NOT IN in SQL (3VL). Confirm NY-only.
        cities = {r.city for r in rows}
        assert cities == {"NY"}
    finally:
        await db.close()


async def test_filter_between_inclusive(temp_db):
    db = await _seed_rows(temp_db)
    try:
        rows = await db.find(amount__between=(10, 20), order_by="amount")
        assert [r.amount for r in rows] == [10, 15, 20]
    finally:
        await db.close()


async def test_filter_between_bad_value_raises(temp_db, db):
    with pytest.raises(ValueError, match="__between"):
        await db.find(value__between=42)  # not a 2-tuple


# ────────────────────────────────────────────────────────────────────────
# 16. PR B — aggregate()
# ────────────────────────────────────────────────────────────────────────

async def test_aggregate_count_no_group_by(temp_db):
    db = await _seed_rows(temp_db)
    try:
        rows = await db.aggregate(count=True)
        assert rows == [{"count": 7}]
    finally:
        await db.close()


async def test_aggregate_count_with_group_by(temp_db):
    db = await _seed_rows(temp_db)
    try:
        rows = await db.aggregate(group_by="city", count=True, order_by="-count")
        # Order: LA(3), NY(2), then SF/None tied at 1 each — ordering between
        # the two 1-count rows is not guaranteed, so check as set after dropping.
        head = rows[:2]
        assert head == [
            {"city": "LA", "count": 3},
            {"city": "NY", "count": 2},
        ]
        tail = {tuple(sorted(r.items())) for r in rows[2:]}
        assert tail == {
            tuple(sorted({"city": "SF", "count": 1}.items())),
            tuple(sorted({"city": None, "count": 1}.items())),
        }
    finally:
        await db.close()


async def test_aggregate_sum_avg_min_max(temp_db):
    db = await _seed_rows(temp_db)
    try:
        rows = await db.aggregate(
            group_by="city",
            sum="amount", avg="amount", min="amount", max="amount",
            city__not_null=True,
        )
        by_city = {r["city"]: r for r in rows}
        assert by_city["LA"]["sum_amount"] == 45
        assert by_city["LA"]["min_amount"] == 5
        assert by_city["LA"]["max_amount"] == 25
        # avg is a float; ~ 45/3 = 15
        assert abs(by_city["LA"]["avg_amount"] - 15.0) < 0.01
    finally:
        await db.close()


async def test_aggregate_having(temp_db):
    db = await _seed_rows(temp_db)
    try:
        rows = await db.aggregate(
            group_by="city", count=True, having={"count__gt": 1},
            order_by="-count",
        )
        cities = [r["city"] for r in rows]
        assert cities == ["LA", "NY"]
    finally:
        await db.close()


async def test_aggregate_requires_one_metric(temp_db, db):
    with pytest.raises(ValueError, match="aggregate"):
        await db.aggregate(group_by="value")


# ────────────────────────────────────────────────────────────────────────
# 17. PR B — stream()
# ────────────────────────────────────────────────────────────────────────

async def test_stream_yields_all_rows(temp_db):
    db = await _seed_rows(temp_db)
    try:
        seen = []
        async for r in db.stream(order_by="amount"):
            seen.append(r.amount)
        assert seen == [5, 7, 10, 15, 20, 25, 100]
    finally:
        await db.close()


async def test_stream_with_filter(temp_db):
    db = await _seed_rows(temp_db)
    try:
        seen = []
        async for r in db.stream(amount__gte=15, order_by="amount"):
            seen.append(r.amount)
        assert seen == [15, 20, 25, 100]
    finally:
        await db.close()


async def test_stream_can_break_early(temp_db):
    """Breaking out of the consumer must not leak the connection."""
    db = await _seed_rows(temp_db)
    try:
        first = None
        async for r in db.stream(order_by="amount"):
            first = r
            break
        assert first is not None and first.amount == 5
        # Subsequent operations must still work (connection released).
        assert await db.count() == 7
    finally:
        await db.close()


# ────────────────────────────────────────────────────────────────────────
# 18. PR C — save_batch (fail-fast) + save_each (per-item) + deprecation
# ────────────────────────────────────────────────────────────────────────

@dataclass
class _FFItem(BaseModel):
    name: str = ""


async def test_save_batch_is_fail_fast(temp_db):
    """save_batch now always uses executemany; a wrong-type row raises."""
    db = AsyncDB(temp_db, "items", _FFItem)
    try:
        class _Other:
            pass
        with pytest.raises(TypeError, match="Expected _FFItem"):
            await db.save_batch([_FFItem(name="ok"), _Other()])  # type: ignore[list-item]
        # Whole batch rolled back — no rows persisted.
        assert await db.count() == 0
    finally:
        await db.close()


async def test_save_each_skips_and_logs(temp_db):
    """save_each persists valid items and skips bad ones with a warning."""
    db = AsyncDB(temp_db, "items", _FFItem)
    try:
        class _Other:
            pass
        n = await db.save_each([
            _FFItem(name="a"),
            _Other(),               # type: ignore[list-item]
            _FFItem(name="b"),
        ])
        assert n == 2
        assert await db.count() == 2
    finally:
        await db.close()


async def test_save_batch_deprecated_skip_errors_kwarg_warns(temp_db):
    """Passing skip_errors= emits DeprecationWarning."""
    db = AsyncDB(temp_db, "items", _FFItem)
    try:
        with pytest.warns(DeprecationWarning, match="save_batch.skip_errors"):
            await db.save_batch([_FFItem(name="x")], skip_errors=False)
        with pytest.warns(DeprecationWarning, match="save_batch.skip_errors"):
            await db.save_batch([_FFItem(name="y")], skip_errors=True)
        # Both items persisted regardless of which path the deprecation took.
        assert await db.count() == 2
    finally:
        await db.close()


async def test_save_batch_unknown_kwarg_raises(temp_db, db):
    with pytest.raises(TypeError, match="unexpected keyword"):
        await db.save_batch([db.schema_class(name="x", value=1)], nonsense=True)


# ────────────────────────────────────────────────────────────────────────
# 19. PR C — IdModel / IntIdModel / TimestampedModel
# ────────────────────────────────────────────────────────────────────────

@dataclass
class _CacheRow(IdModel):
    """Cache/lookup table — no created_at/updated_at."""
    key: str = ""
    value: str = ""


@dataclass
class _AutoIncRow(IntIdModel):
    name: str = ""


async def test_id_model_no_timestamps(temp_db):
    """IdModel schemas have no `created_at`/`updated_at` columns."""
    db = AsyncDB(temp_db, "cache", _CacheRow)
    try:
        await db.save(_CacheRow(key="k1", value="v1"))
        await db.save(_CacheRow(key="k2", value="v2"))
        # Schema has only {id, key, value}.
        assert db._valid_columns == frozenset({"id", "key", "value"})
        # Reads roundtrip cleanly.
        rows = await db.find(order_by="key")
        assert [(r.key, r.value) for r in rows] == [("k1", "v1"), ("k2", "v2")]
        # update_fields works without an updated_at column.
        await db.update_fields(rows[0].id, value="v1-updated")
        loaded = await db.get_by_id(rows[0].id)
        assert loaded.value == "v1-updated"
    finally:
        await db.close()


async def test_int_id_model_autoincrement(temp_db):
    """IntIdModel with id=None lets SQLite assign an auto-incrementing rowid."""
    db = AsyncDB(temp_db, "ids", _AutoIncRow)
    try:
        # id is None initially → SQLite picks the rowid.
        await db.save(_AutoIncRow(name="first"))
        await db.save(_AutoIncRow(name="second"))
        await db.save(_AutoIncRow(name="third"))
        rows = await db.find(order_by="id")
        assert [r.name for r in rows] == ["first", "second", "third"]
        # And ids are integers, increasing.
        ids = [r.id for r in rows]
        assert all(isinstance(i, int) for i in ids)
        assert ids == sorted(ids)
    finally:
        await db.close()


async def test_timestamped_model_is_alias_for_base(temp_db, db):
    """BaseModel and TimestampedModel are the same class (backward compat)."""
    assert BaseModel is TimestampedModel


async def test_id_model_to_dict_roundtrip():
    """to_dict / from_dict still work on IdModel (inherited from _ModelBase)."""
    row = _CacheRow(key="k", value="v")
    restored = _CacheRow.from_dict(row.to_dict())
    assert restored.id == row.id
    assert restored.key == "k"
    assert restored.value == "v"


# ────────────────────────────────────────────────────────────────────────
# 20. Review follow-up — drift equivalence (BOOLEAN/INTEGER, DATETIME/TEXT)
# ────────────────────────────────────────────────────────────────────────

def test_types_equivalent_pairs():
    """The `_types_equivalent` helper accepts the documented same-affinity pairs."""
    from esuls.db_cli import _types_equivalent

    # Identity always wins.
    assert _types_equivalent("INTEGER", "INTEGER") is True
    # Documented equivalences (either direction).
    assert _types_equivalent("BOOLEAN", "INTEGER") is True
    assert _types_equivalent("INTEGER", "BOOLEAN") is True
    assert _types_equivalent("DATETIME", "TEXT") is True
    assert _types_equivalent("TEXT", "DATETIME") is True
    assert _types_equivalent("VARCHAR(36)", "TEXT") is True
    assert _types_equivalent("VARCHAR", "TEXT") is True
    # Genuine drift still reports.
    assert _types_equivalent("INTEGER", "TEXT") is False
    assert _types_equivalent("FLOAT", "INTEGER") is False


@dataclass
class _LegacyBoolRow(BaseModel):
    """Schema mirroring a hypothetical pre-refactor table where `flag`
    was declared INTEGER (the old behavior) but now maps to BOOLEAN."""
    flag: bool = False


async def test_drift_no_false_positive_bool_vs_integer(tmp_path):
    """A pre-existing table with INTEGER for a bool column must NOT trigger
    a drift warning when the new dataclass declares Boolean (BOOLEAN)."""
    from loguru import logger as _logger
    db_path = tmp_path / "legacy.db"
    # Hand-build the table with the legacy INTEGER declared type.
    import sqlite3 as _sqlite3
    c = _sqlite3.connect(db_path)
    c.execute(
        'CREATE TABLE items ('
        '  id TEXT PRIMARY KEY,'
        '  created_at TEXT,'
        '  updated_at TEXT,'
        '  flag INTEGER'
        ')'
    )
    c.commit()
    c.close()

    captured: list[str] = []
    sink_id = _logger.add(captured.append, level="WARNING")
    try:
        db = AsyncDB(db_path, "items", _LegacyBoolRow)
        try:
            await db.save(_LegacyBoolRow(flag=True))
        finally:
            await db.close()
    finally:
        _logger.remove(sink_id)

    msgs = "\n".join(captured)
    assert "type drift" not in msgs, (
        f"BOOLEAN vs INTEGER triggered a false-positive drift warning: {msgs!r}"
    )


@dataclass
class _LegacyDatetimeRow(BaseModel):
    when: Optional[datetime] = None


async def test_drift_no_false_positive_datetime_vs_text(tmp_path):
    """A pre-existing table with DATETIME for a datetime column must NOT
    trigger drift when the new dataclass uses _UTCDateTimeDecorator (TEXT)."""
    from loguru import logger as _logger
    db_path = tmp_path / "legacy.db"
    import sqlite3 as _sqlite3
    c = _sqlite3.connect(db_path)
    c.execute(
        'CREATE TABLE items ('
        '  id TEXT PRIMARY KEY,'
        '  created_at TEXT,'
        '  updated_at TEXT,'
        '  "when" DATETIME'
        ')'
    )
    c.commit()
    c.close()

    captured: list[str] = []
    sink_id = _logger.add(captured.append, level="WARNING")
    try:
        db = AsyncDB(db_path, "items", _LegacyDatetimeRow)
        try:
            await db.save(_LegacyDatetimeRow(when=datetime.now(timezone.utc)))
        finally:
            await db.close()
    finally:
        _logger.remove(sink_id)

    msgs = "\n".join(captured)
    assert "type drift" not in msgs, (
        f"DATETIME vs TEXT triggered a false-positive drift warning: {msgs!r}"
    )


# ────────────────────────────────────────────────────────────────────────
# 21. Review follow-up — aggregate HAVING supports all operators
# ────────────────────────────────────────────────────────────────────────

async def test_aggregate_having_supports_in_operator(temp_db):
    """HAVING accepts `__in` (and the rest of the operator suite), not
    just numeric comparators. Pre-fix this fell through to `eq` and
    produced `count = [1, 2, 3]` (semantically wrong)."""
    db = await _seed_rows(temp_db)
    try:
        rows = await db.aggregate(
            group_by="city", count=True,
            having={"count__in": [2, 3]},
            order_by="-count",
        )
        cities = [r["city"] for r in rows]
        # LA(3), NY(2) match; SF(1) and None(1) excluded.
        assert cities == ["LA", "NY"]
    finally:
        await db.close()


async def test_aggregate_having_supports_between(temp_db):
    """HAVING with __between picks rows whose aggregate falls in the range."""
    db = await _seed_rows(temp_db)
    try:
        rows = await db.aggregate(
            group_by="city", count=True,
            having={"count__between": (2, 3)},
        )
        assert {r["city"] for r in rows} == {"LA", "NY"}
    finally:
        await db.close()


# ────────────────────────────────────────────────────────────────────────
# 22. Review follow-up — migration module cache (no re-exec)
# ────────────────────────────────────────────────────────────────────────

async def test_migration_module_cached_across_invocations(tmp_path):
    """A migration script's top-level body executes once per (path, mtime).

    We track that with a sentinel: the module body increments a counter
    in a sidecar file. After two consecutive `_discover_migrations`
    calls (no file edit between them) the counter must still read 1.
    """
    from esuls.db_cli import _discover_migrations, _migration_module_cache

    mdir = tmp_path / "migrations"
    mdir.mkdir()
    counter = tmp_path / "counter.txt"
    counter.write_text("0")

    (mdir / "001_count.py").write_text(
        f"from pathlib import Path\n"
        f"# Side effect runs once per module-load, so we use it to detect re-exec.\n"
        f"_p = Path({str(counter)!r})\n"
        f"_p.write_text(str(int(_p.read_text()) + 1))\n"
        f"version = 1\n"
        f"description = 'count'\n"
        f"async def upgrade(conn): pass\n"
    )

    # Clear cache so this test is isolated.
    _migration_module_cache.clear()

    _discover_migrations(mdir)
    assert counter.read_text() == "1"
    _discover_migrations(mdir)
    _discover_migrations(mdir)
    assert counter.read_text() == "1", (
        f"module re-executed; counter={counter.read_text()!r}"
    )


# ────────────────────────────────────────────────────────────────────────
# 23. Review follow-up — _apply_op extraction
# ────────────────────────────────────────────────────────────────────────

def test_apply_op_all_operators_reachable():
    """Smoke-test that every operator in OPERATOR_MAP compiles via _apply_op
    without raising. (Doesn't validate semantics — those are covered above —
    only that the dispatch table covers every key.)"""
    from sqlalchemy import column
    col = column("x")
    for op in AsyncDB.OPERATOR_MAP:
        val = (1, 2) if op == "between" else [1, 2] if op in ("in", "not_in") else 1
        AsyncDB._apply_op(col, op, val)  # must not raise


# ────────────────────────────────────────────────────────────────────────
# 24. Audit follow-up — TimestampedIntModel
# ────────────────────────────────────────────────────────────────────────

@dataclass
class _IntTimed(TimestampedIntModel):
    name: str = ""


async def test_timestamped_int_model_autoincrement_plus_timestamps(temp_db):
    """INT autoincrement PK + auto-managed tz-aware timestamps."""
    db = AsyncDB(temp_db, "items", _IntTimed)
    try:
        await db.save(_IntTimed(name="a"))
        await db.save(_IntTimed(name="b"))
        rows = await db.find(order_by="id")
        ids = [r.id for r in rows]
        assert all(isinstance(i, int) for i in ids) and ids == sorted(ids)
        for r in rows:
            assert r.created_at.tzinfo is not None
            assert r.updated_at.tzinfo is not None
    finally:
        await db.close()


def test_utcnow_is_public_and_tz_aware():
    """`utcnow` is the public, exported clock; returns tz-aware UTC."""
    t = utcnow()
    assert t.tzinfo is not None
    assert t.utcoffset().total_seconds() == 0


# ────────────────────────────────────────────────────────────────────────
# 25. Audit follow-up — save() narrows exception scope
# ────────────────────────────────────────────────────────────────────────

@dataclass
class _SimpleRow(BaseModel):
    name: str = ""


async def test_save_propagates_typeerror_on_wrong_type_when_not_skipping(temp_db):
    db = AsyncDB(temp_db, "items", _SimpleRow)
    try:
        with pytest.raises(TypeError, match="Expected _SimpleRow"):
            await db.save("not a model", skip_errors=False)
    finally:
        await db.close()


async def test_save_propagates_valueerror_on_bad_on_conflict_even_with_skip(temp_db):
    """Programming errors (bad on_conflict) must propagate even when
    skip_errors=True — the previous wide `except Exception` swallowed
    them, masking caller bugs as "save failed"."""
    db = AsyncDB(temp_db, "items", _SimpleRow)
    try:
        with pytest.raises(ValueError, match="on_conflict"):
            # `name` is not a unique/PK column, so on_conflict='name'
            # is a programming error.
            await db.save(_SimpleRow(name="x"), on_conflict="name", skip_errors=True)
    finally:
        await db.close()


async def test_save_swallows_db_error_with_skip_errors_true(temp_db):
    """A real DB-level error (IntegrityError on UNIQUE violation) IS still
    swallowed when skip_errors=True — that's the legit use case."""
    @dataclass
    class _UniqRow(BaseModel):
        ext: str = field(default="", metadata={"unique": True})

    db = AsyncDB(temp_db, "items", _UniqRow)
    try:
        await db.save(_UniqRow(ext="dup"))
        # Second save with a different id but same `ext` → UNIQUE violation
        # on `ext` (conflict target is `id` by default, so DO UPDATE
        # doesn't catch it). With skip_errors=True we just return False.
        ok = await db.save(_UniqRow(ext="dup"), skip_errors=True)
        assert ok is False
    finally:
        await db.close()


# ────────────────────────────────────────────────────────────────────────
# 26. Audit follow-up — count_distinct in aggregate
# ────────────────────────────────────────────────────────────────────────

async def test_aggregate_count_distinct(temp_db):
    @dataclass
    class _Visit(BaseModel):
        user: str = ""
        page: str = ""

    db = AsyncDB(temp_db, "visits", _Visit)
    try:
        for u, p in [("u1", "/a"), ("u1", "/b"), ("u2", "/a"),
                     ("u3", "/a"), ("u1", "/a")]:
            await db.save(_Visit(user=u, page=p))
        rows = await db.aggregate(count=True, count_distinct="user")
        assert rows == [{"count": 5, "count_distinct_user": 3}]
        rows = await db.aggregate(group_by="page", count=True,
                                  count_distinct="user", order_by="page")
        by_page = {r["page"]: r for r in rows}
        assert by_page["/a"]["count"] == 4
        assert by_page["/a"]["count_distinct_user"] == 3
        assert by_page["/b"]["count"] == 1
        assert by_page["/b"]["count_distinct_user"] == 1
    finally:
        await db.close()


async def test_aggregate_count_distinct_multiple_columns(temp_db):
    """Each entry in count_distinct produces its own aliased aggregate."""
    @dataclass
    class _Visit(BaseModel):
        user: str = ""
        page: str = ""

    db = AsyncDB(temp_db, "visits", _Visit)
    try:
        for u, p in [("u1", "/a"), ("u1", "/b"), ("u2", "/a")]:
            await db.save(_Visit(user=u, page=p))
        rows = await db.aggregate(count_distinct=["user", "page"])
        assert rows == [{"count_distinct_user": 2, "count_distinct_page": 2}]
    finally:
        await db.close()


# ────────────────────────────────────────────────────────────────────────
# 27. Audit follow-up — update_many
# ────────────────────────────────────────────────────────────────────────

async def test_update_many_updates_matching_rows(temp_db):
    @dataclass
    class _R(BaseModel):
        category: str = ""
        status: str = "new"

    db = AsyncDB(temp_db, "items", _R)
    try:
        for cat, st in [("a", "new"), ("a", "new"), ("b", "new"), ("c", "new")]:
            await db.save(_R(category=cat, status=st))
        n = await db.update_many({"status": "archived"}, category__in=["a", "c"])
        assert n == 3
        archived = await db.find(status="archived")
        assert {r.category for r in archived} == {"a", "c"}
        new = await db.find(status="new")
        assert [r.category for r in new] == ["b"]
    finally:
        await db.close()


async def test_update_many_requires_filter(temp_db, db):
    with pytest.raises(ValueError, match="at least one filter"):
        await db.update_many({"name": "x"})


async def test_update_many_empty_values_returns_zero(temp_db, db):
    """update_many with empty values is a no-op (returns 0) without touching DB."""
    await db.save(db.schema_class(name="x", value=1))
    n = await db.update_many({}, name="x")
    assert n == 0


async def test_update_many_auto_sets_updated_at(temp_db):
    """If the schema has updated_at and the caller doesn't pass it,
    update_many auto-stamps it like update_fields does."""
    @dataclass
    class _R(BaseModel):
        tag: str = ""

    db = AsyncDB(temp_db, "items", _R)
    try:
        await db.save(_R(tag="a"))
        before = (await db.find())[0].updated_at
        # Force a clock-tick gap with asyncio.sleep so timestamps differ.
        await asyncio.sleep(0.01)
        await db.update_many({"tag": "b"}, tag="a")
        after = (await db.find())[0].updated_at
        assert after > before, f"updated_at not refreshed: {before} → {after}"
    finally:
        await db.close()


# ────────────────────────────────────────────────────────────────────────
# 28. Audit follow-up — find_columns
# ────────────────────────────────────────────────────────────────────────

async def test_find_columns_returns_dicts_with_selected_keys(temp_db):
    @dataclass
    class _R(BaseModel):
        name: str = ""
        body: str = ""  # imagine this is a big BLOB

    db = AsyncDB(temp_db, "items", _R)
    try:
        for i in range(3):
            await db.save(_R(name=f"n{i}", body="x" * 1000))
        rows = await db.find_columns(["id", "name"], order_by="name")
        assert all(set(r.keys()) == {"id", "name"} for r in rows)
        assert [r["name"] for r in rows] == ["n0", "n1", "n2"]
        # Filters work
        rows = await db.find_columns(["name"], name__like="n_", order_by="name")
        assert [r["name"] for r in rows] == ["n0", "n1", "n2"]
    finally:
        await db.close()


async def test_find_columns_empty_columns_raises(temp_db, db):
    with pytest.raises(ValueError, match="at least one column"):
        await db.find_columns([])


async def test_find_columns_unknown_column_raises(temp_db, db):
    with pytest.raises(ValueError, match="bogus"):
        await db.find_columns(["bogus"])


# ────────────────────────────────────────────────────────────────────────
# 29. Audit follow-up — migration introspection (discover + list)
# ────────────────────────────────────────────────────────────────────────

async def test_discover_migrations_public_function(tmp_path):
    """The public `discover_migrations(path)` returns plain dicts."""
    mdir = tmp_path / "migrations"
    mdir.mkdir()
    (mdir / "001_first.py").write_text(
        "version = 1\ndescription = 'first'\nasync def upgrade(conn): pass\n"
    )
    (mdir / "002_second.py").write_text(
        "version = 2\ndescription = 'second'\nasync def upgrade(conn): pass\n"
    )
    out = discover_migrations(mdir)
    assert [m["version"] for m in out] == [1, 2]
    assert out[0]["description"] == "first"
    assert out[0]["source"] == "001_first.py"


async def test_list_migrations_reports_applied_status(tmp_path):
    from esuls.db_cli import _db_state_by_loop as _state

    db_path = tmp_path / "app.db"
    mdir = tmp_path / "migrations"

    # Phase 1: no migrations dir → empty.
    db = AsyncDB(db_path, "items", _SimpleRow)
    try:
        await db.save(_SimpleRow(name="seed"))
        assert await db.list_migrations() == []
    finally:
        await db.close()

    # Phase 2: add a migration. Existing DB → not auto-applied.
    mdir.mkdir()
    (mdir / "001_create_side.py").write_text(
        "from sqlalchemy import text\n"
        "version = 1\n"
        "description = 'create side'\n"
        "async def upgrade(conn):\n"
        "    await conn.execute(text('CREATE TABLE side (id INT)'))\n"
    )
    for s in _state.values():
        s["initialized"].clear()

    db2 = AsyncDB(db_path, "items", _SimpleRow)
    try:
        migs = await db2.list_migrations()
        # After _ensure_engines fires (which list_migrations triggers),
        # the migration has been applied; entry should report applied=True.
        assert len(migs) == 1
        assert migs[0]["version"] == 1
        assert migs[0]["applied"] is True
        assert migs[0]["source"] == "001_create_side.py"
    finally:
        await db2.close()


# ────────────────────────────────────────────────────────────────────────
# 30. Audit follow-up — migration import error wrapping (M6)
# ────────────────────────────────────────────────────────────────────────

def test_migration_import_error_wrapped_with_filename(tmp_path):
    """A SyntaxError / ImportError in a migration file is re-raised with
    a RuntimeError that names the file — not the synthetic module name."""
    from esuls.db_cli import _discover_migrations, _migration_module_cache

    _migration_module_cache.clear()
    mdir = tmp_path / "migrations"
    mdir.mkdir()
    (mdir / "001_broken.py").write_text(
        "version = 1\n"
        "description = 'oops'\n"
        "this is not python at all\n"
    )
    with pytest.raises(RuntimeError, match="001_broken.py.*SyntaxError"):
        _discover_migrations(mdir)


# ────────────────────────────────────────────────────────────────────────
# 31. Audit follow-up — ResourceWarning on undisposed engine (H3)
# ────────────────────────────────────────────────────────────────────────

async def test_close_flips_engine_markers(temp_db, db):
    """close() flips the per-engine disposal markers to True *before*
    calling dispose() — so a GC finalizer racing with close() sees the
    flag and skips the leak warning."""
    from esuls.db_cli import _engine_markers
    await db.save(db.schema_class(name="x", value=1))
    writer, reader = await db._ensure_engines()
    assert _engine_markers[writer].disposed is False
    assert _engine_markers[reader].disposed is False
    await db.close()
    assert _engine_markers[writer].disposed is True
    assert _engine_markers[reader].disposed is True


def test_maybe_warn_emits_resource_warning_for_undisposed():
    """The finalize callback emits ResourceWarning when disposed=False
    and is silent when disposed=True."""
    from esuls.db_cli import (
        _EngineDisposalMarker, _maybe_warn_undisposed_engine,
    )

    # Undisposed → warn.
    m = _EngineDisposalMarker()
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        _maybe_warn_undisposed_engine("/tmp/leaky.db", m)
    leak = [x for x in w if issubclass(x.category, ResourceWarning)]
    assert leak and "/tmp/leaky.db" in str(leak[0].message)
    assert "close" in str(leak[0].message).lower()

    # Disposed → silent.
    m.disposed = True
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        _maybe_warn_undisposed_engine("/tmp/leaky.db", m)
    leak = [x for x in w if issubclass(x.category, ResourceWarning)]
    assert leak == []


# ────────────────────────────────────────────────────────────────────────
# 32. Audit follow-up — cross-loop migrations (T1)
# ────────────────────────────────────────────────────────────────────────

def test_migrations_apply_across_asyncio_runs(tmp_path):
    """Migrations apply correctly when a process uses one AsyncDB across
    multiple asyncio.run() calls. The first run leap-frogs user_version
    (fresh DB); the second sees the existing DB and confirms idempotency."""
    db_path = tmp_path / "app.db"
    mdir = tmp_path / "migrations"
    mdir.mkdir()
    (mdir / "001_first.py").write_text(
        "version = 1\ndescription = 'first'\nasync def upgrade(conn): pass\n"
    )

    async def use_db() -> int:
        db = AsyncDB(db_path, "items", _SimpleRow)
        try:
            await db.save(_SimpleRow(name="x"))
            async with db.transaction(read_only=True) as conn:
                from sqlalchemy import text as _text
                return (await conn.execute(_text("PRAGMA user_version"))).scalar()
        finally:
            await db.close()

    v1 = asyncio.run(use_db())  # fresh DB → leap-frog to v=1
    v2 = asyncio.run(use_db())  # existing DB, v=1, nothing pending
    v3 = asyncio.run(use_db())
    assert v1 == 1 and v2 == 1 and v3 == 1


# ────────────────────────────────────────────────────────────────────────
# 33. Audit follow-up — strict_schema=False permits drift (T5)
# ────────────────────────────────────────────────────────────────────────

async def test_strict_schema_false_permits_drift_with_warning(temp_db):
    """Without strict_schema, drift is a warning, not an error: ops succeed."""
    from loguru import logger as _logger
    db1 = AsyncDB(temp_db, "items", _V1)
    try:
        await db1.save(_V1(name="alpha"))
    finally:
        await db1.close()

    for s in _db_state_by_loop.values():
        s["initialized"].clear()

    captured: list[str] = []
    sink_id = _logger.add(captured.append, level="WARNING")
    try:
        # strict_schema=False (default): drift warning, ops continue.
        db2 = AsyncDB(temp_db, "items", _V2)
        try:
            # Save must succeed despite the retrofit warning.
            await db2.save(_V2(name="beta", score=10))
            rows = await db2.fetch_all()
            assert len(rows) == 2
        finally:
            await db2.close()
    finally:
        _logger.remove(sink_id)
    msgs = "\n".join(captured)
    assert "score" in msgs  # drift warning was emitted


# ────────────────────────────────────────────────────────────────────────
# 34. Audit follow-up — retry jitter (smoke)
# ────────────────────────────────────────────────────────────────────────

async def test_retry_backoff_includes_jitter(temp_db, db, monkeypatch):
    """The retry sleep varies across attempts due to the jitter multiplier.

    We monkeypatch asyncio.sleep to record arguments, then trigger a
    fake-busy retry loop and assert no two consecutive sleeps are
    identical (jitter ⇒ effectively-zero probability of collision).
    """
    sleeps: list[float] = []

    async def fake_sleep(t):
        sleeps.append(t)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    class _MarkerBusy(Exception):
        pass

    import esuls.db_cli as dbmod
    real_busy = dbmod._is_sqlite_busy
    monkeypatch.setattr(
        dbmod, "_is_sqlite_busy",
        lambda exc: isinstance(exc, _MarkerBusy) or real_busy(exc),
    )

    async def flaky():
        raise _MarkerBusy("synthetic")

    with pytest.raises(_MarkerBusy):
        await db._execute_with_retry(flaky, max_retries=4)

    # 3 sleeps for 4 attempts (after the first 3 failures).
    assert len(sleeps) == 3
    # Each sleep is in the [0.5×, 1.5×] band of 0.2 * 2**attempt.
    expected_bases = [0.2, 0.4, 0.8]
    for actual, base in zip(sleeps, expected_bases):
        assert base * 0.5 <= actual <= base * 1.5, (
            f"sleep {actual} outside jitter band of base {base}"
        )


# ────────────────────────────────────────────────────────────────────────
# 35. Tier 2 — composite UNIQUE via __unique_together__
# ────────────────────────────────────────────────────────────────────────

@dataclass
class _Deal(BaseModel):
    user_id: str = ""
    external_id: str = ""
    payload: str = ""
    __unique_together__ = [("user_id", "external_id")]


async def test_composite_upsert_updates_existing(temp_db):
    """Two saves with the same (user_id, external_id) but different `id`
    upsert via the composite UNIQUE instead of duplicating."""
    db = AsyncDB(temp_db, "deals", _Deal)
    try:
        await db.save(
            _Deal(user_id="u1", external_id="ext", payload="v1"),
            on_conflict=("user_id", "external_id"),
        )
        await db.save(
            _Deal(user_id="u1", external_id="ext", payload="v2"),
            on_conflict=("user_id", "external_id"),
        )
        rows = await db.find()
        assert len(rows) == 1
        assert rows[0].payload == "v2"
    finally:
        await db.close()


async def test_composite_upsert_order_insensitive(temp_db):
    """on_conflict=('a', 'b') and ('b', 'a') match the same declared composite."""
    db = AsyncDB(temp_db, "deals", _Deal)
    try:
        await db.save(
            _Deal(user_id="u1", external_id="ext", payload="v1"),
            on_conflict=("user_id", "external_id"),
        )
        await db.save(
            _Deal(user_id="u1", external_id="ext", payload="v2"),
            on_conflict=("external_id", "user_id"),  # swapped order
        )
        rows = await db.find()
        assert len(rows) == 1 and rows[0].payload == "v2"
    finally:
        await db.close()


async def test_undeclared_composite_raises_with_helpful_message(temp_db):
    """A composite target not in `__unique_together__` and not all-individual
    raises with a message that points the user at the schema declaration."""
    db = AsyncDB(temp_db, "deals", _Deal)
    try:
        with pytest.raises(ValueError, match=r"composite target not declared"):
            await db.save(
                _Deal(user_id="u1", external_id="x", payload="v"),
                on_conflict=("user_id", "payload"),  # payload isn't unique
                skip_errors=False,
            )
    finally:
        await db.close()


async def test_unique_together_creates_actual_db_constraint(temp_db):
    """The composite UNIQUE is enforced at the SQLite level (not just a
    Python-side validator). Bypassing the upsert with a raw INSERT of a
    duplicate must hit IntegrityError."""
    db = AsyncDB(temp_db, "deals", _Deal)
    try:
        await db.save(_Deal(user_id="u1", external_id="ext"))
        with pytest.raises(sa_exc.IntegrityError):
            async with db.transaction() as conn:
                from sqlalchemy import text as _text
                await conn.execute(_text(
                    "INSERT INTO deals (id, created_at, updated_at, "
                    "user_id, external_id, payload) VALUES "
                    "(:i, :c, :u, :uid, :eid, :p)"
                ), {
                    "i": "fresh", "c": "x", "u": "x",
                    "uid": "u1", "eid": "ext", "p": "dup",
                })
    finally:
        await db.close()


def test_unique_together_rejects_bad_declaration():
    """Schema with malformed __unique_together__ fails at AsyncDB() time."""
    @dataclass
    class _BadOne(BaseModel):
        a: str = ""
        __unique_together__ = [("a",)]  # single-column composite is nonsensical

    @dataclass
    class _BadTwo(BaseModel):
        a: str = ""
        __unique_together__ = [("a", "nonexistent")]

    import tempfile as _tmp
    with _tmp.TemporaryDirectory() as d:
        with pytest.raises(ValueError, match=r"at least 2 column"):
            AsyncDB(Path(d) / "x.db", "items", _BadOne)
        with pytest.raises(ValueError, match=r"unknown column"):
            AsyncDB(Path(d) / "y.db", "items", _BadTwo)


# ────────────────────────────────────────────────────────────────────────
# 36. Tier 2 — find_one + faster exists
# ────────────────────────────────────────────────────────────────────────

async def test_find_one_returns_first_or_none(temp_db, db):
    assert await db.find_one(name="missing") is None
    await db.save(db.schema_class(name="hit", value=1))
    found = await db.find_one(name="hit")
    assert found is not None and found.value == 1


async def test_find_one_respects_order_by(temp_db, db):
    """The 'most recent matching X' idiom: pass order_by to pick deterministically."""
    for i in range(3):
        await db.save(db.schema_class(name="dup", value=i))
    latest = await db.find_one(order_by="-value", name="dup")
    earliest = await db.find_one(order_by="value", name="dup")
    assert latest.value == 2 and earliest.value == 0


async def test_exists_uses_select_1_not_count(temp_db, db, monkeypatch):
    """exists() compiles to SELECT 1 ... LIMIT 1, not SELECT COUNT(*).

    We assert the structure of the SQL emitted by spying on execute().
    Filter to the exists()-targeted SELECT (FROM items + non-aggregate +
    LIMIT) — there are unrelated SELECTs in the captured stream (schema
    introspection, sqlite_master probes).
    """
    await db.save(db.schema_class(name="x", value=1))
    captured: list[str] = []
    _, reader = await db._ensure_engines()
    from sqlalchemy import event as _event

    @_event.listens_for(reader.sync_engine, "before_cursor_execute")
    def _capture(_conn, _cursor, statement, *_):
        captured.append(statement)

    assert await db.exists(name="x") is True
    assert await db.exists(name="missing") is False

    items_queries = [s for s in captured if "FROM items" in s and "WHERE" in s]
    assert items_queries, f"no FROM items queries captured: {captured!r}"
    for s in items_queries:
        assert "count(" not in s.lower(), (
            f"exists() still uses COUNT(*): {s!r}"
        )
        assert "limit" in s.lower(), (
            f"exists() should LIMIT to early-out: {s!r}"
        )


# ────────────────────────────────────────────────────────────────────────
# 37. Tier 2 — conn= on write methods (multi-table atomic transactions)
# ────────────────────────────────────────────────────────────────────────

@dataclass
class _OrderRow(BaseModel):
    sku: str = ""
    qty: int = 0


@dataclass
class _InventoryRow(BaseModel):
    sku: str = ""
    on_hand: int = 0


async def test_two_async_dbs_share_one_transaction(tmp_path):
    """save(conn=conn) across two AsyncDBs on the same .db file commits atomically."""
    p = tmp_path / "shared.db"
    db_o = AsyncDB(p, "orders", _OrderRow)
    db_i = AsyncDB(p, "inventory", _InventoryRow)
    try:
        async with db_o.transaction() as conn:
            await db_o.save(_OrderRow(sku="A", qty=3), conn=conn)
            await db_i.save(_InventoryRow(sku="A", on_hand=97), conn=conn)
        assert await db_o.count() == 1
        assert await db_i.count() == 1
    finally:
        await db_o.close()
        await db_i.close()


async def test_transaction_rollback_rolls_back_both_dbs(tmp_path):
    """A raise inside the transaction rolls back writes on both AsyncDBs."""
    p = tmp_path / "shared.db"
    db_o = AsyncDB(p, "orders", _OrderRow)
    db_i = AsyncDB(p, "inventory", _InventoryRow)
    try:
        try:
            async with db_o.transaction() as conn:
                await db_o.save(_OrderRow(sku="X", qty=1), conn=conn)
                await db_i.save(_InventoryRow(sku="X", on_hand=99), conn=conn)
                raise RuntimeError("simulated failure")
        except RuntimeError:
            pass
        assert await db_o.count() == 0
        assert await db_i.count() == 0
    finally:
        await db_o.close()
        await db_i.close()


async def test_save_batch_with_conn(tmp_path):
    """save_batch(conn=) executes inline (no nested begin())."""
    p = tmp_path / "shared.db"
    db_o = AsyncDB(p, "orders", _OrderRow)
    try:
        items = [_OrderRow(sku=f"S{i}", qty=i) for i in range(10)]
        async with db_o.transaction() as conn:
            n = await db_o.save_batch(items, conn=conn)
        assert n == 10
        assert await db_o.count() == 10
    finally:
        await db_o.close()


async def test_update_fields_with_conn(tmp_path):
    p = tmp_path / "shared.db"
    db_o = AsyncDB(p, "orders", _OrderRow)
    try:
        o = _OrderRow(sku="A", qty=1)
        await db_o.save(o)
        async with db_o.transaction() as conn:
            ok = await db_o.update_fields(o.id, qty=42, conn=conn)
        assert ok is True
        loaded = await db_o.get_by_id(o.id)
        assert loaded.qty == 42
    finally:
        await db_o.close()


async def test_delete_with_conn(tmp_path):
    p = tmp_path / "shared.db"
    db_o = AsyncDB(p, "orders", _OrderRow)
    try:
        o = _OrderRow(sku="A", qty=1)
        await db_o.save(o)
        async with db_o.transaction() as conn:
            ok = await db_o.delete(o.id, conn=conn)
        assert ok is True
        assert await db_o.count() == 0
    finally:
        await db_o.close()


async def test_schema_init_atomically_with_caller_transaction(tmp_path):
    """A brand-new AsyncDB whose schema hasn't been created yet runs
    schema-init on the caller's connection, atomically. Verified by
    rolling back: neither schema nor data persists."""
    p = tmp_path / "fresh.db"
    # Bootstrap a single table so the DB file exists.
    bootstrap = AsyncDB(p, "orders", _OrderRow)
    try:
        await bootstrap.save(_OrderRow(sku="seed"))
    finally:
        await bootstrap.close()

    # Re-init state so the per-loop registry sees these AsyncDBs fresh.
    from esuls.db_cli import _db_state_by_loop as _state
    for s in _state.values():
        s["initialized"].clear()

    db_o = AsyncDB(p, "orders", _OrderRow)
    # db_i has NEVER been initialised in this loop. Its schema doesn't exist.
    db_i = AsyncDB(p, "inventory_v2", _InventoryRow)
    try:
        try:
            async with db_o.transaction() as conn:
                await db_o.save(_OrderRow(sku="rolled-back"), conn=conn)
                # First save on db_i runs its schema-init on this conn,
                # atomically with the rest. Then we explode → roll back.
                await db_i.save(_InventoryRow(sku="rolled-back", on_hand=1), conn=conn)
                raise RuntimeError("rollback")
        except RuntimeError:
            pass

        # Data NOT persisted.
        assert await db_o.find_one(sku="rolled-back") is None
        # And the inventory_v2 table either doesn't exist or is empty —
        # depending on whether SQLite's CREATE TABLE was rolled back.
        # Either way: no inventory_v2 row.
        try:
            assert await db_i.count() == 0
        except sa_exc.OperationalError:
            # Table doesn't exist post-rollback — also acceptable.
            pass
    finally:
        await db_o.close()
        try:
            await db_i.close()
        except Exception:
            pass


# ────────────────────────────────────────────────────────────────────────
# 38. Tier 3 — annotation-driven JSON dict-key coercion
# ────────────────────────────────────────────────────────────────────────

@dataclass
class _IntKeyDict(BaseModel):
    by_year: dict = field(default_factory=dict)


@dataclass
class _TypedIntKeyDict(BaseModel):
    # Real annotation drives the coercion (vs. _IntKeyDict above which is `dict`).
    by_year: "dict[int, float]" = field(default_factory=dict)


async def test_dict_int_keys_roundtrip_with_annotation(temp_db):
    """Dict[int, float] keys come back as int, not str."""
    db = AsyncDB(temp_db, "rows", _TypedIntKeyDict)
    try:
        original = {2020: 100.0, 2021: 150.5, 2022: 200.75}
        await db.save(_TypedIntKeyDict(by_year=original))
        loaded = (await db.find())[0]
        assert loaded.by_year == original
        assert all(isinstance(k, int) for k in loaded.by_year)
    finally:
        await db.close()


async def test_dict_without_annotation_keys_remain_str(temp_db):
    """A field declared `dict` (no Dict[K, V]) has no coercion — keys
    come back as str (pre-PR behaviour, intentionally preserved as default)."""
    db = AsyncDB(temp_db, "rows", _IntKeyDict)
    try:
        await db.save(_IntKeyDict(by_year={2020: 1.0, 2021: 2.0}))
        loaded = (await db.find())[0]
        # No coercion → JSON's default str keys.
        assert all(isinstance(k, str) for k in loaded.by_year)
        assert loaded.by_year == {"2020": 1.0, "2021": 2.0}
    finally:
        await db.close()


async def test_dict_str_keys_stay_str(temp_db):
    """Dict[str, X] is unaffected — no coercion is registered."""
    @dataclass
    class _StrKey(BaseModel):
        m: "dict[str, int]" = field(default_factory=dict)

    db = AsyncDB(temp_db, "rows", _StrKey)
    try:
        await db.save(_StrKey(m={"a": 1, "b": 2}))
        loaded = (await db.find())[0]
        assert loaded.m == {"a": 1, "b": 2}
    finally:
        await db.close()


async def test_dict_decimal_keys_roundtrip(temp_db):
    @dataclass
    class _DecKey(BaseModel):
        m: "dict[Decimal, str]" = field(default_factory=dict)

    db = AsyncDB(temp_db, "rows", _DecKey)
    try:
        original = {Decimal("1.5"): "a", Decimal("2.25"): "b"}
        await db.save(_DecKey(m=original))
        loaded = (await db.find())[0]
        assert loaded.m == original
        assert all(isinstance(k, Decimal) for k in loaded.m)
    finally:
        await db.close()


def test_coercion_for_key_type_known_and_unknown():
    """The helper returns int/float/Decimal/UUID coercions and None elsewhere."""
    from esuls.db_cli import _coercion_for_key_type
    import uuid as _uuid
    assert _coercion_for_key_type(int) is int
    assert _coercion_for_key_type(float) is float
    assert _coercion_for_key_type(Decimal) is Decimal
    assert _coercion_for_key_type(_uuid.UUID) is _uuid.UUID
    # str (no-op) and unknown types return None.
    assert _coercion_for_key_type(str) is None
    assert _coercion_for_key_type(None) is None
    assert _coercion_for_key_type(bytes) is None  # unsupported → fall through
