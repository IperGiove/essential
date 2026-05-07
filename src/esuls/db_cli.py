import asyncio
import aiosqlite
import ast
import base64
import json
import re
import sqlite3
import threading
import weakref
import dataclasses
from datetime import datetime, date
from pathlib import Path
from typing import Any, Dict, List, Optional, TypeVar, Generic, Type, get_type_hints, Union, Tuple
from dataclasses import dataclass, asdict, fields, is_dataclass, field
from functools import lru_cache
import uuid
import contextlib
import enum
from loguru import logger
from decimal import Decimal


# Sentinel prefix for base64-encoded bytes inside JSON-serialised dicts.
# BaseModel.to_dict() prepends this to bytes values; from_dict() strips
# it back. Chosen for clarity in logs/inspection — text fields holding
# a literal "b64:" string are unaffected because from_dict only decodes
# fields whose declared type is bytes.
_B64_PREFIX = "b64:"


def _is_bytes_field(ftype) -> bool:
    """True for `bytes` and `Optional[bytes]` (or any Union including bytes).

    Used by BaseModel.from_dict to know which fields need base64
    decoding back from a transport-encoded ('b64:...') string.
    """
    if ftype is bytes:
        return True
    origin = getattr(ftype, "__origin__", None)
    if origin is Union:
        return bytes in getattr(ftype, "__args__", ())
    return False


_VALID_IDENTIFIER = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*$')

def _validate_identifier(name: str) -> str:
    if not _VALID_IDENTIFIER.match(name):
        raise ValueError(f"Invalid SQL identifier: {name!r}")
    return name


# Retryable SQLite error codes: BUSY = whole-db lock contention,
# LOCKED = table-level lock contention. Both are transient and the
# canonical retry-and-backoff response is the same.
_RETRYABLE_SQLITE_CODES = frozenset({sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED})


def _is_sqlite_busy(exc: BaseException) -> bool:
    """True if `exc` is a transient SQLite contention error worth retrying.

    Prefers the structured `sqlite_errorcode` attribute (available on
    sqlite3 errors since Python 3.11) over string matching. The string
    fallback covers errors wrapped by intermediate layers (mocks in
    tests, future aiosqlite shapes) that may strip the errorcode.
    """
    code = getattr(exc, "sqlite_errorcode", None)
    if code in _RETRYABLE_SQLITE_CODES:
        return True
    msg = str(exc).lower()
    return "database is locked" in msg or "database is busy" in msg


def _is_stale_connection(exc: BaseException) -> bool:
    """True if `exc` indicates the cached aiosqlite connection is dead.

    These errors come from the aiosqlite layer (not sqlite3 itself) when
    a Connection is reused after being closed or after its event loop
    died. They carry no `sqlite_errorcode`, so string matching is the
    only signal available.
    """
    msg = str(exc).lower()
    return "closed" in msg or "no active connection" in msg


# ─── per-loop registry ────────────────────────────────────────────────────
#
# asyncio.Lock and aiosqlite.Connection both bind to the running event loop
# on first use; sharing them across loops raises 'bound to a different event
# loop' or causes futures to be scheduled on the wrong loop. This affects
# any code that calls asyncio.run() more than once in a process (CLI scripts
# that retry, pytest-asyncio with function-scoped loops, embedding via
# anyio, multi-tenant servers).
#
# We keep a per-loop dict of asyncio primitives in a WeakKeyDictionary keyed
# by the loop object. When a loop is garbage-collected, its entry is dropped
# automatically — no manual cleanup, no lingering references.

_db_state_by_loop: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict]" = weakref.WeakKeyDictionary()
_db_state_guard = threading.Lock()


def _db_loop_state() -> dict:
    """Return per-loop state: write locks, schema-init lock, initialized set."""
    loop = asyncio.get_running_loop()
    with _db_state_guard:
        state = _db_state_by_loop.get(loop)
        if state is None:
            state = {
                "locks": {},                          # db_path -> asyncio.Lock
                "schema_init_lock": asyncio.Lock(),   # serialises schema init
                "initialized": set(),                 # set[_db_key]
            }
            _db_state_by_loop[loop] = state
        return state


# Tracks whether we've already warned about an unrecognised aiosqlite
# Connection shape, so the log line doesn't fire on every connect.
_aiosqlite_daemon_shape_warned = False


