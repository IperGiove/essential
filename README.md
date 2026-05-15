# esuls

A Python utility library for async database operations, HTTP requests, and parallel execution utilities.

## Features

- **AsyncDB** - Type-safe async SQLite with dataclass schemas
- **Async HTTP client** - High-performance HTTP client with retry logic and connection pooling
- **Parallel utilities** - Async parallel execution with concurrency control
- **CloudFlare bypass** - curl-cffi integration for bypassing protections

## Installation

```bash
# With pip
pip install esuls

# With uv
uv pip install esuls
```

## Usage

### Parallel Execution

```python
import asyncio
from esuls import run_parallel

async def fetch_data(id):
    await asyncio.sleep(1)
    return f"Data {id}"

async def main():
    # Run multiple async functions in parallel with concurrency limit
    results = await run_parallel(
        lambda: fetch_data(1),
        lambda: fetch_data(2),
        lambda: fetch_data(3),
        limit=20  # Max concurrent tasks
    )
    print(results)

asyncio.run(main())
```

### Database Client (AsyncDB)

Built on SQLAlchemy 2.0 Core async. Dataclass-as-schema ergonomics; SQLite
behind the scenes with a tuned PRAGMA set (WAL, foreign_keys=ON, mmap_size,
temp_store=MEMORY, wal_autocheckpoint, busy_timeout=30s). Two engines per
db: a `StaticPool` writer (single conn, serialised by an in-process write
lock) and a 4-conn reader pool for concurrent WAL reads.

```python
import asyncio
from dataclasses import dataclass, field
from esuls import AsyncDB, BaseModel

@dataclass
class User(BaseModel):                          # id + created_at + updated_at, all UTC
    name: str = field(default="", metadata={"index": True})
    email: str = field(default="", metadata={"unique": True})
    age: int = 0

async def main():
    db = AsyncDB("users.db", "users", User)

    # Single upsert (idempotent by primary key).
    await db.save(User(name="Alice", email="alice@example.com", age=30))

    # Atomic batch — one round-trip, fail-fast on any bad item.
    await db.save_batch([
        User(name="Bob",     email="bob@example.com",     age=25),
        User(name="Charlie", email="charlie@example.com", age=35),
    ])

    # Best-effort batch — per-item loop, log+skip rotten items.
    await db.save_each([User(name="x", email=""), User(name="y", email="z@e.com")])

    # Queries.
    results = await db.find(name="Alice")
    adults  = await db.find(age__gte=18, order_by="-age")
    count   = await db.count(age__gte=18)
    user    = await db.get_by_id("some-uuid")

    # Bulk updates / deletes (both require at least one filter).
    n = await db.update_many({"status": "verified"}, email__like="%@example.com")
    n = await db.delete_many(age__lt=18)

    # Aggregations: count / count_distinct / sum / avg / min / max + GROUP BY + HAVING.
    by_age = await db.aggregate(
        group_by="age",
        count=True, count_distinct="email",
        having={"count__gte": 2},
        order_by="-count",
    )

    # Stream big result sets without materialising in memory.
    async for u in db.stream(order_by="created_at", batch_size=500):
        process(u)

    # Project specific columns (returns List[dict], not List[User]).
    rows = await db.find_columns(["id", "email"], age__gte=18)

    # Always close: PRAGMA optimize + wal_checkpoint(TRUNCATE) on exit.
    await db.close()

asyncio.run(main())
```

**Query operators** (suffix on filter kwargs, e.g. `age__gte=18`):
- `eq` (default), `neq`, `gt`, `gte`, `lt`, `lte`
- `like` — SQL `LIKE`
- `in`, `not_in` — pass any iterable
- `is_null=True/False`, `not_null=True/False`
- `between=(lo, hi)` — inclusive

`HAVING` in `aggregate()` accepts the same suffixes — but on alias names
(`count`, `count_distinct_<col>`, `sum_<col>`, …), not raw columns.

#### Schema base classes

Pick the base class that matches the table's needs:

| Base | Primary key | Timestamps | Use for |
|---|---|---|---|
| `IdModel` | `str` (UUID4) | — | Cache / lookup tables — no `created_at`/`updated_at` columns |
| `IntIdModel` | `int` autoincrement | — | High-throughput tables where UUID string PK fragments the B-tree |
| `TimestampedModel` | `str` (UUID4) | `created_at` + `updated_at` (UTC) | The default — same as `BaseModel` alias |
| `TimestampedIntModel` | `int` autoincrement | `created_at` + `updated_at` (UTC) | Combines int PK perf + auto-managed timestamps |

`BaseModel` is a backward-compat alias for `TimestampedModel`. Timestamps
use the public `utcnow()` clock (always timezone-aware UTC).

#### File-based migrations

Drop `NNN_*.py` scripts next to the db file under `migrations/`. Each
exports `version`, `description`, and `async def upgrade(conn)`:

