"""
Async SQLite layer built on SQLAlchemy 2.0 Core.

Public API and dataclass-as-schema ergonomics preserved from the prior
custom implementation; internals delegate to SQLA Core for SQL building,
parameter binding, type roundtripping, and connection pooling.

SQLite-specific concerns kept custom:
  - Write serialisation lock (SQLite is single-writer)
  - BUSY/LOCKED retry policy with exponential backoff
  - PRAGMA setup via SQLA `connect` event hook (foreign_keys=ON, WAL,
    mmap_size, temp_store=MEMORY, wal_autocheckpoint, ...)
  - Per-event-loop engine registry (a single AsyncDB instance survives
    multiple `asyncio.run()` calls without `bound to a different event loop`)
  - PRAGMA optimize + wal_checkpoint(TRUNCATE) on close
  - Two-engine model: StaticPool writer (single conn; lock pre-serialises)
    + pooled reader (pool_size=4 for concurrent WAL reads)
  - One shared `MetaData` per database FILE (`_shared_table`), so a
    `foreign_key` declared on one AsyncDB can resolve a table declared on
    another — the file being the only boundary inside which SQLite can hold
    a constraint at all
"""
import asyncio
import base64
import contextlib
import dataclasses
import enum
import importlib.util
import inspect
import json
import random
import re
import sqlite3
import threading
import types
import uuid
import warnings
import weakref
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal
from pathlib import Path
from typing import (
    Any, AsyncIterator, Awaitable, Callable, Dict, Generic, List, Optional,
    Tuple, Type, TypeVar, Union, get_type_hints,
)

from loguru import logger
from sqlalchemy import (
    Boolean,
    bindparam, Column, Date, DateTime, Float, ForeignKey, Index, Integer,
    LargeBinary, MetaData, Table, Text, UniqueConstraint, and_, delete, event,
    func, literal, select, text, update,
)
from sqlalchemy import create_engine
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.types import TypeDecorator
import sqlalchemy.exc as sa_exc


# ─── transport-level encoding for BaseModel.to_dict / from_dict ──────────
#
# Used when models are shipped over JSON (cache APIs, IPC). The DB layer
# uses TypeDecorators instead — these two paths are intentionally separate.

_B64_PREFIX = "b64:"


def _get_union_origin(tp):
    """Return Union/UnionType if `tp` is a Union (any form), else None.

    Handles both `typing.Optional[X]` / `Union[...]` and PEP 604 `X | None`
    (which has origin `types.UnionType`, not `typing.Union`).
    """
    if tp is None:
        return None
    if getattr(tp, "__origin__", None) is Union:
        return Union
    if isinstance(tp, types.UnionType):
        return types.UnionType
    return None


def _is_bytes_field(ftype) -> bool:
    """True for `bytes`, `Optional[bytes]`, and `bytes | None`."""
    if ftype is bytes:
        return True
    if _get_union_origin(ftype) is not None:
        return bytes in getattr(ftype, "__args__", ())
    return False


def _unwrap_optional(tp):
    """Drop NoneType from a Union and return the lone remaining arg, if any.

    Returns `tp` unchanged when it's not a Union or has multiple non-None args.
    """
    if _get_union_origin(tp) is None:
        return tp
    args = [a for a in getattr(tp, "__args__", ()) if a is not type(None)]
    if len(args) == 1:
        return args[0]
    return tp


_VALID_IDENTIFIER = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _validate_identifier(name: str) -> str:
    """Reject SQL identifiers that don't match `[a-zA-Z_][a-zA-Z0-9_]*`.

    The schema-column whitelist on AsyncDB is the primary defence; this
    second gate exists so the table_name argument (which isn't a schema
    column) is still validated.
    """
    if not _VALID_IDENTIFIER.match(name):
        raise ValueError(f"Invalid SQL identifier: {name!r}")
    return name


# ─── retry classification ────────────────────────────────────────────────

_RETRYABLE_SQLITE_CODES = frozenset({sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED})


def _is_sqlite_busy(exc: BaseException) -> bool:
    """True if `exc` is a transient SQLite contention error worth retrying.

    SQLA wraps `sqlite3.OperationalError` in `sa_exc.OperationalError` with
    the original DBAPI exception under `.orig`. We inspect both layers.
    """
    inner = getattr(exc, "orig", None) or exc
    code = getattr(inner, "sqlite_errorcode", None)
    if code in _RETRYABLE_SQLITE_CODES:
        return True
    msg = str(exc).lower()
    return "database is locked" in msg or "database is busy" in msg


_STALE_CONNECTION_TYPES: tuple[type, ...] = (
    sa_exc.ResourceClosedError,
    sa_exc.DisconnectionError,
    sa_exc.InvalidRequestError,
)


def _is_stale_connection(exc: BaseException) -> bool:
    """True if `exc` indicates the pooled connection is dead.

    Prefers SQLA's typed exceptions (`ResourceClosedError`,
    `DisconnectionError`, `InvalidRequestError`); falls back to string
    match on the wrapped DBAPI error for cases that don't surface a
    typed SQLA exception (e.g. raw aiosqlite errors leaking through).
    """
    if isinstance(exc, _STALE_CONNECTION_TYPES):
        return True
    msg = str(exc).lower()
    return "closed" in msg or "no active connection" in msg


def utcnow() -> datetime:
    """Timezone-aware UTC `now`.

    The canonical clock for everything in this module: `BaseModel` /
    `TimestampedModel` / `TimestampedIntModel` defaults, `_item_to_row`,
    `update_fields`. Public so callers that build their own
    `TimestampedXxxModel`-style bases can stay consistent.

    Centralised so a future swap (e.g. injectable clock for testing) only
    touches this one symbol.
    """
    return datetime.now(timezone.utc)


# Backward-compat alias for any internal/vendored code still importing the
# private name; new call sites should use `utcnow` directly.
_utcnow = utcnow


# Pairs of SQL type names that SQLite treats as functionally interchangeable
# (same type affinity). The drift detector uses this to avoid false-positive
# warnings when a live table was created with one of these names by an
# older version of the code (or by hand) and the dataclass would now
# declare the other.
#
#   BOOLEAN <-> INTEGER : both have NUMERIC affinity; bool is stored as
#                         0/1 either way. An older table built with
#                         `Integer` for a bool field is functionally
#                         identical to a fresh one built with `Boolean`.
#   DATETIME <-> TEXT   : we now store datetimes via _UTCDateTimeDecorator
#                         (TEXT), but older code used `DateTime(timezone=True)`
#                         which rendered as DATETIME.
#   VARCHAR  <-> TEXT   : SQLA's `String` without length renders VARCHAR;
#                         our `Text` renders TEXT. Both have TEXT affinity.
_EQUIVALENT_TYPES: frozenset[frozenset[str]] = frozenset({
    frozenset({"BOOLEAN", "INTEGER"}),
    frozenset({"DATETIME", "TEXT"}),
    frozenset({"VARCHAR", "TEXT"}),
})


def _normalise_sql_type(t: str) -> str:
    """Reduce a SQL type declaration to a comparable head token.

    Strips length / precision parens (`VARCHAR(36)` → `VARCHAR`) and
    multi-word modifiers (`UNSIGNED INTEGER` → `UNSIGNED`), then
    uppercases. Returns "" for falsy input.
    """
    if not t:
        return ""
    head = t.split("(", 1)[0].strip().split()[0]
    return head.upper()


def _types_equivalent(live: str, declared: str) -> bool:
    """True if two SQL type names compare equal under SQLite's affinity rules.

    Both inputs are normalised via `_normalise_sql_type` (strips
    `(length)` parens, takes the first whitespace-separated token,
    uppercases). The `_EQUIVALENT_TYPES` map then declares a small set
    of "same-affinity" pairs the drift detector should treat as equal.
    """
    a = _normalise_sql_type(live)
    b = _normalise_sql_type(declared)
    if a == b:
        return True
    return frozenset({a, b}) in _EQUIVALENT_TYPES


# ─── engines (process-global, SYNCHRONOUS) + per-loop locks ──────────────
#
# The engines are SQLAlchemy's SYNCHRONOUS engines, and the public API stays
# `async def`. That combination is deliberate, and it is where most of this
# layer's speed comes from.
#
# A local SQLite query is not I/O in any sense asyncio cares about: measured on
# a warm page cache, a point SELECT is **4 microseconds**. Reaching it through
# the async stack cost 247 — aiosqlite hands the statement to a worker thread
# and back (one full event-loop yield per statement), and SQLA's async layer
# bridges every call through greenlets on top. Both exist to keep the loop free
# during a wait that, here, does not exist: the loop was being suspended for
# 4 microseconds of work and 240 of ceremony.
#
# So the driver work happens inline, on the loop, and the awaitable surface is
# kept for the callers (and because the operations that CAN be slow still need
# it). The contract that keeps this honest is bounded work — see
# `AsyncDB._in_thread`: anything that can touch a whole table (fetch_all,
# stream, aggregate, unfiltered scans, batch writes) is off-loaded to a thread;
# anything bounded by a primary key, an index or a LIMIT runs inline.
#
# Engines are process-global because a synchronous engine has no event loop to
# be bound to — which also removes the "bound to a different event loop" class
# of failure the per-loop registry existed to work around. What still binds to
# a loop is `asyncio.Lock`, so the write locks stay per-loop.

_engines_by_path: "dict[str, tuple[Engine, Engine]]" = {}
_initialized_dbs: "set[str]" = set()
_db_state_guard = threading.Lock()

# One MetaData per database FILE, shared by every AsyncDB pointing at it.
#
# It has to be shared, and the reason is foreign keys. SQLAlchemy resolves
# `ForeignKey("parent.id")` by looking `parent` up in the SAME MetaData as the
# child, so with a MetaData per AsyncDB — the natural shape here, one instance
# per table — a cross-table FK could never resolve and DDL died with
# `NoReferencedTableError`. The `foreign_key` field metadata was effectively
# undeclarable in the only layout the library encourages.
#
# The file is the right boundary and not a compromise: SQLite cannot express a
# constraint across two database files any more than it can make a transaction
# atomic across them, so two tables that can reference each other are exactly
# two tables in one file.
#
# Never evicted. A MetaData is a pure in-memory description (no connections, no
# per-loop state), one per path, and paths are resolved and bounded by the
# application's own set of databases.
_metadata_by_path: "dict[str, MetaData]" = {}
# (path, table_name) → the schema class that registered it, so a second AsyncDB
# claiming the same table can be told apart from a re-import of the same one.
_table_owners: "dict[tuple[str, str], type]" = {}


def _shared_table(db_path: Path, table_name: str, schema_class: type) -> tuple[MetaData, Table]:
    """The MetaData for `db_path` and this schema's Table inside it.

    Re-declaring a table is normal and has two shapes, both supported:

      * the SAME schema class again — a module imported twice, a fixture
        rebuilt. The Table already there is returned, so the two AsyncDB
        instances share one object instead of fighting over the name. The test
        is identity, not shape: see the note at the rebuild below.
      * a DIFFERENT schema on the same table — which is what schema DRIFT
        looks like from inside one process: a dataclass that gained a column
        since the table was created. The new declaration REPLACES the old one
        in the MetaData, because it is the one describing what the caller now
        wants; the retrofit path (`ALTER TABLE ADD COLUMN` + the drift check)
        is what reconciles it with the live database.

    Replacing rather than refusing keeps the second case working, and it is
    also the honest default: before the MetaData was shared, each AsyncDB
    silently had its own view of the table anyway, so "the newest declaration
    describes the table" is what the library already did — only now the other
    tables on this file can see it, which is the whole point.
    """
    key = str(db_path)
    with _db_state_guard:
        metadata = _metadata_by_path.get(key)
        if metadata is None:
            metadata = _metadata_by_path[key] = MetaData()

        existing = metadata.tables.get(table_name)
        if existing is not None:
            if _table_owners.get((key, table_name)) is schema_class:
                return metadata, existing
            # A DIFFERENT class: rebuild, always. Comparing the two by column
            # names would look like a safe shortcut and is not — v1 and v2 of a
            # schema routinely have identical columns and differ only in the
            # field metadata (`index`, `unique`), which lives in the Table's
            # indexes rather than its columns. Reusing v1's Table there means
            # the declared index is never created and the constraint reads as
            # enforced while duplicates keep being accepted.
            logger.debug(
                f"table {table_name!r} on {key} re-declared by "
                f"{schema_class.__name__}; the previous declaration is replaced"
            )
            metadata.remove(existing)

        table = _table_from_schema(metadata, table_name, schema_class)
        _table_owners[(key, table_name)] = schema_class
        return metadata, table


_db_state_by_loop: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict]" = (
    weakref.WeakKeyDictionary()
)


def _db_loop_state() -> dict:
    """Return per-loop lock state.

    Keys:
      - "locks"            dict[str, asyncio.Lock]   per-db-path write lock
      - "schema_init_lock" asyncio.Lock              serialises schema init
      - "read_conns"       dict[str, Connection]     shared read connection
    """
    loop = asyncio.get_running_loop()
    with _db_state_guard:
        state = _db_state_by_loop.get(loop)
        if state is None:
            state = {"locks": {}, "schema_init_lock": asyncio.Lock(), "read_conns": {}}
            _db_state_by_loop[loop] = state
        return state


