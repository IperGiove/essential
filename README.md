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

## Probe (`esuls-probe`)

A self-assessment web probe: point it at **your own** sites to see what is
exposed and which defenses never trigger (a mini dirsearch plus basic security
checks). Only scan systems you own or are authorised to test.

```bash
pip install 'esuls[probe]'

esuls-probe headers   https://example.com             # security headers, cookie flags, http->https
esuls-probe exposed   https://example.com             # .git, .env, dumps, backups ... (content-validated)
esuls-probe paths     https://example.com -e php,bak --recursive --depth 2
esuls-probe paths     https://example.com -w my-words.txt
esuls-probe cors      https://example.com             # reflected Origin, null, wildcard + credentials
esuls-probe ratelimit https://example.com/login --path /login --method POST --data 'u=a&p=b' -n 100
esuls-probe full      https://example.com --json -o report.json
```

Outside-view checks for a VM or domain (HOST or URL accepted):

```bash
esuls-probe ports example.com --ports 22,80,443,8000-8010   # TCP connect scan (default ~40 ports)
esuls-probe tls   example.com                               # cert expiry/SAN/trust, TLS 1.0/1.1
esuls-probe email example.com --selector default            # SPF / DMARC / DKIM / MX (dnspython)
esuls-probe auth  https://example.com --auth-path /login --max-attempts 25
```

`ports` also fingerprints known service ports read-only (on by default, `--no-banner`
disables): FTP 21, SMTP 25/587, POP3 110, IMAP 143 and SSH 22 get banner + one
capability command (`FEAT`/`EHLO`/`CAPA`/`CAPABILITY`) + quit, no authentication; a
server with no `AUTH TLS`/`STARTTLS`/`STLS` is a medium "plaintext" finding, the banner
goes in the detail. Implicit-TLS ports 465/993/995 get a permissive TLS connect and an
info finding. The opt-in `--check-plaintext-auth` (more invasive, not used by `full`)
sends one non-credential login step to see whether TLS is enforced first; no password is
ever sent.

`ports` flags dangerous exposures (databases, Caddy admin :2019, RabbitMQ, VNC,
Prometheus) as medium and a Redis that answers `PING` without AUTH as high; it
sends nothing else. `auth` sends wrong credentials until the first defense
(429/503/403/lockout message) fires and then stops; if the cap is reached it
reports no lockout. **It triggers real bans (fail2ban, WAF) and can lock your
own IP: only run it on systems you own**; skip it in `full` with `--skip auth`.
Like `ratelimit`, loopback targets are reported as info unless you pass
`-H 'X-Forwarded-For: 203.0.113.7'`. `full` runs every HTTP check under its own simulated client IP (`X-Forwarded-For`
from 203.0.113.0/24, one per check) so the checks do not share a rate-limit bucket and a
ban from `paths` cannot make `ratelimit`/`auth` look locked at request #1. A user-supplied
`-H 'X-Forwarded-For: A.B.C.D'` is used as-is for the first HTTP check and the others vary
inside its /24. `full` also runs `ports`, `tls`, `email`
and `auth`; `tls` is skipped on plain `http://` targets and `email` on IPs.

Shared options: `-c/--concurrency` (10), `--timeout` (10s), `--delay`, `-H 'Name: value'`,
`--cookie`, `--user-agent`, `--insecure`, `--json`, `-o FILE`. Exit codes:
`0` nothing above info, `1` findings low or higher, `3` error.
`ratelimit` caveats: many frameworks exempt loopback clients from rate limiting
on purpose (hlweb's `Limiter` does), so probing `http://127.0.0.1:PORT` without
a forwarding header only yields an info note; add `-H 'X-Forwarded-For: 203.0.113.7'`
to simulate a remote client. A burst where every answer is an error (403, 422, ...)
is reported as "could not be assessed" (check `--data`, `--method`, headers).
Path discovery drops soft-404s (a random-path baseline), and sensitive files are
only reported when their content validates; secrets are never echoed in evidence.

```python
import asyncio
from esuls.probe import ProbeOptions, run_full_probe

report = asyncio.run(run_full_probe("https://example.com", ProbeOptions(concurrency=5)))
print(report.render_text())      # or report.to_dict()
raise SystemExit(report.exit_code)
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