def _mark_aiosqlite_daemon(connection_object) -> bool:
    """Mark aiosqlite's worker thread as daemon so it can't block process exit.

    `aiosqlite.Connection` runs a non-daemon worker thread in a loop waiting
    for SQL operations. If the user forgets to call `close()` (or doesn't
    use the async context manager), that thread keeps the interpreter alive
    indefinitely on exit. Marking it daemon makes it die with the process.

    The attribute we touch is private to aiosqlite (it has changed shape
    once already: pre-0.22 the Connection itself was a Thread; from 0.22
    it wraps the worker in `_thread`). To stay resilient against future
    refactors we:
      - try `_thread.daemon` first (current shape),
      - fall back to `Connection`-extends-`Thread` (legacy shape),
      - on any AttributeError/TypeError log at debug and return False so
        the caller knows the safety net wasn't applied.

    Returns True iff the flag was applied. The connection still works
    when False — it's only the "forgotten close()" path that suffers.
    """
    global _aiosqlite_daemon_shape_warned
    try:
        if hasattr(connection_object, '_thread'):
            connection_object._thread.daemon = True
            return True
        if isinstance(connection_object, threading.Thread):
            connection_object.daemon = True
            return True
    except (AttributeError, TypeError) as e:
        logger.debug(f"Could not mark aiosqlite worker as daemon: {e}")
        return False

    if not _aiosqlite_daemon_shape_warned:
        _aiosqlite_daemon_shape_warned = True
        logger.warning(
            "aiosqlite worker shape changed: neither `_thread` nor a "
            "Thread subclass. Process may hang on unclosed connections; "
            "always call AsyncDB.close() or use `async with AsyncDB(...)`."
        )
    return False

T = TypeVar('T')
SchemaType = TypeVar('SchemaType', bound='BaseModel')