def _drop_engines(db_path: Path) -> None:
    """Forget the cached engines for `db_path` (stale connection recovery)."""
    key = str(db_path)
    with _db_state_guard:
        _engines_by_path.pop(key, None)
        for k in [k for k in _initialized_dbs if k.startswith(key)]:
            _initialized_dbs.discard(k)


class _AwaitableConn:
    """A synchronous `Connection` that still answers `await conn.execute(...)`.

    The connection handed to `transaction()` and to a migration's `upgrade()`
    is now synchronous, but every caller in the wild writes
    `await conn.execute(text(...))` — including migration files sitting in
    other repositories, which this package cannot edit and must not break. So
    `execute` stays a coroutine function that does its work inline and hands
    back the very same `CursorResult` the async API returned (SQLAlchemy's
    async `execute` buffers into a sync result anyway, so `.fetchall()`,
    `.scalar()`, `.mappings()` and `.rowcount` behave identically).

    Everything else is proxied straight through, so `conn.begin()`,
    `conn.in_transaction()`, `conn.exec_driver_sql()` keep working.
    """

    __slots__ = ("_conn",)

    def __init__(self, conn: Connection):
        self._conn = conn

    async def execute(self, *args, **kwargs):
        return self._conn.execute(*args, **kwargs)

    async def run_sync(self, fn, *args, **kwargs):
        """Mirror of AsyncConnection.run_sync — the connection IS sync now."""
        return fn(self._conn, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._conn, name)


ConnLike = Union[Connection, _AwaitableConn]


def _raw_conn(conn: "ConnLike") -> Connection:
    """Unwrap a caller-supplied connection to the synchronous one underneath."""
    return conn._conn if isinstance(conn, _AwaitableConn) else conn


# ─── TypeDecorators ──────────────────────────────────────────────────────
#
# Each Python-level type that needs a roundtrip gets a SQLA TypeDecorator.
# These attach to specific columns; bare `str` columns get plain `Text`
# (no JSON guessing). This is the structural fix for the previous
# `_deserialize_value` `json.loads(value)` fallback that silently
# converted str-holding-"123" into int 123.


def _json_encode_default(v):
    """JSON default for nested values embedded in list/dict/set columns."""
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, time):
        return v.isoformat()
    if isinstance(v, bytes):
        return _B64_PREFIX + base64.b64encode(v).decode("ascii")
    if isinstance(v, enum.Enum):
        return v.value
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, (set, frozenset)):
        return list(v)
    if is_dataclass(v):
        return asdict(v)
    raise TypeError(f"Object of type {type(v).__name__} is not JSON serialisable")


# Annotation-driven key coercion for JSON dict columns. When a dataclass
# field is declared `Dict[K, V]`, the column-level `_JSONDecorator` reads
# `K` and uses the matching callable to restore the original key type on
# load. JSON serialises non-string keys as strings (lossy by design), so
# without this the read path returns `Dict[str, V]` regardless of `K`.
#
# Limitation: only applies at the column boundary. Nested dicts inside a
# list/dict column (e.g. `List[Dict[int, X]]`) are NOT coerced — JSON
# loses that nesting information and we can't recover it from the
# annotation. If you need full-fidelity nested keys, coerce at the
# application boundary.
_KEY_COERCIONS: Dict[type, Callable[[Any], Any]] = {
    int: int,
    float: float,
    Decimal: Decimal,
    uuid.UUID: uuid.UUID,
}


def _coercion_for_key_type(key_type) -> Optional[Callable[[Any], Any]]:
    """Return a callable that converts a JSON-loaded `str` key back to `key_type`.

    Returns None for `str` (the no-op default) and for types we don't
    know how to coerce (the value falls through as str — preserving the
    pre-coercion behaviour for unrecognised types).
    """
    if key_type is None or key_type is str:
        return None
    return _KEY_COERCIONS.get(key_type)


def _stringify_dict_key(k):
    """Convert a dict key to something `json.dumps` will accept.

    JSON only allows str / int / float / bool / None as keys, and
    `json.dumps` stringifies the non-string variants automatically.
    Other types (Decimal, UUID, etc.) need explicit conversion or
    `json.dumps` raises TypeError. We mirror the value-side
    `_json_encode_default` for symmetry, so a key written via the JSON
    column comes back through the right coercion on read.
    """
    if isinstance(k, (str, int, float, bool)) or k is None:
        return k
    if isinstance(k, Decimal):
        return str(k)
    if isinstance(k, uuid.UUID):
        return str(k)
    if isinstance(k, enum.Enum):
        return k.value
    # Fall through — json.dumps will raise TypeError with a clear message.
    return k


class _JSONDecorator(TypeDecorator):
    """JSON-encoded TEXT column for list / dict / set / nested dataclass fields.

    NEVER attach this to a bare `str` column — that is the bug being fixed.

    For `Dict[K, V]` columns, pass `key_coercion=<callable>` to restore
    the original key type on read (`{1: 0.5}` round-trips as `{1: 0.5}`
    instead of `{"1": 0.5}`). Without `key_coercion`, JSON behavior is
    preserved (all keys are str on read).

    Other annotation-vs-runtime footguns to remember (dataclass annotations
    are NOT enforced at runtime — these are JSON limitations):

      - `Tuple[X, ...]`: JSON has no tuple, so `(1, 2)` round-trips as
        `[1, 2]` (a list). Declare `List[X]` if you care about the runtime
        type matching the annotation.
      - `Set[X]` / `FrozenSet[X]`: same — round-trips as list (order loss).
      - Nested dict keys (`List[Dict[int, X]]`) are NOT coerced —
        `key_coercion` only fires at the column boundary.
    """
    impl = Text
    cache_ok = True

    def __init__(self, *, key_coercion: Optional[Callable[[Any], Any]] = None):
        self._key_coercion = key_coercion
        super().__init__()

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, dict):
            # JSON spec only allows str/int/float/bool/None keys. Convert
            # other key types (Decimal, UUID, Enum, ...) via the symmetric
            # helper so `key_coercion` can reverse them on read.
            value = {_stringify_dict_key(k): v for k, v in value.items()}
        return json.dumps(value, default=_json_encode_default)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        decoded = json.loads(value)
        if self._key_coercion is not None and isinstance(decoded, dict):
            return {self._key_coercion(k): v for k, v in decoded.items()}
        return decoded


class _UTCDateTimeDecorator(TypeDecorator):
    """TZ-aware datetime stored as ISO 8601 with offset (Text column).

    SQLA's stock `DateTime(timezone=True)` on SQLite parses datetimes
    through a regex that drops the offset on read — naive datetime
    returned for what was stored as tz-aware. We bypass it by handling
    `isoformat()` / `fromisoformat()` ourselves; both round-trip the
    offset correctly.

    Naive datetimes on the bind path are assumed UTC (legacy data from
    before the UTC switch). The result path always returns tz-aware UTC.
    """
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class _TimeDecorator(TypeDecorator):
    """Time-of-day stored as ISO 8601 (Text column).

    `datetime.time` has no native SQLite storage class, so — like
    `_UTCDateTimeDecorator` does for `datetime` — we store `isoformat()`
    ("13:45:00", "09:30:00.500000", optionally "13:45:00+02:00") and parse
    it back with `time.fromisoformat()`. Without this, a `time` field would
    fall through to `_JSONDecorator`, whose `json.loads` raises on the
    colons in an ISO time string. Any tzinfo on the value is preserved.
    """
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return value.isoformat()

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, time):
            return value
        return time.fromisoformat(value)


class _DecimalDecorator(TypeDecorator):
    """Lossless Decimal roundtrip via TEXT storage.

    SQLite's NUMERIC affinity coerces high-precision decimals through
    `float`, losing digits past ~15-17. Storing the Decimal as its string
    representation in a TEXT column preserves arbitrary precision.
    """
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return str(value if isinstance(value, Decimal) else Decimal(str(value)))

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return Decimal(value)


class _UUIDDecorator(TypeDecorator):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else str(value)

    def process_result_value(self, value, dialect):
        return None if value is None else uuid.UUID(value)


class _EnumDecorator(TypeDecorator):
    """Stores `enum.value`; reconstructs via `enum_cls(value)`.

    Pass `impl_cls=Integer` for IntEnum/IntFlag, `impl_cls=Text` for
    StrEnum and regular Enum. The class-level `impl = Text` satisfies
    SQLA's "TypeDecorator needs an impl" check; `load_dialect_impl`
    overrides to the per-instance impl at dialect-compile time.
    """
    impl = Text
    cache_ok = True

    def __init__(self, enum_cls, *, impl_cls=Text):
        self.enum_cls = enum_cls
        self._impl_cls = impl_cls
        super().__init__()

    @property
    def python_type(self):
        return self.enum_cls

    def load_dialect_impl(self, dialect):
        return dialect.type_descriptor(self._impl_cls())

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return value.value if isinstance(value, enum.Enum) else value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return self.enum_cls(value)


def _python_to_sa_type(ftype):
    """Map a Python type (already unwrapped from Optional) to a SQLA type."""
    if ftype is bytes:
        return LargeBinary()
    if ftype is datetime:
        # Custom decorator (not SQLA's DateTime(timezone=True)) because
        # SQLA's SQLite path strips the offset on read. The decorator
        # stores ISO-with-offset and reads back tz-aware UTC datetimes,
        # closing the naive-local timestamp footgun.
        return _UTCDateTimeDecorator()
    if ftype is date:
        return Date()
    if ftype is time:
        # No native SQLite time type and SQLA's Time() doesn't round-trip
        # on SQLite; store ISO 8601 text (see _TimeDecorator). Without this
        # branch `time` falls through to the JSON fallback, which can't read
        # an ISO time string back ("12:03:00" is not valid JSON).
        return _TimeDecorator()
    if ftype is float:
        return Float()
    if ftype is Decimal:
        return _DecimalDecorator()
    if ftype is uuid.UUID:
        return _UUIDDecorator()
    if ftype is bool:
        # bool MUST be checked before int (bool is a subclass of int).
        return Boolean()
    if isinstance(ftype, type) and issubclass(ftype, enum.Enum):
        impl_cls = Integer if issubclass(ftype, int) else Text
        return _EnumDecorator(ftype, impl_cls=impl_cls)
    if isinstance(ftype, type) and issubclass(ftype, int):
        return Integer()
    if isinstance(ftype, type) and issubclass(ftype, str):
        return Text()
    origin = getattr(ftype, "__origin__", None) if ftype is not None else None
    if origin is dict:
        # Dict[K, V] — read K and wire up key-coercion on the decoder so
        # the runtime keys match the annotation instead of always being
        # str (JSON's only key flavour).
        args = getattr(ftype, "__args__", ())
        key_type = args[0] if len(args) >= 1 else None
        return _JSONDecorator(key_coercion=_coercion_for_key_type(key_type))
    if origin in (list, set, frozenset, tuple):
        return _JSONDecorator()
    if ftype in (list, dict, set, frozenset, tuple):
        return _JSONDecorator()
    if isinstance(ftype, type) and is_dataclass(ftype):
        return _JSONDecorator()
    # Last-resort: serialise via JSON. Surfaced at DEBUG so the caller can
    # see they're hitting the fallback (e.g. for `Any`-typed columns).
    logger.debug(f"Falling back to JSONDecorator for type {ftype!r}")
    return _JSONDecorator()