```python
# my_app/migrations/001_add_email_index.py
from sqlalchemy import text

version = 1
description = "Index users.email for login lookup"

async def upgrade(conn):
    await conn.execute(text("CREATE INDEX idx_users_email ON users(email)"))
```

AsyncDB picks them up on first use, applies pending ones inside the same
transaction as schema init (atomic), and stamps `PRAGMA user_version`.
Fresh databases leap-frog to the latest version (the dataclass already
represents the post-migration state). Migrations are forward-only.

```python
# Inspect without opening a db:
from esuls import discover_migrations
print(discover_migrations(Path("my_app/migrations")))

# Inspect with applied/pending status:
db = AsyncDB(...)
print(await db.list_migrations())
```

#### Concurrency model

- Writes serialise through a per-loop, per-db-path `asyncio.Lock` + a
  single physical writer connection. No SQLITE_BUSY surfaces to the
  caller; transient contention is retried with exponential backoff
  (jittered) up to `max_retries` (configurable per call).
- Reads use a 4-conn pool — true concurrent reads under WAL.
- Cross-loop reuse is supported: the same `AsyncDB` instance survives
  multiple `asyncio.run()` calls because locks/engines are keyed on the
  running loop.
- `await db.close()` (or `async with AsyncDB(...)`) is required for
  clean fd release. Skipping it emits a `ResourceWarning` when the
  engines are GC'd; aiosqlite's worker thread is daemon-marked so the
  process never hangs on missing close.

### HTTP Request Client

```python
import asyncio
from esuls import AsyncRequest, make_request

# Using context manager (recommended for multiple requests)
async def example1():
    async with AsyncRequest() as client:
        response = await client.request(
            url="https://api.example.com/data",
            method="GET",
            add_user_agent=True,
            max_attempt=3,
            timeout_request=30
        )
        if response:
            data = response.json()
            print(data)

# Using standalone function (uses shared connection pool)
async def example2():
    response = await make_request(
        url="https://api.example.com/users",
        method="POST",
        json_data={"name": "Alice", "email": "alice@example.com"},
        headers={"Authorization": "Bearer token"},
        max_attempt=5,
        force_response=True  # Return response even on error
    )
    if response:
        print(response.status_code)
        print(response.text)

asyncio.run(example1())
```

**Request Parameters:**
- `url` - Request URL
- `method` - HTTP method (GET, POST, PUT, DELETE, etc.)
- `headers` - Request headers
- `cookies` - Cookies dict
- `params` - URL parameters
- `json_data` - JSON body
- `files` - Multipart file upload
- `proxy` - Proxy URL
- `timeout_request` - Timeout in seconds (default: 60)
- `max_attempt` - Max retry attempts (default: 10)
- `force_response` - Return response even on error (default: False)
- `json_response` - Validate JSON response (default: False)
- `json_response_check` - Check for key in JSON response
- `skip_response` - Skip if text contains pattern(s)
- `exception_sleep` - Delay between retries in seconds (default: 10)
- `add_user_agent` - Add random User-Agent header (default: False)

### CloudFlare Bypass

```python
import asyncio
from esuls import make_request_cffi

async def fetch_protected_page():
    html = await make_request_cffi("https://protected-site.com")
    if html:
        print(html)

asyncio.run(fetch_protected_page())
```

## Development

### Project Structure

```
utils/
├── pyproject.toml
├── README.md
├── LICENSE
└── src/
    └── esuls/
        ├── __init__.py
        ├── utils.py          # Parallel execution utilities
        ├── db_cli.py         # AsyncDB with dataclass schemas
        └── request_cli.py    # Async HTTP client
```

### Local Development Installation

```bash
# Navigate to the project
cd utils

# Install in editable mode with uv
uv pip install -e .

# Or with pip
pip install -e .
```

### Building and Publishing

```bash
# With uv
uv build && twine upload dist/*

# Or with traditional tools
pip install build twine
python -m build
twine upload dist/*
```

## Advanced Features

### AsyncDB Schema Definition

```python
from dataclasses import dataclass, field
from esuls import BaseModel
from datetime import datetime
from typing import Optional, List
import enum

class Status(enum.Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"

@dataclass
class User(BaseModel):
    # BaseModel provides: id, created_at, updated_at

    # Indexed field
    email: str = field(metadata={"index": True, "unique": True})

    # Simple fields
    name: str = ""
    age: int = 0

    # Enum support
    status: Status = Status.ACTIVE

    # JSON-serialized complex types
    tags: List[str] = field(default_factory=list)

    # Optional fields
    phone: Optional[str] = None

    # Table constraints (optional)
    __table_constraints__ = [
        "CHECK (age >= 0)"
    ]
```

### Connection Pooling & Performance

The HTTP client uses:
- Shared connection pool (prevents "too many open files" errors)
- Automatic retry with exponential backoff
- SSL optimization
- Random User-Agent rotation
- Cookie and header persistence

## License

MIT License