@dataclass
class BaseModel:
    id: str = field(default_factory=lambda: str(uuid.uuid4()), metadata={"primary_key": True})
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict:
        result = {}
        for f in fields(self):
            value = getattr(self, f.name)
            result[f.name] = self._serialize_value(value)
        return result

    @classmethod
    def from_dict(cls: Type[SchemaType], data: dict) -> SchemaType:
        """Reconstruct an instance from a JSON-style dict.

        Symmetric counterpart to `to_dict()`: decodes any "b64:..."
        strings back into bytes for fields whose declared type is
        `bytes` (or `Optional[bytes]`). Other fields are passed
        through unchanged — the dataclass constructor handles
        primitives, and downstream `_deserialize_value` (in AsyncDB)
        handles datetime/enum/etc. on the SQLite read path.

        Use this whenever you reconstruct a model from JSON received
        over the wire (e.g. cache_save / cache_find_many endpoints);
        plain `cls(**data)` would leave bytes fields holding the
        encoded string and break downstream consumers.
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
            # Bytes can't go through json.dumps. Base64-encode with the
            # _B64_PREFIX sentinel so the symmetric `from_dict` knows
            # to decode this back into a bytes object on the receiving
            # side. The SQLite read/write path (AsyncDB._serialize_value
            # / _deserialize_value) already handles bytes natively as
            # BLOB and is unaffected by this transport-level encoding.
            return _B64_PREFIX + base64.b64encode(value).decode("ascii")
        if isinstance(value, enum.Enum):
            return value.value
        if isinstance(value, uuid.UUID):
            return str(value)
        if isinstance(value, Decimal):
            return float(value)
        if isinstance(value, (list, tuple, set)):
            return [self._serialize_value(v) for v in value]
        if isinstance(value, dict):
            return {k: self._serialize_value(v) for k, v in value.items()}
        if hasattr(value, "to_dict"):          # nested BaseModel
            return value.to_dict()
        if is_dataclass(value):                # nested dataclass generico
            return asdict(value)
        return value


class AsyncDB(Generic[SchemaType]):
    """High-performance async SQLite with dataclass schema and reliable connection handling.

    Locks and the cached connection are scoped to the running event loop:
    multiple `asyncio.run()` invocations against the same AsyncDB instance
    (or against different instances pointing at the same db file) will
    re-acquire fresh asyncio primitives in the new loop instead of
    crashing with `bound to a different event loop`.
    """

    OPERATOR_MAP = {
        'gt': '>', 'lt': '<', 'gte': '>=', 'lte': '<=',
        'neq': '!=', 'like': 'LIKE', 'in': 'IN', 'eq': '='
    }

    def __init__(self, db_path: Union[str, Path], table_name: str, schema_class: Type[SchemaType]):
        """Initialize AsyncDB with a path and schema dataclass."""
        if not is_dataclass(schema_class):
            raise TypeError(f"Schema must be a dataclass, got {schema_class}")

        self.db_path = Path(db_path).resolve()
        self.schema_class = schema_class
        self.table_name = _validate_identifier(table_name)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # Validate all field names upfront
        for f in fields(schema_class):
            _validate_identifier(f.name)

        # Whitelist of column names declared on the schema. Used by
        # _validate_column() to gate every column name interpolated into SQL
        # at runtime (where-clause keys, order_by, update_fields kwargs), so
        # untrusted inputs cannot smuggle arbitrary identifiers — even if
        # they happen to match the safe-identifier regex.
        self._valid_columns: frozenset[str] = frozenset(
            f.name for f in fields(schema_class)
        )

        # Collect columns that can be used as upsert conflict targets:
        # any field flagged primary_key=True or unique=True. Used by save()
        # / save_batch() to validate the `on_conflict` argument up-front and
        # to surface typos as a ValueError instead of an opaque SQL error.
        self._conflict_targets: set[str] = {
            f.name for f in fields(schema_class)
            if f.metadata.get('primary_key') or f.metadata.get('unique')
        }

        # Make schema initialization unique per instance
        self._db_key = f"{str(self.db_path)}:{self.table_name}:{self.schema_class.__name__}"

        # Write lock is acquired per running loop via _get_write_lock();
        # see _db_loop_state() at module level for the registry semantics.

        self._type_hints = get_type_hints(schema_class)

        # Persistent connection (lazy init); _connection_loop tracks the
        # event loop in which it was opened. A cross-loop reuse drops the
        # stale connection so a fresh one is opened in the current loop.
        self._connection: Optional[aiosqlite.Connection] = None
        self._connection_loop: Optional[asyncio.AbstractEventLoop] = None

    async def _get_write_lock(self) -> asyncio.Lock:
        """Return the write lock for this db file in the current loop.

        All AsyncDB instances pointing at the same db_path within the same
        loop share one Lock, so writes against a single SQLite file are
        serialised in-process before SQLite has to. Different loops get
        different Lock objects (otherwise they would clash on first
        contention with `bound to a different event loop`).
        """
        state = _db_loop_state()
        locks = state["locks"]
        key = str(self.db_path)
        lock = locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            locks[key] = lock
        return lock
    
    async def _ensure_connection(self, max_retries: int = 5) -> aiosqlite.Connection:
        """Return the persistent connection, creating it on first call with retry logic.

        If a cached connection is present but bound to a different (likely
        defunct) event loop, it is silently dropped and a fresh connection
        is opened in the current loop. We don't try to close the stale
        connection — closing requires the original loop, which is gone.
        """
        loop = asyncio.get_running_loop()
        if self._connection is not None:
            if self._connection_loop is loop:
                return self._connection
            # Cross-loop reuse: drop without close (the original loop is gone).
            self._connection = None
            self._connection_loop = None

        state = _db_loop_state()
        schema_init_lock: asyncio.Lock = state["schema_init_lock"]
        initialized: set = state["initialized"]

        last_error = None
        for attempt in range(max_retries):
            try:
                db = aiosqlite.connect(self.db_path, timeout=30.0)
                # Mark daemon BEFORE await (which calls thread.start()).
                _mark_aiosqlite_daemon(db)
                db = await db
                # Fast WAL mode with minimal sync
                await db.execute("PRAGMA journal_mode=WAL")
                await db.execute("PRAGMA synchronous=NORMAL")
                await db.execute("PRAGMA cache_size=10000")
                await db.execute("PRAGMA busy_timeout=30000")  # 30s busy timeout

                # Initialize schema if needed (with lock to prevent race condition)
                if self._db_key not in initialized:
                    async with schema_init_lock:
                        # Double-check after acquiring lock
                        if self._db_key not in initialized:
                            await self._init_schema(db)
                            initialized.add(self._db_key)

                self._connection = db
                self._connection_loop = loop
                return db
            except Exception as e:
                last_error = e
                if attempt < max_retries - 1:
                    # Exponential backoff: 0.1s, 0.2s, 0.4s, 0.8s, 1.6s
                    wait_time = 0.1 * (2 ** attempt)
                    await asyncio.sleep(wait_time)
                    continue
                raise
        raise last_error

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def close(self) -> None:
        """Explicitly close the persistent connection."""
        if self._connection is not None:
            try:
                await self._connection.close()
            except Exception:
                pass
            self._connection = None
            self._connection_loop = None

    async def _init_schema(self, db: aiosqlite.Connection) -> None:
        """Generate schema from dataclass structure with support for field additions."""
        logger.debug(f"Initializing schema for {self.schema_class.__name__} in table {self.table_name}")
        
        field_defs = []
        indexes = []
        
        # First check if table exists
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (self.table_name,)
        )
        table_exists = await cursor.fetchone() is not None
        
        existing_columns = set()
        if table_exists:
            # Get existing columns if table exists
            cursor = await db.execute(f"PRAGMA table_info({self.table_name})")
            columns = await cursor.fetchall()
            existing_columns = {col[1] for col in columns}  # col[1] is the column name
        
        # Process all fields in the dataclass - ONLY THIS SCHEMA CLASS
        schema_fields = fields(self.schema_class)
        logger.debug(f"Processing {len(schema_fields)} fields for {self.schema_class.__name__}")
        
        for f in schema_fields:
            field_name = f.name
            field_type = self._type_hints.get(field_name)
            logger.debug(f"  Field: {field_name} -> {field_type}")

            # Unwrap Optional[X] → X
            if hasattr(field_type, '__origin__') and field_type.__origin__ is Union:
                args = [a for a in field_type.__args__ if a is not type(None)]
                if len(args) == 1:
                    field_type = args[0]

            # Map Python types to SQLite types.
            #
            # Order matters: we check by identity for the leaf types that
            # don't participate in subclass relationships we care about
            # (bytes, datetime, float), then by `issubclass` so that
            # IntEnum/IntFlag inherit `int` → INTEGER, StrEnum inherits
            # `str` → TEXT, and other Enum subclasses fall back to TEXT.
            # `isinstance(field_type, type)` guards against typing aliases
            # (e.g. `List[str]`) that would crash `issubclass`.
            if field_type is bytes:
                sql_type = "BLOB"
            elif field_type is datetime:
                sql_type = "TIMESTAMP"
            elif field_type is float:
                sql_type = "REAL"
            elif isinstance(field_type, type) and issubclass(field_type, int):
                # int, bool, IntEnum, IntFlag (bool is a subclass of int).
                sql_type = "INTEGER"
            elif isinstance(field_type, type) and issubclass(field_type, str):
                # str, StrEnum.
                sql_type = "TEXT"
            elif isinstance(field_type, type) and issubclass(field_type, enum.Enum):
                # Other Enum subtypes — serialized via `.value`, stored as TEXT.
                sql_type = "TEXT"
            elif getattr(field_type, '__origin__', None) is list:
                # List[X] / list[X] — JSON-encoded into a TEXT column.
                sql_type = "TEXT"
            else:
                # Dict, nested dataclass, unrecognised typing aliases, etc.
                sql_type = "TEXT"  # JSON-encoded
                
            # Handle special field metadata
            constraints = []
            if f.metadata.get('primary_key'):
                constraints.append("PRIMARY KEY")
            if f.metadata.get('unique'):
                constraints.append("UNIQUE")
            if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING and f.metadata.get('required', True):
                constraints.append("NOT NULL")
                
            field_def = f"{field_name} {sql_type} {' '.join(constraints)}"
            
            if not table_exists:
                # Add field definition for new table creation
                field_defs.append(field_def)
            elif field_name not in existing_columns:
                # Alter table to add the new column without NOT NULL constraint
                alter_sql = f"ALTER TABLE {self.table_name} ADD COLUMN {field_name} {sql_type}"
                logger.debug(f"  Adding new column: {alter_sql}")
                await db.execute(alter_sql)
                await db.commit()
                
            # Handle indexes
            if f.metadata.get('index'):
                index_name = f"idx_{self.table_name}_{field_name}"
                index_sql = f"CREATE INDEX IF NOT EXISTS {index_name} ON {self.table_name}({field_name})"
                indexes.append(index_sql)
        
        # Create table if it doesn't exist
        if not table_exists:
            # Check for table constraints
            table_constraints = getattr(self.schema_class, '__table_constraints__', [])

            constraints_sql = ""
            if table_constraints:
                constraints_sql = ", " + ", ".join(table_constraints)

            create_sql = f"""
                CREATE TABLE IF NOT EXISTS {self.table_name} (
                    {', '.join(field_defs)}{constraints_sql}
                )
            """
            logger.debug(f"Creating table: {create_sql}")
            await db.execute(create_sql)
        
        # Create indexes
        for idx_stmt in indexes:
            await db.execute(idx_stmt)
            
        await db.commit()
        logger.debug(f"Schema initialization complete for {self.schema_class.__name__}")
    
    @contextlib.asynccontextmanager
    async def transaction(self, read_only: bool = False):
        """Run operations in a transaction.

        Write paths (default, `read_only=False`) commit on clean exit and
        roll back on exception. Read paths (`read_only=True`) skip both
        commit and rollback — for SELECT-only sequences SQLite doesn't
        auto-start a transaction (with the default deferred isolation),
        so commit/rollback are pointless round-trips to the aiosqlite
        worker thread.

        On a stale-connection error ("connection is closed", "no active
        connection") the cached connection is cleared so the next call to
        `_ensure_connection()` reopens it. Auto-retry is the caller's
        responsibility — write paths wrap this in a retry loop; read
        paths surface the error to the caller.
        """
        db = await self._ensure_connection()
        try:
            yield db
            if not read_only:
                await db.commit()
        except Exception as e:
            if not read_only:
                try:
                    await db.rollback()
                except Exception:
                    pass
            if _is_stale_connection(e):
                self._connection = None
            raise
    
    # @lru_cache(maxsize=128)
    def _serialize_value(self, value: Any) -> Any:
        """Fast value serialization with type-based optimization."""
        if value is None or isinstance(value, (int, float, bool, str)):
            return value
        if isinstance(value, bytes):
            return value 
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, enum.Enum):
            return value.value
        if isinstance(value, (list, dict, tuple)):
            return json.dumps(value, default=lambda v: v.value if isinstance(v, enum.Enum) else str(v))
        return str(value)
    
    def _deserialize_value(self, field_name: str, value: Any) -> Any:
        """Deserialize values based on field type."""
        if value is None:
            return value

        field_type = self._type_hints.get(field_name)

        # Handle bytes fields - keep as bytes
        if field_type == bytes:
            if isinstance(value, bytes):
                return value
            # If somehow stored as string, convert back
            if isinstance(value, str):
                try:
                    return ast.literal_eval(value)
                except (ValueError, SyntaxError):
                    return value.encode('utf-8')

        # Handle bool fields - SQLite stores as INTEGER, need to convert back
        if field_type is bool:
            return bool(value)

        # Handle string fields - ensure phone numbers are strings
        if field_type is str or (hasattr(field_type, '__origin__') and field_type.__origin__ is Union and str in getattr(field_type, '__args__', ())):
            return str(value)

        if field_type is datetime and isinstance(value, str):
            return datetime.fromisoformat(value)

        # Handle enum types
        if hasattr(field_type, '__origin__') and field_type.__origin__ is Union:
            # Handle Optional[EnumType] case
            args = getattr(field_type, '__args__', ())
            for arg in args:
                if arg is not type(None) and isinstance(arg, type) and issubclass(arg, enum.Enum):
                    try:
                        return arg(value)
                    except (ValueError, TypeError):
                        pass
        elif isinstance(field_type, type) and issubclass(field_type, enum.Enum):
            # Handle direct enum types
            try:
                return field_type(value)
            except (ValueError, TypeError):
                pass

        if isinstance(value, str):
            try:
                return json.loads(value)
            except (json.JSONDecodeError, TypeError):
                pass

        return value
    
    @lru_cache(maxsize=64)
    def _generate_save_sql(
        self,
        field_names: Tuple[str, ...],
        conflict_target: Union[str, Tuple[str, ...]] = "id",
    ) -> str:
        """Generate INSERT ... ON CONFLICT(...) DO UPDATE SQL.

        `conflict_target` selects the UNIQUE / PRIMARY KEY column(s) on
        which a duplicate triggers the UPDATE branch. Defaults to 'id'.
        Pass a tuple for composite UNIQUE constraints.

        On UPDATE we refresh every column except `created_at`, so the
        original row's creation timestamp is preserved across updates.
        """
        columns = ','.join(field_names)
        placeholders = ','.join('?' for _ in field_names)

        set_clause = ','.join(f'{col}=excluded.{col}' for col in field_names if col != 'created_at')

        if isinstance(conflict_target, str):
            conflict_cols = conflict_target
        else:
            conflict_cols = ','.join(conflict_target)

        return f"""
            INSERT INTO {self.table_name} ({columns},id)
            VALUES ({placeholders},?)
            ON CONFLICT({conflict_cols}) DO UPDATE SET {set_clause}
        """

    def _prepare_item(
        self,
        item: SchemaType,
        conflict_target: Union[str, Tuple[str, ...]] = "id",
    ) -> Tuple[str, List[Any]]:
        """Prepare an item for saving. Returns (sql, values).

        `id` and `created_at` are auto-generated only when explicitly None
        (the missing-value sentinel). Falsy-but-non-None values like `""`
        or `0` are preserved — the user passed them deliberately, and
        replacing them silently with a UUID/timestamp would mask schemas
        that use a non-string id (e.g. `id: int = 0`) or mismatch the
        caller's expectations.
        """
        data = asdict(item)
        item_id = data.pop('id', None)
        if item_id is None:
            item_id = str(uuid.uuid4())
        now = datetime.now()
        if data.get('created_at') is None:
            data['created_at'] = now
        data['updated_at'] = now
        field_names = tuple(sorted(data.keys()))
        sql = self._generate_save_sql(field_names, conflict_target=conflict_target)
        values = [self._serialize_value(data[name]) for name in field_names]
        values.append(item_id)
        return sql, values

    def _resolve_conflict_target(
        self,
        on_conflict: Optional[Union[str, Tuple[str, ...], List[str]]],
    ) -> Union[str, Tuple[str, ...]]:
        """Validate and normalize an `on_conflict` argument.

        Returns 'id' when on_conflict is None (legacy upsert-by-primary-key
        behaviour). Otherwise verifies every column is declared as
        primary_key or unique on the schema and returns it normalized.
        """
        if on_conflict is None:
            return "id"
        if isinstance(on_conflict, str):
            targets = (on_conflict,)
        else:
            targets = tuple(on_conflict)
        unknown = [c for c in targets if c not in self._conflict_targets]
        if unknown:
            raise ValueError(
                f"on_conflict={on_conflict!r}: column(s) {unknown} are not declared "
                f"primary_key=True or unique=True on {self.schema_class.__name__}. "
                f"Available conflict targets: {sorted(self._conflict_targets)}"
            )
        return targets[0] if len(targets) == 1 else targets

    async def save_batch(
        self,
        items: List[SchemaType],
        skip_errors: bool = True,
        *,
        on_conflict: Optional[Union[str, Tuple[str, ...], List[str]]] = None,
    ) -> int:
        """Save multiple items atomically in a single transaction.

        Each row is upserted via INSERT ... ON CONFLICT(...) DO UPDATE.

        Args:
            items: List of schema objects to save.
            skip_errors: If True, skip items that cause errors and continue.
            on_conflict: Column (or tuple of columns) used as the conflict
                target. Must reference columns declared primary_key=True or
                unique=True on the schema. Defaults to the primary key
                ('id'), making save_batch() idempotent on retries. Use a
                natural key (e.g. on_conflict='external_id') to make the
                upsert race-free against concurrent writers identifying
                rows by that key.

        Returns:
            Number of items successfully saved.
        """
        if not items:
            return 0

        conflict_target = self._resolve_conflict_target(on_conflict)

        saved_count = 0

        max_retries = 3
        for attempt in range(max_retries):
            try:
                saved_count = 0
                write_lock = await self._get_write_lock()
                async with write_lock:
                    async with self.transaction() as db:
                        for item in items:
                            try:
                                if not isinstance(item, self.schema_class):
                                    if not skip_errors:
                                        raise TypeError(f"Expected {self.schema_class.__name__}, got {type(item).__name__}")
                                    continue

                                sql, values = self._prepare_item(item, conflict_target=conflict_target)
                                await db.execute(sql, values)
                                saved_count += 1

                            except Exception as e:
                                if skip_errors:
                                    logger.warning(f"Save error (skipped): {e}")
                                    continue
                                raise
                break
            except Exception as e:
                if attempt < max_retries - 1:
                    if _is_sqlite_busy(e):
                        wait_time = 0.2 * (2 ** attempt)
                        logger.debug(f"DB busy/locked, retry {attempt + 1}/{max_retries} in {wait_time}s")
                        await asyncio.sleep(wait_time)
                        continue
                    if _is_stale_connection(e):
                        # transaction() already cleared self._connection;
                        # next iteration will reopen via _ensure_connection.
                        logger.debug(f"Stale connection, reconnecting (retry {attempt + 1}/{max_retries})")
                        continue
                raise

        return saved_count

    async def save(
        self,
        item: SchemaType,
        skip_errors: bool = True,
        *,
        on_conflict: Optional[Union[str, Tuple[str, ...], List[str]]] = None,
    ) -> bool:
        """Atomically insert or update a schema object.

        Implemented as INSERT ... ON CONFLICT(...) DO UPDATE, so it is
        race-free against concurrent writers as long as the conflict
        target is the same key the caller uses to identify the row.

        Args:
            item: The schema object to save.
            skip_errors: If True, silently skip errors and return False.
                If False, raise errors.
            on_conflict: Column (or tuple of columns) used as the conflict
                target. Must reference columns declared primary_key=True or
                unique=True on the schema. Defaults to the primary key
                ('id'), making save() idempotent on retries. Use a natural
                key (e.g. on_conflict='external_id') when the application
                identifies rows by a field other than id; this makes the
                upsert atomic and prevents UNIQUE-constraint races where
                two callers concurrently try to insert the same logical
                row with different ids.

        Returns:
            True if save was successful, False if error occurred and
            skip_errors=True.
        """
        try:
            if not isinstance(item, self.schema_class):
                if skip_errors:
                    return False
                raise TypeError(f"Expected {self.schema_class.__name__}, got {type(item).__name__}")

            conflict_target = self._resolve_conflict_target(on_conflict)
            sql, values = self._prepare_item(item, conflict_target=conflict_target)

            # Perform save with reliable transaction (retry on "database is
            # locked" and on stale-connection errors).
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    write_lock = await self._get_write_lock()
                    async with write_lock:
                        async with self.transaction() as db:
                            await db.execute(sql, values)
                    break
                except Exception as e:
                    if attempt < max_retries - 1:
                        if _is_sqlite_busy(e):
                            wait_time = 0.2 * (2 ** attempt)
                            logger.debug(f"DB busy/locked, retry {attempt + 1}/{max_retries} in {wait_time}s")
                            await asyncio.sleep(wait_time)
                            continue
                        if _is_stale_connection(e):
                            logger.debug(f"Stale connection, reconnecting (retry {attempt + 1}/{max_retries})")
                            continue
                    raise

            return True

        except Exception as e:
            if skip_errors:
                logger.warning(f"Save error (skipped): {e}")
                return False
            raise
    
    async def get_by_id(self, record_id: str) -> Optional[SchemaType]:
        """Fetch an item by ID with reliable connection handling."""
        async with self.transaction(read_only=True) as db:
            cursor = await db.execute(f"SELECT * FROM {self.table_name} WHERE id = ?", (record_id,))
            row = await cursor.fetchone()
            
            if not row:
                return None
                
            # Get column names and build data dictionary
            columns = [desc[0] for desc in cursor.description]
            return self.schema_class(**{
                col: self._deserialize_value(col, row[i]) 
                for i, col in enumerate(columns)
            })
    
    def _validate_column(self, name: str) -> str:
        """Ensure `name` is a column declared on the schema.

        This is the runtime gate for every column identifier interpolated
        into SQL (where-clause keys, order_by, update_fields kwargs). A
        permissive regex is not enough because an attacker-controlled but
        regex-safe name like `id; ATTACH DATABASE` could still target the
        wrong column or be appended to crafted SQL fragments downstream.
        Whitelisting against the schema makes the column reference
        provably safe.
        """
        if name not in self._valid_columns:
            raise ValueError(
                f"Unknown column {name!r} on {self.schema_class.__name__}. "
                f"Valid columns: {sorted(self._valid_columns)}"
            )
        return name

    def _build_where_clause(self, filters: Dict[str, Any]) -> Tuple[str, List[Any]]:
        """Build optimized WHERE clause for queries."""
        if not filters:
            return "", []

        conditions = []
        values = []

        for key, value in filters.items():
            # Parse field and operator
            parts = key.split('__', 1)
            field = self._validate_column(parts[0])

            if len(parts) > 1 and parts[1] in self.OPERATOR_MAP:
                op_str = self.OPERATOR_MAP[parts[1]]

                # Handle IN operator specially
                if op_str == 'IN' and isinstance(value, (list, tuple)):
                    placeholders = ','.join(['?'] * len(value))
                    conditions.append(f"{field} IN ({placeholders})")
                    values.extend(value)
                else:
                    conditions.append(f"{field} {op_str} ?")
                    values.append(value)
            else:
                # Default to equality
                conditions.append(f"{field} = ?")
                values.append(value)

        return f"WHERE {' AND '.join(conditions)}", values
    
    async def find(self, order_by=None, limit: int = None, offset: int = None, **filters) -> List[SchemaType]:
        """Query items with reliable connection handling."""
        where_clause, values = self._build_where_clause(filters)

        # Build query
        query = f"SELECT * FROM {self.table_name} {where_clause}"

        # Add ORDER BY clause if specified
        if order_by:
            order_fields = [order_by] if isinstance(order_by, str) else order_by
            order_clauses = []
            for entry in order_fields:
                if entry.startswith('-'):
                    col = self._validate_column(entry[1:])
                    order_clauses.append(f"{col} DESC")
                else:
                    col = self._validate_column(entry)
                    order_clauses.append(f"{col} ASC")
            query += f" ORDER BY {', '.join(order_clauses)}"

        # Add LIMIT/OFFSET (SQLite requires LIMIT before OFFSET)
        if limit is not None:
            query += " LIMIT ?"
            values.append(limit)
        elif offset is not None:
            query += " LIMIT -1"
        if offset is not None:
            query += " OFFSET ?"
            values.append(offset)

        # Execute query with reliable transaction (read-only — no commit needed)
        async with self.transaction(read_only=True) as db:
            cursor = await db.execute(query, values)
            rows = await cursor.fetchall()

            if not rows:
                return []

            # Process results
            columns = [desc[0] for desc in cursor.description]
            return [
                self.schema_class(**{
                    col: self._deserialize_value(col, row[i])
                    for i, col in enumerate(columns)
                })
                for row in rows
            ]
    
    async def count(self, **filters) -> int:
        """Count items matching filters with reliable connection handling."""
        where_clause, values = self._build_where_clause(filters)
        query = f"SELECT COUNT(*) FROM {self.table_name} {where_clause}"

        async with self.transaction(read_only=True) as db:
            cursor = await db.execute(query, values)
            result = await cursor.fetchone()
            return result[0] if result else 0
    
    async def fetch_all(self) -> List[SchemaType]:
        """Retrieve all items."""
        return await self.find()
    
    async def delete(self, record_id: str) -> bool:
        """Delete an item by ID with reliable transaction handling."""
        write_lock = await self._get_write_lock()
        async with write_lock:
            async with self.transaction() as db:
                cursor = await db.execute(f"DELETE FROM {self.table_name} WHERE id = ?", (record_id,))
                return cursor.rowcount > 0

    async def exists(self, **filters) -> bool:
        """Check if any record matches the filters without fetching data."""
        return await self.count(**filters) > 0

    async def delete_many(self, **filters) -> int:
        """Delete all items matching filters. Returns count of deleted rows."""
        if not filters:
            raise ValueError("delete_many() requires at least one filter to prevent accidental full table delete")
        where_clause, values = self._build_where_clause(filters)
        write_lock = await self._get_write_lock()
        async with write_lock:
            async with self.transaction() as db:
                cursor = await db.execute(f"DELETE FROM {self.table_name} {where_clause}", values)
                return cursor.rowcount

    async def update_fields(self, record_id: str, **values) -> bool:
        """Update specific fields on a record by ID without fetching the full record.

        `values` is a kwargs mapping of column → new value. The parameter is
        named `values` (not `fields`) so it doesn't shadow `dataclasses.fields`.
        """
        if not values:
            return False
        values['updated_at'] = datetime.now()
        set_clause = ', '.join(f"{self._validate_column(k)} = ?" for k in values)
        sql_values = [self._serialize_value(v) for v in values.values()]
        sql_values.append(record_id)
        write_lock = await self._get_write_lock()
        async with write_lock:
            async with self.transaction() as db:
                cursor = await db.execute(
                    f"UPDATE {self.table_name} SET {set_clause} WHERE id = ?", sql_values
                )
                return cursor.rowcount > 0