def _table_from_schema(metadata: MetaData, table_name: str, schema_class: type) -> Table:
    """Build a SQLA `Table` from a dataclass schema.

    Honours field metadata: `primary_key`, `unique`, `index`, `required`,
    `foreign_key` (+ optional `on_delete`). `unique` and `index` become
    separate `Index` objects (not inline) so schema-drift `ALTER TABLE ADD
    COLUMN` + a fresh `CREATE INDEX IF NOT EXISTS` keep working uniformly.

    `foreign_key` takes `"<table>.<column>"` and resolves against the OTHER
    tables declared on the same database file, which the shared MetaData
    (`_shared_table`) is what makes possible:

        parent_id: str = field(default=None, metadata={
            "index": True, "foreign_key": "parent.id", "on_delete": "CASCADE"})

    Declaration order does not matter (SQLAlchemy resolves the reference when
    the DDL is emitted, not here), but the referenced model's AsyncDB must
    have been CONSTRUCTED — normally: its module imported — before the first
    use of this one, and must point at the same file. `PRAGMA foreign_keys=ON`
    is already set on every connection, so the constraint is enforced by
    SQLite: an orphan INSERT raises IntegrityError and `ON DELETE CASCADE`
    happens in the database, not in the application.

    Two optional class-level attributes are honoured:
      - `__unique_together__`: list of column-name tuples, e.g.
        `[("user_id", "external_id")]`. Each tuple becomes a
        `UniqueConstraint` and is registered as a valid
        composite conflict target on the AsyncDB instance.
      - `__table_constraints__`: list of raw SQL constraint strings
        (CHECK, etc.). Use `__unique_together__` for composite uniques
        — it's structurally introspectable, the raw-SQL form is not.
    """
    type_hints = get_type_hints(schema_class, include_extras=False)
    columns: list[Column] = []
    indexes: list[Index] = []

    for f in fields(schema_class):
        col_name = _validate_identifier(f.name)
        declared = type_hints.get(f.name)
        ftype = _unwrap_optional(declared)
        sa_type = _python_to_sa_type(ftype)

        has_default = (
            f.default is not dataclasses.MISSING
            or f.default_factory is not dataclasses.MISSING
        )
        is_optional = _get_union_origin(declared) is not None
        # A column is nullable if the declared type is Optional, OR the
        # dataclass field has a default (so callers can omit it), OR the
        # metadata explicitly opts out of NOT NULL.
        nullable = (not f.metadata.get("required", True)) or has_default or is_optional

        col_args: list = [col_name, sa_type]
        if (fk := f.metadata.get("foreign_key")):
            on_delete = f.metadata.get("on_delete")
            col_args.append(
                ForeignKey(fk, ondelete=on_delete) if on_delete else ForeignKey(fk)
            )
        col = Column(
            *col_args,
            primary_key=bool(f.metadata.get("primary_key")),
            nullable=nullable and not f.metadata.get("primary_key"),
        )
        columns.append(col)

        if f.metadata.get("unique"):
            indexes.append(Index(f"idx_{table_name}_{col_name}_unique", col, unique=True))
        if f.metadata.get("index"):
            indexes.append(Index(f"idx_{table_name}_{col_name}", col))

    # Composite UNIQUE constraints from __unique_together__.
    valid_col_names = {c.name for c in columns}
    unique_together = getattr(schema_class, "__unique_together__", ()) or ()
    composite_constraints: list[UniqueConstraint] = []
    for cols in unique_together:
        if not isinstance(cols, (tuple, list)) or len(cols) < 2:
            raise ValueError(
                f"{schema_class.__name__}.__unique_together__: each entry must be "
                f"a tuple/list of at least 2 column names; got {cols!r}"
            )
        for c in cols:
            if c not in valid_col_names:
                raise ValueError(
                    f"{schema_class.__name__}.__unique_together__: unknown column "
                    f"{c!r}; must be a declared dataclass field"
                )
        validated = tuple(_validate_identifier(c) for c in cols)
        composite_constraints.append(
            UniqueConstraint(
                *validated,
                name=f"uq_{table_name}_" + "_".join(validated),
            )
        )

    table_constraints = getattr(schema_class, "__table_constraints__", ()) or ()
    return Table(
        table_name, metadata,
        *columns,
        *indexes,
        *composite_constraints,
        *table_constraints,
    )


# ─── PRAGMAs + engine factory ────────────────────────────────────────────

_PRAGMAS: tuple[str, ...] = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA mmap_size=268435456",     # 256 MB memory-mapped reads
    "PRAGMA temp_store=MEMORY",
    "PRAGMA cache_size=-65536",       # 64 MB (negative = KiB)
    "PRAGMA busy_timeout=30000",
    "PRAGMA wal_autocheckpoint=1000",
)


def _attach_pragmas(engine: Engine) -> None:
    """Install `connect` + `begin` event listeners on the engine.

    The `connect` listener sets all PRAGMAs on each new physical SQLite
    connection (reader and writer pools see them independently — both
    must be set) and disables Python sqlite3's auto-COMMIT-before-DDL
    so SQLA can wrap CREATE TABLE / ALTER inside real transactions.

    The `begin` listener emits the actual `BEGIN` statement since
    setting `isolation_level=None` switches us to manual transaction
    control. Without this, a failing migration that issued DDL would
    leave the DDL committed even though SQLA "rolled back" the txn —
    because pysqlite had implicitly committed it first.
    """

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        try:
            for sql in _PRAGMAS:
                cur.execute(sql)
        finally:
            cur.close()
        # Make DDL transactional. See SQLA "Serializable isolation /
        # Savepoints / Transactional DDL" recipe for pysqlite.
        try:
            dbapi_conn.isolation_level = None
        except AttributeError:
            pass

    @event.listens_for(engine, "begin")
    def _do_begin(conn):
        # With isolation_level=None we issue BEGIN ourselves so SQLA's
        # transactional semantics work for DDL too.
        conn.exec_driver_sql("BEGIN")


class _EngineDisposalMarker:
    """Mutable token shared between `close()` and the GC finalizer.

    Stored in `_engine_markers` (a WeakKeyDictionary keyed on the engine,
    because SQLA's Engine does not accept arbitrary attributes, so we can't
    private attribute). `close()` flips `disposed=True` BEFORE actually
    disposing; the GC finalizer, when it later fires, checks the flag:
    still False ⇒ the user dropped the engine without calling `close()`,
    and we emit a `ResourceWarning` so the leak doesn't go silent.
    """
    __slots__ = ("disposed",)

    def __init__(self):
        self.disposed = False


# Engine → marker map. WeakKeyDictionary auto-drops entries when the
# engine is GC'd; the marker survives until the finalizer also runs
# (the finalizer closes over the marker via its arg list).
_engine_markers: "weakref.WeakKeyDictionary[Engine, _EngineDisposalMarker]" = (
    weakref.WeakKeyDictionary()
)


def _maybe_warn_undisposed_engine(db_path: str, marker: _EngineDisposalMarker) -> None:
    """Emit ResourceWarning iff the engine reached GC without being disposed.

    Runs in `weakref.finalize` context, so it must be import-time-safe and
    not require a running event loop. We only warn — the actual fd-level
    cleanup is the OS's responsibility (the aiosqlite worker thread is
    daemon-marked so it dies with the process either way).
    """
    if marker.disposed:
        return
    warnings.warn(
        f"AsyncDB engine for {db_path!r} was garbage-collected without "
        f"`await db.close()`. File descriptors and the aiosqlite worker "
        f"thread will leak until process exit. Always call `await db.close()` "
        f"or use `async with AsyncDB(...)`.",
        ResourceWarning,
        stacklevel=2,
    )


def _get_engines(db_path: Path) -> tuple[Engine, Engine]:
    """Return the process-wide (writer, reader) engines for `db_path`.

    No active disposal happens from a finalizer — a finalizer triggered at GC
    may run at an arbitrary point. Instead we register a `ResourceWarning`
    finalizer that fires only when the engine was dropped without `close()`,
    surfacing the leak instead of letting it go silent.
    """
    key = str(db_path)
    with _db_state_guard:
        pair = _engines_by_path.get(key)
        if pair is not None:
            return pair

    url = f"sqlite:///{db_path}"
    writer = create_engine(
        url,
        poolclass=StaticPool,           # one underlying conn; write lock serialises
        connect_args={"timeout": 30.0, "check_same_thread": False},
    )
    reader = create_engine(
        url,
        pool_size=4,
        max_overflow=4,
        pool_timeout=30.0,
        pool_recycle=3600,
        connect_args={"timeout": 30.0, "check_same_thread": False},
    )
    _attach_pragmas(writer)
    _attach_pragmas(reader)

    # Attach a disposal marker per engine via a WeakKeyDictionary. The
    # finalizer captures the marker in its arg list so it survives even
    # after the WKD entry is auto-cleaned on engine GC.
    for engine in (writer, reader):
        marker = _EngineDisposalMarker()
        _engine_markers[engine] = marker
        weakref.finalize(engine, _maybe_warn_undisposed_engine, key, marker)

    with _db_state_guard:
        # Another thread may have built the pair while we were: keep theirs and
        # drop ours, so the process never holds two engines for one file.
        existing = _engines_by_path.get(key)
        if existing is not None:
            writer.dispose()
            reader.dispose()
            return existing
        _engines_by_path[key] = (writer, reader)
    return writer, reader


# ─── schema versioning (tier 2): file-based migrations ────────────────────
#
# Convention: AsyncDB probes `<db_path>.parent / "migrations"` and applies
# any script whose `version` exceeds the live `PRAGMA user_version`.
# Migrations are plain Python modules:
#
#     # migrations/001_add_email.py
#     version = 1
#     description = "Add email column to users"
#
#     async def upgrade(conn):
#         conn.execute(text("ALTER TABLE users ADD COLUMN email TEXT"))
#
# Validation rules:
#   - filenames match `NNN_*.py` where NNN is the version (zero-padded ok)
#   - versions start at 1 and form a contiguous sequence (no gaps, no dupes)
#   - each module defines `version: int`, `description: str`,
#     `async def upgrade(conn)`.
#
# Forward-only by design. We deliberately do NOT support `downgrade()`:
# automated schema rollbacks are dangerous in practice and tempt users
# to write reversible migrations that aren't actually reversible (e.g.
# SQLite can't DROP COLUMN before 3.35; backfilled data is often lost
# on rollback). For ad-hoc rollback, restore from backup or write a
# forward migration that undoes the change.

_MIGRATION_FILENAME = re.compile(r"^(\d+)_[A-Za-z0-9_]+\.py$")


@dataclass(frozen=True)
class _Migration:
    version: int
    description: str
    upgrade: Callable[["ConnLike"], Awaitable[None]]
    source: Path


# Process-wide cache for migration modules: key is (abs path, mtime_ns).
# A file edit invalidates the cache automatically; same-file re-imports
# are reused. Stops `_ensure_engines()` from re-executing every migration
# module on every (instance, loop) — important for test suites that
# create many short-lived AsyncDBs against a shared migrations dir.
_migration_module_cache: Dict[Tuple[str, int], Any] = {}


def _load_migration_module(fp: Path) -> Any:
    """Import (or return cached) the migration module at `fp`.

    Cache key is (resolved path, mtime_ns). Editing the file produces a
    new key; identical re-imports are free. Any error from the module's
    top-level execution (`SyntaxError`, `ImportError`, anything raised
    at import time) is wrapped in a `RuntimeError` that points to the
    migration file by name — otherwise the traceback shows the synthetic
    `_esuls_migration_*` module name and the user has to dig.
    """
    stat = fp.stat()
    key = (str(fp.resolve()), stat.st_mtime_ns)
    module = _migration_module_cache.get(key)
    if module is not None:
        return module
    # Include the mtime in the synthetic module name so a re-imported
    # edited file doesn't collide in sys.modules with its prior version.
    mod_name = f"_esuls_migration_{fp.stem}_{stat.st_mtime_ns}"
    spec = importlib.util.spec_from_file_location(mod_name, fp)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load migration spec: {fp.name}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        raise RuntimeError(
            f"failed to load migration {fp.name}: {type(e).__name__}: {e}"
        ) from e
    _migration_module_cache[key] = module
    return module


def _discover_migrations(migrations_dir: Path) -> List[_Migration]:
    """Load + sort + validate migration scripts in `migrations_dir`.

    Raises `ValueError` on gaps, duplicate versions, or invalid module
    structure. Returns an empty list if the directory has no matching
    files (an empty `migrations/` is not an error).
    """
    candidates = sorted(
        p for p in migrations_dir.glob("*.py")
        if _MIGRATION_FILENAME.match(p.name)
    )
    migrations: List[_Migration] = []
    for fp in candidates:
        module = _load_migration_module(fp)

        version = getattr(module, "version", None)
        if not isinstance(version, int) or version < 1:
            raise ValueError(
                f"{fp}: `version` must be a positive int, got {version!r}"
            )
        description = getattr(module, "description", "")
        upgrade = getattr(module, "upgrade", None)
        if not callable(upgrade):
            raise ValueError(f"{fp}: missing `async def upgrade(conn)`")
        # Any `downgrade` defined in the module is ignored — forward-only.
        migrations.append(_Migration(
            version=version,
            description=str(description),
            upgrade=upgrade,
            source=fp,
        ))

    migrations.sort(key=lambda m: m.version)
    seen: set[int] = set()
    for m in migrations:
        if m.version in seen:
            raise ValueError(
                f"duplicate migration version {m.version} in {migrations_dir}"
            )
        seen.add(m.version)
    if migrations:
        if migrations[0].version != 1:
            raise ValueError(
                f"migrations must start at version 1; first is {migrations[0].version}"
            )
        for prev, curr in zip(migrations, migrations[1:]):
            if curr.version != prev.version + 1:
                raise ValueError(
                    f"migration version gap: {prev.version} → {curr.version} "
                    f"(expected {prev.version + 1})"
                )
    return migrations


def discover_migrations(migrations_dir: Path) -> List[Dict[str, Any]]:
    """Public introspection of migration files without opening a db.

    Returns one dict per migration with `version`, `description`, and
    `source` (filename). For "applied vs pending" status against a real
    db, instantiate AsyncDB and call `await db.list_migrations()`.
    """
    return [
        {
            "version": m.version,
            "description": m.description,
            "source": m.source.name,
        }
        for m in _discover_migrations(migrations_dir)
    ]


async def _apply_pending_migrations(
    conn: "ConnLike", migrations_dir: Path
) -> List[int]:
    """Apply all migrations whose `version` > current `PRAGMA user_version`.

    Runs inside the caller's transaction (`writer.begin()`), so a
    migration failure rolls back everything: the user_version stays at
    its pre-migration value and the data is untouched. Returns the list
    of applied versions for callers that want to log / verify.
    """
    if not migrations_dir.is_dir():
        return []
    migrations = _discover_migrations(migrations_dir)
    if not migrations:
        return []
    current = (conn.execute(text("PRAGMA user_version"))).scalar() or 0
    applied: List[int] = []
    for m in migrations:
        if m.version <= current:
            continue
        logger.info(
            f"applying migration {m.version}: {m.description} ({m.source.name})"
        )
        # Migration files live in the SITE's repo and are written
        # `async def upgrade(conn): await conn.execute(...)`. They must keep
        # working untouched, so they get the awaitable adapter, never the
        # bare connection.
        await m.upgrade(_AwaitableConn(_raw_conn(conn)))
        # PRAGMA user_version doesn't accept bound parameters; embed an
        # already-validated int directly.
        conn.execute(text(f"PRAGMA user_version = {int(m.version)}"))
        applied.append(m.version)
    return applied


T = TypeVar("T")
SchemaType = TypeVar("SchemaType", bound="_ModelBase")


@dataclass
class _ModelBase:
    """Shared `to_dict` / `from_dict` (transport-level JSON encoding).

    No fields of its own. Concrete bases — `IdModel`, `IntIdModel`,
    `TimestampedModel` — add primary-key and (optionally) timestamp
    fields. Compose by inheritance: `class MyRow(TimestampedModel)`
    for the legacy default, `class MyRow(IdModel)` for "id only, no
    timestamps", `class MyRow(IntIdModel)` for autoincrement int PK.
    """

    def to_dict(self) -> dict:
        result = {}
        for f in fields(self):
            value = getattr(self, f.name)
            result[f.name] = self._serialize_value(value)
        return result

    @classmethod
    def from_dict(cls: Type[SchemaType], data: dict) -> SchemaType:
        """Decode any `b64:...` strings back into bytes for bytes-typed fields.

        Non-bytes fields pass through unchanged — a `str` field that happens
        to hold the literal text `"b64:..."` is preserved as-is.
        """
        type_hints = get_type_hints(cls)
        converted: Dict[str, Any] = {}
        for k, v in data.items():
            if (
                isinstance(v, str)
                and v.startswith(_B64_PREFIX)
                and _is_bytes_field(type_hints.get(k))
            ):
                converted[k] = base64.b64decode(v[len(_B64_PREFIX):])
            else:
                converted[k] = v
        return cls(**converted)

    def _serialize_value(self, value):
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, bytes):
            return _B64_PREFIX + base64.b64encode(value).decode("ascii")
        if isinstance(value, enum.Enum):
            return value.value
        if isinstance(value, uuid.UUID):
            return str(value)
        if isinstance(value, Decimal):
            # Lossless. Previous version coerced to float() — that silently
            # truncated precision for big-decimal payloads.
            return str(value)
        if isinstance(value, (list, tuple, set, frozenset)):
            return [self._serialize_value(v) for v in value]
        if isinstance(value, dict):
            return {k: self._serialize_value(v) for k, v in value.items()}
        if hasattr(value, "to_dict"):
            return value.to_dict()
        if is_dataclass(value):
            return asdict(value)
        return value


@dataclass
class IdModel(_ModelBase):
    """String-id only — for cache/lookup tables that don't need timestamps."""
    id: str = field(default_factory=lambda: str(uuid.uuid4()), metadata={"primary_key": True})


@dataclass
class IntIdModel(_ModelBase):
    """Integer autoincrement PK. SQLite assigns the rowid on insert when
    `id` is None. Use for high-throughput tables where UUID4-as-string PK
    fragments the B-tree."""
    id: Optional[int] = field(default=None, metadata={"primary_key": True})


@dataclass
class TimestampedModel(IdModel):
    """String id + `created_at` / `updated_at` (the legacy default).

    `created_at` is preserved across upserts (excluded from the SET
    clause of ON CONFLICT DO UPDATE). `updated_at` is overwritten on
    every save / update_fields.
    """
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class TimestampedIntModel(IntIdModel):
    """Integer autoincrement PK + `created_at` / `updated_at`.

    The high-throughput counterpart to `TimestampedModel`. Use when you
    want the perf characteristics of an INTEGER PRIMARY KEY (no UUID
    string fragmentation, smaller indexes, faster joins) but also need
    the auto-managed timestamp columns.
    """
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


# Backward-compat alias — `BaseModel` was the old single class; everything
# that inherits from it keeps working because `TimestampedModel` has the
# same fields + methods. New code can prefer the explicit names.
BaseModel = TimestampedModel


class AsyncDB(Generic[SchemaType]):
    """Async SQLite layer with dataclass schemas, built on SQLAlchemy Core.

    Locks and engines are scoped to the running event loop via the
    per-loop registry — the same AsyncDB instance can be used across
    multiple `asyncio.run()` calls without `bound to a different event
    loop` crashes.
    """

    # Operator suffixes recognised on filter kwargs (e.g. `count__gt=5`).
    # Anything else is treated as a column name (so a literal `__` in a
    # column name still works as long as the schema declares it).
    OPERATOR_MAP = frozenset({
        "gt", "lt", "gte", "lte", "neq", "like", "in", "eq",
        "is_null", "not_null", "not_in", "between",
    })

    def __init__(
        self,
        db_path: Union[str, Path],
        table_name: str,
        schema_class: Type[SchemaType],
        *,
        strict_schema: bool = False,
    ):
        """
        `strict_schema=True` promotes schema-drift warnings (type mismatch
        between dataclass and DB, NOT NULL violated by retrofit, orphan
        columns in the DB) into `RuntimeError`. Default is to log a
        warning and proceed — useful for dev/iteration but a footgun in
        prod, hence the opt-in strict mode.
        """
        if not is_dataclass(schema_class):
            raise TypeError(f"Schema must be a dataclass, got {schema_class}")

        self.db_path = Path(db_path).resolve()
        self.schema_class = schema_class
        self.table_name = _validate_identifier(table_name)
        self.strict_schema = strict_schema
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # Migrations convention: `./migrations/` next to the db file.
        # Probed on every `_init_or_migrate_schema`; an absent or empty
        # directory is silently a no-op (so callers without migrations
        # pay no cost). Per-instance attribute so users can override it
        # for testing without monkeypatching constants.
        self.migrations_dir: Path = self.db_path.parent / "migrations"

        # Validate field identifiers up front so a bad schema fails at
        # construction time rather than on first save.
        for f in fields(schema_class):
            _validate_identifier(f.name)

        # Whitelist used by `_validate_column` to gate every column-name
        # interpolated into SQL (filter keys, order_by, update_fields kwargs).
        self._valid_columns: frozenset[str] = frozenset(
            f.name for f in fields(schema_class)
        )
        # Columns individually eligible as upsert conflict targets.
        self._conflict_targets: set[str] = {
            f.name for f in fields(schema_class)
            if f.metadata.get("primary_key") or f.metadata.get("unique")
        }
        # Composite conflict targets declared via `__unique_together__`.
        # Stored as frozensets so the caller can pass the columns in any
        # order: `on_conflict=("a", "b")` matches `[("b", "a")]`.
        unique_together = getattr(schema_class, "__unique_together__", ()) or ()
        self._composite_conflict_targets: set[frozenset[str]] = {
            frozenset(cols) for cols in unique_together
        }
        self._db_key = f"{self.db_path}:{self.table_name}:{self.schema_class.__name__}"

        # Build the SQLA Table synchronously — pure-Python, no I/O. The
        # MetaData is shared with every other AsyncDB on this file so that a
        # `foreign_key` can find the table it points at (see `_shared_table`).
        self._metadata, self._table = _shared_table(
            self.db_path, self.table_name, schema_class
        )
        # Engines + schema flag cached on the instance: every call used to walk
        # a WeakKeyDictionary keyed on the running loop and a dict keyed on the
        # path just to reach them, and at 4 microseconds of actual query that
        # bookkeeping is not a rounding error.
        self._engines: Optional[tuple[Engine, Engine]] = None
        self._schema_ready = False
        # Columns a query can be BOUNDED by: the primary key and anything
        # declared `index` or `unique`. `_bounded()` reads this to decide
        # whether a read runs inline or goes to a thread.
        self._indexed_columns: set[str] = {c.name for c in self._table.primary_key}
        for _idx in self._table.indexes:
            self._indexed_columns.update(c.name for c in _idx.columns)
        self._indexed_columns.update(
            c.name for c in self._table.columns if c.unique or c.primary_key
        )

    # ──────────────── engines + write lock ────────────────

    @property
    def _stmt_by_id(self):
        """`SELECT * FROM <table> WHERE id = :pk_value`, built once per instance.

        The bind name is deliberately not a column name, so it cannot collide
        with the schema whatever it declares.
        """
        stmt = self.__dict__.get("_stmt_by_id_cache")
        if stmt is None:
            stmt = select(self._table).where(self._table.c.id == bindparam("pk_value"))
            self.__dict__["_stmt_by_id_cache"] = stmt
        return stmt

    def _bounded(self, filters: Dict[str, Any], limit: Optional[int]) -> bool:
        """Is this read guaranteed to touch a bounded slice of the table?

        The whole point of executing inline is that a point read is 4
        microseconds — a thread hop costs ten times that. But the same code path
        also serves `find()` over a million rows, and THAT must not sit on the
        event loop. So the split is by what the query can touch, not by which
        method was called: a LIMIT, or a filter on the primary key / an indexed
        / a unique column, means SQLite walks an index and stops. Anything else
        may scan, and a scan goes to a thread.

        Deliberately conservative: an unindexed filter is treated as a scan even
        when it happens to match two rows, because the schema is what we can
        check and the data is not.
        """
        if limit is not None:
            return True
        if not filters:
            return False
        return any(
            key.split("__", 1)[0] in self._indexed_columns for key in filters
        )

    async def _read(self, fn, *, bounded: bool):
        """Execute a read: inline when bounded, in a worker thread otherwise."""
        _, reader = await self._ensure_engines()

        def _run():
            with reader.connect() as conn:
                return fn(conn)

        if not bounded:
            return await asyncio.to_thread(_run)

        # Bounded reads share ONE connection per (db file, event loop) instead of
        # checking one out per query: the pool checkout, the Connection object and
        # the checkout/checkin events cost more than the query itself. Measured on
        # a point read, 0.076 ms per checkout against 0.044 shared — 1.7x.
        #
        # Safe for exactly the reason the sync layer exists: a bounded read has no
        # `await` inside, so the loop cannot interleave a second query onto this
        # connection, and the connection never leaves the loop's thread (the
        # thread path above keeps taking its own from the pool).
        conn = self._loop_read_conn(reader)
        try:
            result = fn(conn)
        except Exception as e:
            # Any DB-level failure retires the shared connection rather than
            # leaving a broken one cached for every later read.
            if isinstance(e, (sa_exc.SQLAlchemyError, sqlite3.Error)):
                self._drop_loop_read_conn()
            raise
        # END THE READ TRANSACTION — not optional, and not obvious. SQLAlchemy
        # opens one on first execute and holds it until told otherwise, so a
        # connection living for the life of the process would keep serving the
        # snapshot it first saw (a row written a second ago invisible forever) and
        # SQLite could never checkpoint the WAL past that open read.
        # `isolation_level="AUTOCOMMIT"` does NOT prevent it here: the `begin`
        # listener this module installs — so migrations get transactional DDL —
        # fires anyway and emits a real BEGIN. Measured: with the rollback the
        # shared connection is still 1.7x a checkout, so the snapshot was the
        # expensive part, not the rollback.
        conn.rollback()
        return result

    def _loop_read_conn(self, reader: Engine) -> Connection:
        """The per-loop shared read connection, opened on first use.

        Opened with AUTOCOMMIT as a belt-and-braces measure; what actually keeps
        it honest is the `rollback()` after every read in `_read` — see the note
        there, because the option alone does NOT stop this module's own `begin`
        listener from opening a transaction.
        """
        state = _db_loop_state()
        conns = state.setdefault("read_conns", {})
        key = str(self.db_path)
        conn = conns.get(key)
        if conn is None or conn.closed:
            conn = reader.connect().execution_options(isolation_level="AUTOCOMMIT")
            conns[key] = conn
        return conn

    def _drop_loop_read_conn(self) -> None:
        """Close and forget the shared read connection (error recovery, close())."""
        try:
            state = _db_loop_state()
        except RuntimeError:
            return
        conn = state.get("read_conns", {}).pop(str(self.db_path), None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    async def _get_write_lock(self) -> asyncio.Lock:
        """Return the per-loop, per-db-path write lock.

        Still an `asyncio.Lock` and still per-loop, because that is the one
        thing a synchronous engine does NOT make unnecessary. A single
        statement no longer needs it — it runs inline with no await inside, so
        the loop cannot interleave two of them — but `transaction()` yields to
        caller code that awaits, and that is where writers must queue.
        """
        state = _db_loop_state()
        locks = state["locks"]
        key = str(self.db_path)
        lock = locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            locks[key] = lock
        return lock

    async def _ensure_engines(self) -> tuple[Engine, Engine]:
        """Get/create the (writer, reader) pair; run schema init if needed."""
        if self._engines is None:
            self._engines = _get_engines(self.db_path)
        if not self._schema_ready:
            await self._ensure_schema_initialized()
        return self._engines

    async def _ensure_schema_initialized(
        self, conn: Optional["ConnLike"] = None,
    ) -> None:
        """Run schema init + pending migrations once per (instance, process).

        When called without `conn` (the normal path), it opens its own
        `writer.begin()` and does the work inside that transaction, then
        caches the result in `_initialized_dbs` so subsequent calls
        are no-ops.

        When called WITH `conn` (the `save(conn=...)` path), it runs on
        the caller's connection so the schema-init DDL is atomic with
        the caller's transaction — and crucially is rolled back together
        if the caller's transaction fails. For this reason we do NOT
        cache the init result: a future call after a rollback must redo
        the schema init. The op is idempotent (CREATE TABLE IF NOT EXISTS,
        ALTER ADD COLUMN only on missing columns, migrations gated by
        PRAGMA user_version), so re-running is safe and cheap.
        """
        if self._db_key in _initialized_dbs:
            self._schema_ready = True
            return
        state = _db_loop_state()
        async with state["schema_init_lock"]:
            if self._db_key in _initialized_dbs:
                self._schema_ready = True
                return
            if conn is not None:
                # Inline path: schema-init runs on the caller's conn so
                # it's atomic with the rest of their transaction. We
                # deliberately don't mark as initialized — if the caller
                # rolls back, the CREATE TABLE rolls back too and the
                # next call must redo it.
                await self._init_or_migrate_schema(_raw_conn(conn))
                return
            writer, _ = _get_engines(self.db_path)
            with writer.begin() as own_conn:
                await self._init_or_migrate_schema(own_conn)
            _initialized_dbs.add(self._db_key)
            self._schema_ready = True

    async def _init_or_migrate_schema(self, conn: Connection) -> None:
        """Create-on-first-run or add missing columns on schema drift.

        Order is load-bearing: ALTER TABLE ADD COLUMN must run BEFORE the
        idempotent `metadata.create_all`, because indexes referencing new
        columns would otherwise fail to create.

        After the retrofit, we diff the live schema against the dataclass
        and surface mismatches (type drift, NOT NULL violated by retrofit,
        orphan columns) as warnings — or as `RuntimeError` when
        `strict_schema=True`.
        """
        result = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name=:n"),
            {"n": self.table_name},
        )
        table_exists = result.fetchone() is not None

        # Whether the DATABASE is new, which is a different question from
        # whether this TABLE is new — and the distinction decides whether the
        # migration pointer may be leap-frogged below. Read BEFORE create_all,
        # because create_all is what stops it from being empty.
        db_is_empty = not table_exists and conn.execute(text(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' LIMIT 1"
        )).fetchone() is None

        if not table_exists:
            # Fresh table — create_all handles columns and indexes in one shot.
            #
            # It runs over the WHOLE shared MetaData (every table declared on
            # this file), not just this one, and that is deliberate: SQLAlchemy
            # emits CREATE TABLE in foreign-key dependency order, so a child
            # declared before its parent still lands after it. Restricting it to
            # `tables=[self._table]` would create a child whose parent table does
            # not exist yet, and with `foreign_keys=ON` the failure would surface
            # much later, as "no such table" on the first INSERT.
            #
            # `checkfirst` (the default) makes the sibling creations no-ops once
            # they exist, so this stays idempotent.
            try:
                self._metadata.create_all(conn)
            except sa_exc.NoReferencedTableError as e:
                raise sa_exc.NoReferencedTableError(
                    f"{e.args[0] if e.args else e}\n\n"
                    f"esuls: {self.table_name!r} declares a foreign key to a table "
                    f"that has no AsyncDB on {self.db_path}. A FK is resolved "
                    f"against the other AsyncDBs declared for the SAME file, so "
                    f"the module defining the referenced model must be imported "
                    f"before this one is first used — and both must point at "
                    f"{self.db_path}, since SQLite cannot reference across files.",
                    e.table_name,
                ) from e
            if db_is_empty:
                # Fresh DATABASE: the dataclass-driven schema is by definition
                # "at the latest version", so leap-frog `PRAGMA user_version` to
                # the max declared migration. Otherwise the next start would try
                # to re-apply migrations that already match the live schema
                # (typically ALTER ADD COLUMN of a column the dataclass already
                # declared).
                if self.migrations_dir.is_dir():
                    migs = _discover_migrations(self.migrations_dir)
                    if migs:
                        max_version = migs[-1].version
                        conn.execute(
                            text(f"PRAGMA user_version = {int(max_version)}")
                        )
                return

            # A NEW TABLE in an EXISTING database, which must NOT leap-frog.
            # `user_version` is a property of the DATABASE while "is it new?" was
            # asked about one TABLE, so leap-frogging here would burn the pending
            # migrations of every OTHER table in the file — and burn them
            # silently: nothing fails, the migration simply never runs and
            # `user_version` is already past it, so it never runs again either.
            # Adding a table to a live schema is one of the most ordinary things
            # a project does, which is what made this quiet.
            #
            # The right answer is the same one the existing-table path gives:
            # apply what is pending. This table is already at the latest version
            # (create_all built it from the current dataclass), so a migration
            # that touches it is a no-op or an ALTER it can absorb; the ones that
            # matter are the ones aimed at its neighbours.
            await _apply_pending_migrations(conn, self.migrations_dir)
            return

        # Existing table — retrofit missing columns. SQLite's ADD COLUMN
        # cannot enforce NOT NULL retroactively, so all retrofits are
        # NULL-able regardless of `required=True` on the dataclass.
        info = conn.execute(
            text(f'PRAGMA table_info("{self.table_name}")')
        )
        existing = {row[1] for row in info.fetchall()}
        retrofit_names: set[str] = set()
        for col in self._table.columns:
            if col.name in existing:
                continue
            type_ddl = col.type.compile(dialect=conn.dialect)
            conn.execute(
                text(
                    f'ALTER TABLE "{self.table_name}" '
                    f'ADD COLUMN "{col.name}" {type_ddl}'
                )
            )
            retrofit_names.add(col.name)
        # Pick up any index declared since the table was created. NOT
        # `metadata.create_all`: with checkfirst it skips an existing table
        # WHOLESALE, indexes included, so a `metadata={"unique": True}` added to
        # a live table was silently never enforced.
        await self._ensure_indexes(conn)

        # Drift detection: surface mismatches the retrofit cannot fix.
        await self._check_schema_drift(conn, retrofit_names)

        # Apply file-based migrations from `<db_path.parent>/migrations`.
        # Runs in the same writer transaction → atomic across schema init
        # + drift check + every migration. Any failure rolls back the
        # whole thing and leaves `PRAGMA user_version` at its prior value.
        await _apply_pending_migrations(conn, self.migrations_dir)

    async def _ensure_indexes(self, conn: Connection) -> list[str]:
        """Create every declared index the live table is missing.

        Needed because SQLAlchemy's `create_all(checkfirst=True)` emits NOTHING
        for a table it did not create — indexes included. So adding
        `metadata={"index": True}` (or `"unique": True`) to a dataclass whose
        table already exists used to add the column and silently never enforce
        the constraint. `CREATE [UNIQUE] INDEX IF NOT EXISTS` is the SQLite-
        native way to add one after the fact, and is idempotent.

        A UNIQUE index is pre-checked for existing duplicates: SQLite refuses to
        build it if any exist, and that error would abort schema init — i.e.
        boot — for every caller of a database that has been accumulating
        duplicates precisely BECAUSE the constraint was missing. Deduplicating
        is a decision for a human with the domain in mind, not for a startup
        path, so such an index is skipped and its column reported instead.

        Returns the columns left unenforced (empty when everything is in place).
        """
        rows = conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = :t"
        ), {"t": self.table_name})
        present = {r[0] for r in rows.fetchall()}

        skipped: list[str] = []
        for index in self._table.indexes:
            if index.name in present:
                continue
            cols = [c.name for c in index.columns]
            quoted = ", ".join(f'"{c}"' for c in cols)

            if index.unique:
                # NULLs are NOT duplicates: SQL treats them as distinct, so a
                # UNIQUE index permits any number of rows with a NULL in the
                # indexed column(s) — which is the whole reason a nullable
                # column can be unique at all. Excluding them matters in the
                # common case: an optional-but-unique column (a handle, a slug,
                # an external id) is mostly NULL early on, and counting those
                # NULLs as a conflict would refuse an index SQLite builds
                # happily. For a composite index the row is exempt if ANY of its
                # columns is NULL, matching SQLite's own rule.
                not_null = " AND ".join(f'"{c}" IS NOT NULL' for c in cols)
                dupes = conn.execute(text(
                    f'SELECT COUNT(*) FROM (SELECT 1 FROM "{self.table_name}" '
                    f"WHERE {not_null} GROUP BY {quoted} HAVING COUNT(*) > 1)"
                ))
                if dupes.scalar():
                    skipped.extend(cols)
                    logger.error(
                        f"{self.table_name}: duplicate values in "
                        f"({', '.join(cols)}) — UNIQUE index {index.name!r} NOT "
                        "created. Deduplicate, then restart to enforce it."
                    )
                    continue

            unique_sql = "UNIQUE " if index.unique else ""
            conn.execute(text(
                f'CREATE {unique_sql}INDEX IF NOT EXISTS "{index.name}" '
                f'ON "{self.table_name}" ({quoted})'
            ))
            logger.info(
                f"{self.table_name}: created missing {unique_sql.lower()}index "
                f"{index.name!r} on ({', '.join(cols)})"
            )
        return skipped

    async def _check_schema_drift(
        self,
        conn: Connection,
        retrofit_names: set[str],
    ) -> None:
        """Diff live schema against dataclass and surface issues.

        Three flavours of drift, all reported via the same channel:
          1. Type mismatch — column exists in both, but the declared SQL
             type is not equivalent under SQLite affinity rules to what
             the dataclass would produce (`_types_equivalent` handles the
             common BOOLEAN↔INTEGER, DATETIME↔TEXT, VARCHAR↔TEXT pairs).
          2. Retrofit NOT NULL violation — a `required=True` column had
             to be added via ALTER, which cannot enforce NOT NULL on
             pre-existing rows.
          3. Orphan column — exists in the DB but no longer declared on
             the dataclass.

        Each is `logger.warning` by default; with `strict_schema=True`,
        the first issue is raised as `RuntimeError` to stop the world.
        """
        issues: list[str] = []

        info = conn.execute(
            text(f'PRAGMA table_info("{self.table_name}")')
        )
        live_by_name = {row[1]: row for row in info.fetchall()}
        declared_by_name = {c.name: c for c in self._table.columns}
        # Dataclass field metadata is the source of truth for `required` —
        # SQLA Column doesn't carry that flag forward, so we look it up here.
        dataclass_field_meta = {
            f.name: f.metadata for f in fields(self.schema_class)
        }

        for name, col in declared_by_name.items():
            declared_ddl = col.type.compile(dialect=conn.dialect)
            live_row = live_by_name.get(name)
            if live_row is None:
                continue  # should not happen post-retrofit
            live_type = (live_row[2] or "")
            if live_type and declared_ddl and not _types_equivalent(live_type, declared_ddl):
                issues.append(
                    f"column {self.table_name}.{name}: type drift "
                    f"(DB declared {live_type!r}, schema would declare {declared_ddl!r}). "
                    f"SQLite is dynamically typed so reads may still work, "
                    f"but writes can store values the schema can't decode."
                )
            field_meta = dataclass_field_meta.get(name, {})
            if name in retrofit_names and field_meta.get("required", False):
                issues.append(
                    f"column {self.table_name}.{name}: retrofitted via "
                    f"ALTER TABLE ADD COLUMN but declared required=True. "
                    f"SQLite cannot enforce NOT NULL on retrofitted columns; "
                    f"pre-existing rows will have NULL in this column."
                )

        for name in live_by_name:
            if name not in declared_by_name:
                issues.append(
                    f"column {self.table_name}.{name}: present in DB but not "
                    f"on {self.schema_class.__name__}. Either drop it via a "
                    f"migration or add the field to the dataclass."
                )

        for msg in issues:
            logger.warning(f"schema drift: {msg}")
        if issues and self.strict_schema:
            raise RuntimeError(
                f"strict_schema=True: {len(issues)} schema drift issue(s) "
                f"on {self.table_name}. See warnings above."
            )

    # ──────────────── lifecycle ────────────────

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def close(self) -> None:
        """Run PRAGMA optimize + checkpoint, then dispose both engines.

        Idempotent — a second call is a no-op. The `initialized` flag is
        cleared too so the next operation re-runs schema init against fresh
        engines.

        Flips each engine's `_EngineDisposalMarker.disposed` flag BEFORE
        calling `dispose()` so the GC finalizer, when it later fires,
        recognises this as a clean teardown and does NOT emit the
        "engine GC'd without close" `ResourceWarning`.
        """
        self._drop_loop_read_conn()
        key = str(self.db_path)
        with _db_state_guard:
            pair = _engines_by_path.pop(key, None)
            _initialized_dbs.discard(self._db_key)
        self._engines = None
        self._schema_ready = False
        if pair is None:
            return
        writer, reader = pair
        # Mark disposed BEFORE the actual dispose so a finalizer racing
        # against close() (rare but possible if GC kicks in mid-method)
        # sees the flag set and skips the warning.
        for engine in (writer, reader):
            marker = _engine_markers.get(engine)
            if marker is not None:
                marker.disposed = True
        try:
            with writer.connect() as conn:
                conn.execute(text("PRAGMA optimize"))
        except Exception as e:
            logger.debug(f"close(): PRAGMA optimize skipped: {e}")
        for engine in (reader, writer):
            try:
                engine.dispose()
            except Exception:
                pass
        # The checkpoint runs LAST, on a connection of its own, because
        # `wal_checkpoint(TRUNCATE)` needs every other connection to the file
        # closed — and both pools were still holding theirs. It has been failing
        # this whole time, logging "database table is locked" at DEBUG (i.e.
        # nowhere) and leaving the -wal file to carry across restarts instead of
        # folding back into the database. Cheap to do right, invisible when wrong.
        try:
            raw = sqlite3.connect(str(self.db_path), timeout=5.0)
            try:
                raw.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                raw.close()
        except Exception as e:
            logger.debug(f"close(): wal_checkpoint skipped: {e}")

    async def checkpoint(self, mode: str = "PASSIVE") -> tuple[int, int, int]:
        """Run `PRAGMA wal_checkpoint(<mode>)`. Returns (busy, log, checkpointed)."""
        if mode not in ("PASSIVE", "FULL", "RESTART", "TRUNCATE"):
            raise ValueError(f"Invalid checkpoint mode: {mode!r}")
        writer, _ = await self._ensure_engines()
        with writer.connect() as conn:
            result = conn.execute(text(f"PRAGMA wal_checkpoint({mode})"))
            row = result.fetchone()
            return tuple(row) if row else (0, 0, 0)

    async def list_migrations(self) -> List[Dict[str, Any]]:
        """List all migrations next to this db with their applied status.

        Returns a list of dicts: ``{version, description, source, applied}``.
        Because the AsyncDB constructor (well, first-use of any method)
        applies any pending migrations automatically, in normal usage
        every entry comes back ``applied=True``. The method is most
        useful right after writing a new migration to verify it was
        picked up — or in a health-check endpoint that wants to log
        which schema version is live.

        For pre-apply discovery (e.g. CI dry-runs), call the
        module-level `discover_migrations(path)` instead.
        """
        if not self.migrations_dir.is_dir():
            return []
        migs = _discover_migrations(self.migrations_dir)
        if not migs:
            return []
        writer, _ = await self._ensure_engines()
        with writer.connect() as conn:
            current = (conn.execute(text("PRAGMA user_version"))).scalar() or 0
        return [
            {
                "version": m.version,
                "description": m.description,
                "source": m.source.name,
                "applied": m.version <= current,
            }
            for m in migs
        ]

    @contextlib.asynccontextmanager
    async def transaction(self, read_only: bool = False):
        """Yield a connection whose `execute()` you still `await`.

        - Writer (`read_only=False`): wrapped in `engine.begin()` — autocommit
          on clean exit, rollback on exception.
        - Reader (`read_only=True`): `engine.connect()` — no BEGIN, no COMMIT,
          no rollback. SQLite's deferred isolation makes BEGIN unnecessary
          for SELECT-only sequences.

        Use `await conn.execute(text("...raw SQL..."))` for raw queries; SQLA
        2.0 requires textual SQL to be wrapped in `text()`.

        Concurrency contract for writes: the write transaction HOLDS the
        per-loop write lock for the whole block. It has to, and the version
        that did not was actively dangerous. The reasoning that excused it —
        "the writer engine is a StaticPool of size 1, so writers queue at the
        pool" — is wrong about StaticPool: it does not queue, it hands the SAME
        connection to every caller. Two requests whose transactions overlapped
        therefore shared one connection and one transaction, so the second one's
        `BEGIN` raised "cannot start a transaction within a transaction", and
        when the timing let both in, a rollback in one erased writes the other
        had already been told were committed. Measured on a signup endpoint:
        36 callers got a success response, 30 rows existed. Holding the lock
        makes concurrent transactions queue instead — which is what the callers
        that had noticed were already doing by hand with their own mutex.

        The lock is per PROCESS (per loop, per db path). Across processes the
        arbiter is SQLite's own file lock plus `busy_timeout`, exactly as before.

        Multi-table atomic writes: to mix several AsyncDB instances inside
        one transaction (e.g. saving a Company row and an AccrediaCache
        row in a single commit), use the `conn=` kwarg on the write
        methods:

            async with DB_COMPANY.transaction() as conn:
                await DB_COMPANY.save(company,        conn=conn)
                await DB_CACHE.save(cache_row,        conn=conn)
            # both writes commit together; either both apply or neither.

        All AsyncDBs in the call chain must point at the SAME .db file —
        SQLite cannot make writes across separate files atomic. When
        `conn=` is passed, the inner method skips its own write-lock
        acquisition and `writer.begin()` (the caller already holds those),
        and `max_retries` is ignored (a transient failure mid-transaction
        is the caller's to retry, not ours).
        """
        writer, reader = await self._ensure_engines()
        try:
            if read_only:
                # Readers never conflict under WAL: no lock, no BEGIN.
                with reader.connect() as conn:
                    yield _AwaitableConn(conn)
            else:
                lock = await self._get_write_lock()
                async with lock:
                    with writer.begin() as conn:
                        yield _AwaitableConn(conn)
        except Exception as e:
            if _is_stale_connection(e):
                self._drop_loop_read_conn()
                _drop_engines(self.db_path)
                self._engines = None
                self._schema_ready = False
            raise

    # ──────────────── validation helpers ────────────────

    def col(self, name: str) -> ColumnElement:
        """The SQL column, for the write the database must compute ITSELF.

            taken = DB_COURSE.col("taken")
            claimed = await DB_COURSE.update_many(
                {"taken": taken + 1},                    # SET taken = taken + 1
                id=course_id, taken__lt=DB_COURSE.col("capacity"),
            )

        Read-modify-write in Python — read the row, add one, write it back — is
        the single most common way to lose data on this layer, and concurrency
        does not have to mean processes: 200 coroutines each reading the same
        counter before any of them writes lands 200 increments as one. Measured
        on a real endpoint: 200 concurrent claims on a 30-seat course produced 91
        rows through the read-then-write path, and exactly 30 through this one.

        Naming a column also lets a filter compare two columns
        (`taken__lt=self.col("capacity")`), which is what turns "check the
        capacity, then insert" into a single statement with no window in it.

        Raises `ValueError` for a name the schema doesn't declare.
        """
        return self._table.c[self._validate_column(name)]

    def _validate_column(self, name: str) -> str:
        if name not in self._valid_columns:
            raise ValueError(
                f"Unknown column {name!r} on {self.schema_class.__name__}. "
                f"Valid columns: {sorted(self._valid_columns)}"
            )
        return name

    def _resolve_conflict_target(
        self,
        on_conflict: Optional[Union[str, Tuple[str, ...], List[str]]],
    ) -> tuple[str, ...]:
        """Validate and normalise `on_conflict` to a tuple of column names.

        Three accepted shapes:
          1. `None` → defaults to the primary key (`"id"`).
          2. A single column name (str) → must be declared
             `primary_key=True` or `unique=True`.
          3. A tuple/list of column names → either every column is
             individually unique/PK (rare but legal), OR the set of
             column names matches a declared `__unique_together__` entry
             (order-insensitive).
        """
        if on_conflict is None:
            return ("id",)
        if isinstance(on_conflict, str):
            targets = (on_conflict,)
        else:
            targets = tuple(on_conflict)

        # Composite target: prefer matching a declared __unique_together__
        # (order-insensitive via frozenset). Otherwise fall back to "every
        # column is individually unique" — rare but harmless.
        if len(targets) > 1:
            if frozenset(targets) in self._composite_conflict_targets:
                return targets
            unknown = [c for c in targets if c not in self._conflict_targets]
            if unknown:
                composites = sorted(
                    tuple(sorted(s)) for s in self._composite_conflict_targets
                )
                raise ValueError(
                    f"on_conflict={on_conflict!r}: composite target not "
                    f"declared on {self.schema_class.__name__}. Add "
                    f"`__unique_together__ = [{tuple(targets)!r}]` to the schema, "
                    f"or use columns that are individually primary_key=True / "
                    f"unique=True. Available composite targets: {composites}. "
                    f"Individually-eligible columns: {sorted(self._conflict_targets)}."
                )
            return targets

        # Single target: must be individually unique/PK.
        unknown = [c for c in targets if c not in self._conflict_targets]
        if unknown:
            raise ValueError(
                f"on_conflict={on_conflict!r}: column(s) {unknown} are not "
                f"declared primary_key=True or unique=True on "
                f"{self.schema_class.__name__}. "
                f"Available conflict targets: {sorted(self._conflict_targets)}"
            )
        return targets

    def _item_to_row(self, item: SchemaType) -> dict:
        """Build the SQLA values dict for an upsert.

        - `id` semantics depend on the PK column type:
            * Integer PK + id=None → pop the key so SQLite assigns rowid
              (autoincrement). Falsy non-None values (e.g. 0) are preserved.
            * String PK + id=None → auto-generate a UUID. Falsy non-None
              values (e.g. "") are preserved.
        - `created_at` / `updated_at` are only injected if the schema
          declares them (composition: IdModel has no timestamps,
          TimestampedModel does).
        - All other fields pass through unchanged — TypeDecorators handle
          the actual roundtripping.
        """
        data = asdict(item)
        if data.get("id") is None and "id" in data:
            id_col = self._table.c.get("id")
            if id_col is not None and isinstance(id_col.type, Integer):
                # Let SQLite assign rowid.
                data.pop("id")
            else:
                data["id"] = str(uuid.uuid4())
        now = utcnow()
        if "created_at" in self._valid_columns and data.get("created_at") is None:
            data["created_at"] = now
        if "updated_at" in self._valid_columns:
            data["updated_at"] = now
        return data

    @staticmethod
    def _apply_op(
        lhs: ColumnElement, op: str, value: Any, *, _key_for_error: str = "",
    ) -> ColumnElement:
        """Compile a single `lhs <op> value` SQLA expression.

        Shared between WHERE (`_build_filter_clause`) and HAVING
        (`aggregate(having=...)`) so both surfaces support exactly the
        same operator set. The `_key_for_error` argument is used purely
        for error messages on `__between` mis-shapes.
        """
        if op == "gt":
            return lhs > value
        if op == "lt":
            return lhs < value
        if op == "gte":
            return lhs >= value
        if op == "lte":
            return lhs <= value
        if op == "neq":
            return lhs != value
        if op == "like":
            return lhs.like(value)
        if op == "in":
            return lhs.in_(value)
        if op == "not_in":
            return lhs.not_in(value)
        if op == "is_null":
            # is_null=True → IS NULL; is_null=False → IS NOT NULL.
            return lhs.is_(None) if value else lhs.is_not(None)
        if op == "not_null":
            return lhs.is_not(None) if value else lhs.is_(None)
        if op == "between":
            try:
                lo, hi = value
            except (TypeError, ValueError) as e:
                key_msg = f"{_key_for_error}: " if _key_for_error else ""
                raise ValueError(
                    f"{key_msg}__between requires a 2-element sequence "
                    f"(lo, hi); got {value!r}"
                ) from e
            return lhs.between(lo, hi)
        # eq (default)
        return lhs == value

    def _build_filter_clause(self, filters: Dict[str, Any]) -> list:
        """Translate `key__op=value` kwargs into SQLA boolean expressions.

        Supported suffixes:
          eq (default), neq, gt, gte, lt, lte, like, in, not_in,
          is_null=bool, not_null=bool, between=(lo, hi).

        `__in` and `__not_in` accept any iterable; an empty iterable
        compiles to a SQLA expression that always evaluates false
        (`col IN ()` semantics).
        """
        clauses = []
        for key, value in filters.items():
            parts = key.split("__", 1)
            col_name = self._validate_column(parts[0])
            col = self._table.c[col_name]
            op = parts[1] if (len(parts) > 1 and parts[1] in self.OPERATOR_MAP) else "eq"
            clauses.append(self._apply_op(col, op, value, _key_for_error=key))
        return clauses

    def _build_upsert(self, conflict_target: tuple[str, ...]):
        """Build the `INSERT ... ON CONFLICT(...) DO UPDATE SET ...` statement.

        `created_at` is excluded from the UPDATE branch so the original
        row's creation timestamp survives upserts.
        """
        stmt = sqlite_insert(self._table)
        update_cols = {
            c.name: stmt.excluded[c.name]
            for c in self._table.columns
            if c.name != "created_at"
        }
        return stmt.on_conflict_do_update(
            index_elements=list(conflict_target),
            set_=update_cols,
        )

    async def _execute_with_retry(self, action, *, max_retries: int = 3,
                                  in_thread: bool = False):
        """Run `action` (a SYNCHRONOUS no-arg callable) with BUSY/stale retries.

        Exponential backoff on BUSY/LOCKED; stale-connection errors drop the
        engine cache so the next attempt builds a fresh pair. The action runs
        inline — the sleep between attempts is the only await, which is exactly
        what it should be: the loop is released while WAITING, not while working.
        """
        last_error = None
        for attempt in range(max_retries):
            try:
                # A batch is the one write whose size the caller chooses, so it
                # is the one that can hold the loop for as long as the list is
                # long. It runs in a thread — safely, because the caller holds
                # the write lock, so no other writer can reach the connection.
                result = await asyncio.to_thread(action) if in_thread else action()
                # The action is normally synchronous now, but an async one still
                # works: callers outside this module (and the tests that pin the
                # retry policy) predate the change.
                if inspect.isawaitable(result):
                    result = await result
                return result
            except Exception as e:
                last_error = e
                if attempt < max_retries - 1:
                    if _is_sqlite_busy(e):
                        # Exponential backoff with jitter (×0.5..×1.5).
                        # The randomisation desynchronises concurrent
                        # retries — otherwise N tasks hitting BUSY at the
                        # same instant would all retry together and pile
                        # right back on the lock.
                        wait_time = (
                            0.2 * (2 ** attempt) * random.uniform(0.5, 1.5)
                        )
                        logger.debug(
                            f"DB busy/locked, retry {attempt + 1}/{max_retries} "
                            f"in {wait_time}s"
                        )
                        await asyncio.sleep(wait_time)
                        continue
                    if _is_stale_connection(e):
                        self._drop_loop_read_conn()
                        _drop_engines(self.db_path)
                        self._engines = None
                        self._schema_ready = False
                        logger.debug(
                            f"Stale connection, reconnecting "
                            f"(retry {attempt + 1}/{max_retries})"
                        )
                        continue
                raise
        raise last_error  # pragma: no cover — loop always raises on last attempt

    # ──────────────── public API ────────────────

    async def save(
        self,
        item: SchemaType,
        skip_errors: bool = True,
        *,
        on_conflict: Optional[Union[str, Tuple[str, ...], List[str]]] = None,
        max_retries: int = 3,
        conn: Optional["ConnLike"] = None,
    ) -> bool:
        """Atomically insert or upsert one item.

        With `on_conflict=None` (default), the upsert is by primary key
        (`id`), so retrying a save is idempotent. Pass a natural-key column
        (e.g. `on_conflict="external_id"`) — or a composite tuple declared
        in `__unique_together__` — to make the upsert race-free against
        concurrent writers identifying rows by that key.

        `max_retries` controls how many times the BUSY/stale-connection
        retry loop attempts the write before giving up.

        `skip_errors=True` (default) only swallows **DB-level** errors
        (SQLAlchemy / sqlite3). Programming errors — `TypeError` (wrong
        item type), `ValueError` (bad `on_conflict`), `AttributeError`,
        etc. — always propagate so bugs in the caller don't hide as
        "save failed". Set `skip_errors=False` to surface DB errors too.

        `conn` lets the caller inject their own open connection
        (typically from `async with another_db.transaction(): ...`) so
        multiple AsyncDBs sharing a single SQLite file can write inside
        one atomic transaction. When `conn` is passed:
          - the write lock is NOT acquired (caller's `transaction()` already
            holds the only writer connection via StaticPool);
          - `writer.begin()` is NOT entered (caller owns the transaction);
          - `max_retries` is IGNORED (a transient failure mid-transaction
            requires the caller to retry the whole transaction, not just
            this statement).
        Both AsyncDBs must point at the same .db file for the cross-table
        atomicity to actually hold — esuls does not promise atomicity
        across separate SQLite files (impossible).
        """
        # Type check first — wrong type is a programming error path. When
        # skip_errors=True we degrade to a warning + False; otherwise raise.
        if not isinstance(item, self.schema_class):
            if skip_errors:
                logger.warning(
                    f"save(): expected {self.schema_class.__name__}, "
                    f"got {type(item).__name__}; skipping"
                )
                return False
            raise TypeError(
                f"Expected {self.schema_class.__name__}, "
                f"got {type(item).__name__}"
            )

        # The next three calls can raise (`_resolve_conflict_target` →
        # ValueError on bad on_conflict; `_item_to_row` / `_build_upsert`
        # could fail on a malformed schema). These are setup-time
        # programming errors — let them propagate rather than be silently
        # swallowed by skip_errors.
        conflict_target = self._resolve_conflict_target(on_conflict)
        row = self._item_to_row(item)
        stmt = self._build_upsert(conflict_target).values(**row)

        if conn is not None:
            # Inline path: caller owns the transaction. Schema-init must
            # also run on their conn so the DDL is atomic with the rest.
            try:
                await self._ensure_schema_initialized(conn)
                _raw_conn(conn).execute(stmt)
                return True
            except (sa_exc.SQLAlchemyError, sqlite3.Error) as e:
                if skip_errors:
                    logger.warning(
                        f"save(): DB error in caller's transaction (skipped): {e}"
                    )
                    return False
                raise

        writer, _ = await self._ensure_engines()
        write_lock = await self._get_write_lock()

        def _do() -> bool:
            with writer.begin() as own_conn:
                own_conn.execute(stmt)
            return True

        try:
            async with write_lock:
                return await self._execute_with_retry(_do, max_retries=max_retries)
        except (sa_exc.SQLAlchemyError, sqlite3.Error) as e:
            if skip_errors:
                logger.warning(f"save(): DB error (skipped): {e}")
                return False
            raise

    async def save_batch(
        self,
        items: List[SchemaType],
        *,
        on_conflict: Optional[Union[str, Tuple[str, ...], List[str]]] = None,
        max_retries: int = 3,
        conn: Optional["ConnLike"] = None,
        **deprecated_kwargs,
    ) -> int:
        """Save many items atomically via `executemany` — fail-fast.

        All rows go in a single round-trip to SQLite. Any per-row error
        (type mismatch, constraint violation, …) rolls back the whole
        batch. This is the path you want for scrapers / pipelines that
        do many inserts and need to notice failures immediately.

        For best-effort per-item saves (log + skip on error), use
        `save_each(...)` instead.

        `conn` (optional) lets the caller run this inside their own open
        transaction — see `save()` for the semantics and caveats.

        ``skip_errors`` kwarg (deprecated): the old `save_batch` accepted
        ``skip_errors`` to choose between two paths. The fast path is now
        the default and the per-item loop has moved to `save_each`.
        Passing ``skip_errors=True`` emits a `DeprecationWarning` and
        forwards to `save_each`; ``skip_errors=False`` emits a warning
        and runs the new default path.
        """
        if "skip_errors" in deprecated_kwargs:
            skip = bool(deprecated_kwargs.pop("skip_errors"))
            warnings.warn(
                "save_batch(skip_errors=...) is deprecated. "
                "Use save_each(...) for the per-item loop with skip-and-log, "
                "or call save_batch(...) (now always fail-fast executemany).",
                DeprecationWarning,
                stacklevel=2,
            )
            if skip:
                return await self.save_each(
                    items, on_conflict=on_conflict, max_retries=max_retries,
                    conn=conn,
                )
            # skip_errors=False is the new default — fall through.
        if deprecated_kwargs:
            raise TypeError(
                f"save_batch() got unexpected keyword arguments: "
                f"{list(deprecated_kwargs)}"
            )

        if not items:
            return 0
        conflict_target = self._resolve_conflict_target(on_conflict)
        # Validate all item types up-front so we never partially commit
        # via the conn path before the type check fires.
        for item in items:
            if not isinstance(item, self.schema_class):
                raise TypeError(
                    f"Expected {self.schema_class.__name__}, "
                    f"got {type(item).__name__}"
                )

        if conn is not None:
            await self._ensure_schema_initialized(conn)
            rows = [self._item_to_row(it) for it in items]
            stmt = self._build_upsert(conflict_target)
            _raw_conn(conn).execute(stmt, rows)
            return len(rows)

        writer, _ = await self._ensure_engines()
        write_lock = await self._get_write_lock()

        def _do() -> int:
            with writer.begin() as own_conn:
                rows = [self._item_to_row(it) for it in items]
                stmt = self._build_upsert(conflict_target)
                own_conn.execute(stmt, rows)
                return len(rows)

        async with write_lock:
            return await self._execute_with_retry(
                _do, max_retries=max_retries, in_thread=True
            )

    async def save_each(
        self,
        items: List[SchemaType],
        *,
        on_conflict: Optional[Union[str, Tuple[str, ...], List[str]]] = None,
        max_retries: int = 3,
        conn: Optional["ConnLike"] = None,
    ) -> int:
        """Save items one-by-one inside a single transaction, skipping rotten ones.

        Each row gets its own `execute()` (N round-trips to the aiosqlite
        worker). Items that fail are logged via `logger.warning` and the
        loop continues. Returns the count of items successfully written.

        Use this when the batch contains untrusted/mixed data and you'd
        rather persist the good rows than reject the whole batch. For
        clean batches prefer `save_batch()` — it's faster and surfaces
        bugs immediately.

        `conn` (optional) lets the caller run this inside their own open
        transaction — see `save()` for the semantics and caveats.
        """
        if not items:
            return 0
        conflict_target = self._resolve_conflict_target(on_conflict)

        def _loop(c: Connection) -> int:
            saved = 0
            for item in items:
                try:
                    if not isinstance(item, self.schema_class):
                        logger.warning(
                            f"save_each skipped non-{self.schema_class.__name__} "
                            f"item ({type(item).__name__})"
                        )
                        continue
                    row = self._item_to_row(item)
                    stmt = self._build_upsert(conflict_target).values(**row)
                    c.execute(stmt)
                    saved += 1
                except Exception as e:
                    logger.warning(f"save_each skipped item: {e}")
                    continue
            return saved

        if conn is not None:
            await self._ensure_schema_initialized(conn)
            return _loop(_raw_conn(conn))

        writer, _ = await self._ensure_engines()
        write_lock = await self._get_write_lock()

        def _do() -> int:
            with writer.begin() as own_conn:
                return _loop(own_conn)

        async with write_lock:
            return await self._execute_with_retry(
                _do, max_retries=max_retries, in_thread=True
            )

    async def get_by_id(
        self, record_id: Union[str, int]
    ) -> Optional[SchemaType]:
        """Fetch by primary key. Accepts `str` (UUID/IdModel/TimestampedModel)
        or `int` (IntIdModel) since the PK type depends on the schema."""
        # The statement is built ONCE (see `_stmt_by_id`) and the id travels as a
        # bound parameter. Rebuilding it per call meant SQLAlchemy coercing the
        # comparison and re-deriving the statement's cache key every time —
        # together the largest single entry in a profile of this method.
        stmt = self._stmt_by_id
        row = await self._read(
            lambda c: c.execute(stmt, {"pk_value": record_id}).mappings().first(),
            bounded=True,
        )
        if row is None:
            return None
        return self.schema_class(**dict(row))

    async def find(
        self,
        order_by: Optional[Union[str, List[str]]] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        **filters,
    ) -> List[SchemaType]:
        """SELECT rows matching `**filters`, with optional ordering / paging.

        ``order_by`` accepts either ``"column"`` (ASC) or ``"-column"``
        (DESC), or a list mixing both for multi-column sorts. No other
        syntax (e.g. ``NULLS LAST``, expressions) is supported here —
        drop to ``transaction(read_only=True)`` + ``text(...)`` for
        anything more elaborate. Unknown column names raise ``ValueError``.

        ``**filters`` uses the same ``key__op=value`` mini-DSL as
        ``_build_filter_clause``: ``eq`` (default), ``neq``, ``gt``,
        ``gte``, ``lt``, ``lte``, ``like``, ``in``, ``not_in``,
        ``is_null=bool``, ``not_null=bool``, ``between=(lo, hi)``.
        """
        stmt = select(self._table)
        for clause in self._build_filter_clause(filters):
            stmt = stmt.where(clause)

        if order_by:
            order_fields = [order_by] if isinstance(order_by, str) else order_by
            for entry in order_fields:
                if entry.startswith("-"):
                    col = self._table.c[self._validate_column(entry[1:])]
                    stmt = stmt.order_by(col.desc())
                else:
                    col = self._table.c[self._validate_column(entry)]
                    stmt = stmt.order_by(col.asc())

        if limit is not None:
            stmt = stmt.limit(limit)
        elif offset is not None:
            # SQLite refuses OFFSET without LIMIT. -1 means "no limit".
            stmt = stmt.limit(-1)
        if offset is not None:
            stmt = stmt.offset(offset)

        rows = await self._read(
            lambda c: c.execute(stmt).mappings().all(),
            bounded=self._bounded(filters, limit),
        )
        return [self.schema_class(**dict(row)) for row in rows]

    async def find_columns(
        self,
        columns: List[str],
        *,
        order_by: Optional[Union[str, List[str]]] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        **filters,
    ) -> List[Dict[str, Any]]:
        """Project only `columns`; return raw dicts instead of SchemaType.

        Useful for tables with large BLOB/JSON columns when you only need
        a subset — avoids deserialising fields you'll throw away. Returns
        `List[Dict]` (not `List[SchemaType]`) because partial rows can't
        be constructed as the dataclass without fake defaults for the
        missing fields, which would mislead the caller.

        Same filter / order_by / limit / offset semantics as `find()`.
        """
        if not columns:
            raise ValueError("find_columns() requires at least one column")
        validated = [self._validate_column(c) for c in columns]
        stmt = select(*(self._table.c[c] for c in validated))
        for clause in self._build_filter_clause(filters):
            stmt = stmt.where(clause)

        if order_by:
            order_fields = [order_by] if isinstance(order_by, str) else order_by
            for entry in order_fields:
                if entry.startswith("-"):
                    col = self._table.c[self._validate_column(entry[1:])]
                    stmt = stmt.order_by(col.desc())
                else:
                    col = self._table.c[self._validate_column(entry)]
                    stmt = stmt.order_by(col.asc())

        if limit is not None:
            stmt = stmt.limit(limit)
        elif offset is not None:
            stmt = stmt.limit(-1)
        if offset is not None:
            stmt = stmt.offset(offset)

        rows = await self._read(
            lambda c: c.execute(stmt).mappings().all(),
            bounded=self._bounded(filters, limit),
        )
        return [dict(row) for row in rows]

    async def count(self, **filters) -> int:
        stmt = select(func.count()).select_from(self._table)
        for clause in self._build_filter_clause(filters):
            stmt = stmt.where(clause)
        return await self._read(
            lambda c: c.execute(stmt).scalar_one(),
            bounded=self._bounded(filters, None),
        )

    async def fetch_all(self) -> List[SchemaType]:
        return await self.find()

    async def find_one(
        self,
        order_by: Optional[Union[str, List[str]]] = None,
        **filters,
    ) -> Optional[SchemaType]:
        """Return the first row matching `**filters`, or `None`.

        Thin shortcut over `find(limit=1, ...)` — replaces the very common
        `rows = await db.find(...); row = rows[0] if rows else None`
        idiom. `order_by` is supported so "most recent matching row" is
        a one-call operation (`find_one(order_by="-created_at", user=u)`).
        """
        rows = await self.find(order_by=order_by, limit=1, **filters)
        return rows[0] if rows else None

    async def exists(self, **filters) -> bool:
        """True if at least one row matches `**filters`.

        Compiles to `SELECT 1 FROM <table> WHERE ... LIMIT 1` — SQLite
        stops at the first matching row, so this is O(1) when the
        filtered columns are indexed and O(N) scan only when forced to.
        Faster than `count(**filters) > 0`, which has to count every
        match across the full table.
        """
        stmt = select(literal(1)).select_from(self._table)
        clauses = self._build_filter_clause(filters)
        if clauses:
            stmt = stmt.where(and_(*clauses))
        stmt = stmt.limit(1)
        return await self._read(lambda c: c.execute(stmt).first() is not None, bounded=True)

    async def aggregate(
        self,
        *,
        group_by: Optional[Union[str, List[str]]] = None,
        count: bool = False,
        count_distinct: Optional[Union[str, List[str]]] = None,
        sum: Optional[Union[str, List[str]]] = None,
        avg: Optional[Union[str, List[str]]] = None,
        min: Optional[Union[str, List[str]]] = None,
        max: Optional[Union[str, List[str]]] = None,
        having: Optional[Dict[str, Any]] = None,
        order_by: Optional[Union[str, List[str]]] = None,
        limit: Optional[int] = None,
        **filters,
    ) -> List[Dict[str, Any]]:
        """Compute aggregations grouped by zero or more columns.

        Returns a list of dicts (column → value). Keys come from
        `group_by` columns plus aliases for each requested aggregate:
        `count` / `count_distinct_<col>` / `sum_<col>` / `avg_<col>` /
        `min_<col>` / `max_<col>`.

        `filters` apply via WHERE (pre-aggregation). `having` applies via
        HAVING (post-aggregation), using the same `__op` suffix syntax —
        but on alias names (`count`, `sum_<col>`, ...), not raw columns.

        Example:
            await db.aggregate(group_by="city",
                               count=True, count_distinct="user_id",
                               sum="amount",
                               amount__gt=0,
                               having={"count_distinct_user_id__gte": 5})
        """
        if not (count or count_distinct or sum or avg or min or max):
            raise ValueError(
                "aggregate() requires at least one of: "
                "count, count_distinct, sum, avg, min, max"
            )

        def _as_list(v):
            if v is None:
                return []
            return [v] if isinstance(v, str) else list(v)

        group_cols = _as_list(group_by)
        cdist_cols = _as_list(count_distinct)
        sum_cols = _as_list(sum)
        avg_cols = _as_list(avg)
        min_cols = _as_list(min)
        max_cols = _as_list(max)

        # Validate every column name against the schema whitelist.
        for c in group_cols + cdist_cols + sum_cols + avg_cols + min_cols + max_cols:
            self._validate_column(c)

        # Build projection: group columns first, then aggregates, each
        # aliased so the result mapping has stable, predictable keys.
        projection: list = []
        alias_to_expr: Dict[str, Any] = {}
        for c in group_cols:
            col = self._table.c[c]
            projection.append(col.label(c))
            alias_to_expr[c] = col
        if count:
            expr = func.count().label("count")
            projection.append(expr)
            alias_to_expr["count"] = func.count()
        for c in cdist_cols:
            alias = f"count_distinct_{c}"
            # NOTE: SQLite has no `COUNT(DISTINCT a, b)` form — each entry
            # in `count_distinct` produces its own COUNT(DISTINCT col).
            expr = func.count(func.distinct(self._table.c[c])).label(alias)
            projection.append(expr)
            alias_to_expr[alias] = func.count(func.distinct(self._table.c[c]))
        for c in sum_cols:
            alias = f"sum_{c}"
            expr = func.sum(self._table.c[c]).label(alias)
            projection.append(expr)
            alias_to_expr[alias] = func.sum(self._table.c[c])
        for c in avg_cols:
            alias = f"avg_{c}"
            expr = func.avg(self._table.c[c]).label(alias)
            projection.append(expr)
            alias_to_expr[alias] = func.avg(self._table.c[c])
        for c in min_cols:
            alias = f"min_{c}"
            expr = func.min(self._table.c[c]).label(alias)
            projection.append(expr)
            alias_to_expr[alias] = func.min(self._table.c[c])
        for c in max_cols:
            alias = f"max_{c}"
            expr = func.max(self._table.c[c]).label(alias)
            projection.append(expr)
            alias_to_expr[alias] = func.max(self._table.c[c])

        stmt = select(*projection).select_from(self._table)
        for clause in self._build_filter_clause(filters):
            stmt = stmt.where(clause)
        for c in group_cols:
            stmt = stmt.group_by(self._table.c[c])

        # HAVING: same `__op` mini-DSL, but the LHS is an aggregate alias
        # rather than a column. Resolve via `alias_to_expr`, then dispatch
        # through `_apply_op` so HAVING gets the full operator set
        # (in/not_in/like/is_null/between/...), not just numeric comparators.
        if having:
            for key, val in having.items():
                parts = key.split("__", 1)
                alias = parts[0]
                if alias not in alias_to_expr:
                    raise ValueError(
                        f"having key {key!r} references unknown alias "
                        f"{alias!r}; available: {sorted(alias_to_expr)}"
                    )
                lhs = alias_to_expr[alias]
                op = parts[1] if len(parts) > 1 and parts[1] in self.OPERATOR_MAP else "eq"
                stmt = stmt.having(self._apply_op(lhs, op, val, _key_for_error=key))

        # order_by accepts aliases (e.g. "count", "sum_amount") so users
        # can sort by their aggregates. Falls back to a raw column lookup
        # if the entry isn't an alias.
        if order_by:
            order_entries = [order_by] if isinstance(order_by, str) else list(order_by)
            for entry in order_entries:
                desc = entry.startswith("-")
                name = entry[1:] if desc else entry
                if name in alias_to_expr:
                    expr = alias_to_expr[name]
                else:
                    expr = self._table.c[self._validate_column(name)]
                stmt = stmt.order_by(expr.desc() if desc else expr.asc())

        if limit is not None:
            stmt = stmt.limit(limit)

        rows = await self._read(lambda c: c.execute(stmt).mappings().all(), bounded=False)
        return [dict(row) for row in rows]

    async def stream(
        self,
        order_by: Optional[Union[str, List[str]]] = None,
        batch_size: int = 1000,
        **filters,
    ) -> AsyncIterator[SchemaType]:
        """Yield rows one at a time without loading everything into memory.

        Fetches `batch_size` rows at a time from a server-side cursor, so a
        table too large to materialise via `find()` never lands in memory at
        once. Each FETCH runs in a worker thread: this is the unbounded read by
        definition, so it is exactly the case that must not sit on the loop.

        Generator cleanup (early `break`, `aclose`, exception) closes the
        underlying connection via the `with reader.connect()` context manager.
        """
        stmt = select(self._table)
        for clause in self._build_filter_clause(filters):
            stmt = stmt.where(clause)
        if order_by:
            order_fields = [order_by] if isinstance(order_by, str) else order_by
            for entry in order_fields:
                if entry.startswith("-"):
                    col = self._table.c[self._validate_column(entry[1:])]
                    stmt = stmt.order_by(col.desc())
                else:
                    col = self._table.c[self._validate_column(entry)]
                    stmt = stmt.order_by(col.asc())

        _, reader = await self._ensure_engines()
        with reader.connect() as conn:
            result = conn.execution_options(yield_per=batch_size).execute(stmt)
            while True:
                rows = await asyncio.to_thread(result.mappings().fetchmany, batch_size)
                if not rows:
                    break
                for row in rows:
                    yield self.schema_class(**dict(row))

    async def delete(
        self,
        record_id: Union[str, int],
        *,
        max_retries: int = 3,
        conn: Optional["ConnLike"] = None,
    ) -> bool:
        """Delete by id. `conn=` runs inline (see `save()` for semantics)."""
        stmt = delete(self._table).where(self._table.c.id == record_id)

        if conn is not None:
            await self._ensure_schema_initialized(conn)
            result = _raw_conn(conn).execute(stmt)
            return result.rowcount > 0

        writer, _ = await self._ensure_engines()
        write_lock = await self._get_write_lock()

        def _do() -> bool:
            with writer.begin() as own_conn:
                result = own_conn.execute(stmt)
                return result.rowcount > 0

        async with write_lock:
            return await self._execute_with_retry(_do, max_retries=max_retries)

    async def delete_many(
        self,
        *,
        max_retries: int = 3,
        conn: Optional["ConnLike"] = None,
        **filters,
    ) -> int:
        """Delete every row matching `**filters`. `conn=` runs inline."""
        if not filters:
            raise ValueError(
                "delete_many() requires at least one filter to prevent "
                "accidental full table delete"
            )
        stmt = delete(self._table)
        for clause in self._build_filter_clause(filters):
            stmt = stmt.where(clause)

        if conn is not None:
            await self._ensure_schema_initialized(conn)
            result = _raw_conn(conn).execute(stmt)
            return result.rowcount

        writer, _ = await self._ensure_engines()
        write_lock = await self._get_write_lock()

        def _do() -> int:
            with writer.begin() as own_conn:
                result = own_conn.execute(stmt)
                return result.rowcount

        async with write_lock:
            return await self._execute_with_retry(_do, max_retries=max_retries)

    async def update_fields(
        self,
        record_id: Union[str, int],
        *,
        max_retries: int = 3,
        conn: Optional["ConnLike"] = None,
        **values,
    ) -> bool:
        """Update specific fields by id, without round-tripping the full row.

        `conn=` runs inline (see `save()` for semantics).
        """
        if not values:
            return False
        for k in values:
            self._validate_column(k)
        # Only auto-set updated_at if the schema actually declares it
        # (an IdModel-only schema has no timestamp columns).
        if "updated_at" in self._valid_columns:
            values["updated_at"] = utcnow()
        stmt = (
            update(self._table)
            .where(self._table.c.id == record_id)
            .values(**values)
        )

        if conn is not None:
            await self._ensure_schema_initialized(conn)
            result = _raw_conn(conn).execute(stmt)
            return result.rowcount > 0

        writer, _ = await self._ensure_engines()
        write_lock = await self._get_write_lock()

        def _do() -> bool:
            with writer.begin() as own_conn:
                result = own_conn.execute(stmt)
                return result.rowcount > 0

        async with write_lock:
            return await self._execute_with_retry(_do, max_retries=max_retries)

    async def update_many(
        self,
        values: Dict[str, Any],
        *,
        max_retries: int = 3,
        conn: Optional["ConnLike"] = None,
        **filters,
    ) -> int:
        """Update every row matching `**filters` with the `values` dict.

        Returns the count of rows affected. `filters` uses the standard
        `key__op=value` mini-DSL; `values` is a flat `column → new value`
        dict. `updated_at` is auto-set if the schema declares it AND the
        caller hasn't passed an explicit value for it.

        For safety, an empty `filters` raises ValueError — pass at least
        one filter, or use `transaction(read_only=False)` + raw SQL if
        you really mean "update every row". This mirrors the
        `delete_many()` guard.

        `conn=` runs inline (see `save()` for semantics).
        """
        if not values:
            return 0
        if not filters:
            raise ValueError(
                "update_many() requires at least one filter to prevent "
                "an accidental update-every-row. Use transaction() + text() "
                "if you really mean it."
            )
        for k in values:
            self._validate_column(k)
        if (
            "updated_at" in self._valid_columns
            and "updated_at" not in values
        ):
            values = {**values, "updated_at": utcnow()}

        stmt = update(self._table)
        for clause in self._build_filter_clause(filters):
            stmt = stmt.where(clause)
        stmt = stmt.values(**values)

        if conn is not None:
            await self._ensure_schema_initialized(conn)
            result = _raw_conn(conn).execute(stmt)
            return result.rowcount

        writer, _ = await self._ensure_engines()
        write_lock = await self._get_write_lock()

        def _do() -> int:
            with writer.begin() as own_conn:
                result = own_conn.execute(stmt)
                return result.rowcount

        async with write_lock:
            return await self._execute_with_retry(_do, max_retries=max_retries)
