# Changelog

## 0.5.0 — 2026-07-27

### Declared indexes are now actually created on an existing table

`AsyncDB` retrofits missing **columns** onto a table it did not create, then
called `metadata.create_all` to "pick up any new indexes". It never did:
SQLAlchemy's `checkfirst` skips an existing table **wholesale**, indexes
included. So adding `metadata={"index": True}` — or, worse,
`{"unique": True}` — to a dataclass whose table already existed added the
column and silently enforced nothing. The declaration read as binding while
duplicates kept being accepted, and the only way to find out was a corrupted
invariant much later. Two consumers had already hand-written raw
`CREATE UNIQUE INDEX` statements in their app startup to work around it.

A new `_ensure_indexes` runs in the existing-table branch of schema init and
issues `CREATE [UNIQUE] INDEX IF NOT EXISTS` for every declared index the live
table lacks. Idempotent, and unchanged for freshly created tables.

**A UNIQUE index whose column already holds duplicates is SKIPPED**, not
attempted: SQLite refuses to build it, and that error would abort schema init —
i.e. startup — for a database that accumulated duplicates *precisely because*
the constraint was missing. The column is logged at ERROR with the index name
instead. Deduplicating is a decision for a human with the domain in mind, not
for a boot path. Fix the data, restart, and the index is created.

**Minor, not patch:** on an existing deployment this begins creating indexes
that were silently absent. Expect a one-off cost on the first boot after
upgrading for large tables, and check the logs for skipped UNIQUE indexes —
each one is a constraint your code believes it has and does not.

## 0.4.0 — 2026-07-09

### Optional dependencies: the heavy stack is now behind per-feature extras

The core install used to pull **everything** — httpx, curl_cffi, Pillow, pypdf,
python-magic, and Playwright (~170 MB with its Node driver). A consumer that
only used the DB/config layer (e.g. an SSR web app) still dragged the whole
scraping stack into its virtualenv and, when compiled with Nuitka, into the
binary. Now the core is just the async utils + the SQLAlchemy DB layer, and each
heavier surface is an **optional extra**:

| install | adds |
|---|---|
| `esuls` | core: `AsyncDB`, models, `run_parallel` |
| `esuls[config]` | `load_config` / `generate_example_files` (OmegaConf) |
| `esuls[http]` | `make_request` / `AsyncRequest` (httpx) |
| `esuls[scraping]` | `make_request_cffi` / `make_request_playwright` (curl_cffi + Playwright) |
| `esuls[pdf]` | PDF metadata/inspection + the `esuls-pdf` CLI |
| `esuls[icons]` | `download_icon` (Pillow + libmagic) |
| `esuls[all]` | everything (the pre-0.4 default) |

**BREAKING (install-time only — the Python API is unchanged).** If you use the
HTTP/scraping/PDF/icon helpers, add the matching extra. The fastest migration is
the catch-all: change your dependency `esuls` → **`esuls[all]`** and nothing else
moves. The `esuls-pdf` console script now requires `esuls[pdf]`.

### Lazy public API (PEP 562)

`from esuls import AsyncDB` (and every other symbol) still works exactly as
before, but the top-level package now imports the optional submodules **lazily**,
on first access. So `import esuls` for DB/config work no longer imports
Playwright at all. Accessing a symbol whose extra isn't installed raises a clear
`ImportError` that names the extra to install, instead of a cryptic
`No module named 'playwright_stealth'`.

Internally, `request_cli` moved its `curl_cffi` / `fake_useragent` /
`playwright_stealth` imports **function-local**, so importing the module (or the
httpx-only `make_request` path) needs only httpx; `fake_useragent` is now soft
(falls back to a static UA if absent). Guarded by `tests/test_lazy_imports.py`,
which asserts `import esuls` never pulls the heavy stack into `sys.modules`.

### Dev tooling → PEP 735 dependency groups

The `dev` **extra** became a `[dependency-groups]` entry, so test tooling no
longer ships in the wheel or leaks into consumers. Run the suite with
`uv run --all-extras --group dev pytest`.

## 0.2.5 — 2026-07-06

### `load_config`: `*.local.yaml` now overrides the committed defaults

`load_config` merged the YAML files in plain alphabetical order. Because
`config.local.yaml` sorts *before* `config.yaml` (`'l' < 'y'`), the local file
was merged **first** and the committed `config.yaml` **won** every shared key —
the exact opposite of what a "local override" file is supposed to do.

`*.local.yaml` files are now ordered to merge **last**, so local values
(secrets **and** per-environment overrides) take precedence over the
version-controlled defaults. Non-local files, then local files, each keep
alphabetical order within their group.

**Behavior change:** if you relied on `config.yaml` winning a key that is also
present in a `*.local.yaml`, that key now resolves to the local value. The
common case — disjoint key sets, i.e. secrets *only* in the local file — is
unaffected. Covered by `tests/test_config_loader.py`.

## 0.2.0 — 2026-05-15

### Architecture: AsyncDB now built on SQLAlchemy 2.0 Core async

Internals replaced; **public API stays compatible** (`save`, `find`, `count`,
`delete`, `update_fields`, `get_by_id`, `exists`, `fetch_all`, `transaction`,
`close`) except where explicitly noted below. SQLite is still the backend
(via `aiosqlite`); SQLA Core handles SQL construction, parameter binding,
type roundtripping, and connection pooling.

#### Bug fixes (these were data-correctness footguns in 0.1.x)

- **`str` columns no longer auto-decode JSON.** Previously the read path
  blindly tried `json.loads(value)` on every string column, silently
  turning a TEXT field holding `"123"` into `int 123`, `"true"` into
  `bool`, etc. With the new TypeDecorator system, plain `str` fields
  stay strings. **Regression test:** `test_str_column_holding_json_text_stays_str`.
- **`Decimal` precision preserved.** The old code coerced to `float`,
  truncating arbitrary-precision decimals. Now stored as TEXT via a
  dedicated decorator; `Decimal("3.14159265358979323846")` round-trips
  exactly.
- **PEP 604 unions in schemas (`int | None`)** are recognised. The old
  type detector only handled `typing.Union`/`Optional` and treated
  `int | None` as the fallback JSON path.
- **No silent `ast.literal_eval` recovery on bytes columns.** Out-of-band
  writes that bypassed the TypeDecorator (raw `sqlite3` connections)
  are now passed through unchanged on read instead of being silently
  "recovered" as bytes from string literals.

#### Performance improvements

- **Concurrent reads via a 4-connection reader pool.** Previously a
  single persistent connection serialised all reads; now WAL-mode
  reads run in parallel up to `pool_size`.
- **Tuned PRAGMA set on every connect** (writer + reader): `journal_mode=WAL`,
  `synchronous=NORMAL`, `foreign_keys=ON`, `mmap_size=256MB`,
  `temp_store=MEMORY`, `cache_size=64MB`, `busy_timeout=30000`,
  `wal_autocheckpoint=1000`.
- **`PRAGMA optimize`** runs on every `close()`; **`wal_checkpoint(TRUNCATE)`**
  bounds WAL growth at close.
- **`db.checkpoint(mode)`** exposes manual WAL checkpoint control.

#### New schema base classes

`BaseModel` is now an alias for `TimestampedModel`. Pick a base by needs:

- `IdModel` — `str` PK only, no timestamps. Cache/lookup tables.
- `IntIdModel` — `int` autoincrement PK only. High-throughput tables
  where UUID string PK fragments the B-tree.
- `TimestampedModel(IdModel)` — `str` PK + `created_at`/`updated_at`
  (the legacy `BaseModel` shape).
- `TimestampedIntModel(IntIdModel)` — `int` PK + timestamps.

#### New query / write API

- **`save_batch(items)`** is now always atomic `executemany` (fail-fast).
- **`save_each(items)`** is the new per-item-loop variant that logs and
  skips rotten items. The old `save_batch(items, skip_errors=True)`
  forwards here with a `DeprecationWarning`.
- **`update_many(values, **filters)`** — bulk update (requires at least
  one filter; auto-stamps `updated_at` when the schema has it).
- **`find_columns(columns, **filters)`** — projection; returns
  `List[dict]` instead of `List[SchemaType]`.
- **`aggregate(...)`** — `GROUP BY` with `count` / `count_distinct` /
  `sum` / `avg` / `min` / `max`, `HAVING` (full operator suite), and
  alias-aware `order_by` / `limit`.
- **`stream(**filters, batch_size=N)`** — `AsyncIterator[SchemaType]`
  for large result sets; uses SQLA's `yield_per` cursor streaming.
- **`max_retries` kwarg** on every write method (`save`, `save_batch`,
  `save_each`, `delete`, `delete_many`, `update_fields`, `update_many`).
- **Filter operators added:** `is_null`, `not_null`, `not_in`, `between`.
- **`strict_schema=True`** constructor flag — promote schema-drift
  warnings (type mismatch, NOT NULL violated by retrofit, orphan
  columns) into `RuntimeError`.

#### File-based migrations (tier 2)

`AsyncDB` automatically probes `<db_path>.parent / "migrations"` for
scripts named `NNN_description.py` exporting `version`, `description`,
and `async def upgrade(conn)`. Pending migrations apply inside the
schema-init transaction (atomic) and stamp `PRAGMA user_version`.
Forward-only by design; no `downgrade()` support.

- `discover_migrations(path)` — pre-open introspection (no DB connection).
- `db.list_migrations()` — applied/pending status against the live DB.

#### Other correctness improvements

- **UTC everywhere.** `created_at` / `updated_at` use a public
  `utcnow()` clock returning timezone-aware UTC datetimes. The custom
  `DateTime` decorator stores ISO-with-offset strings and reads them
  back as tz-aware (works around SQLA's SQLite stock `DateTime(timezone=True)`
  losing the offset on read).
- **Transactional DDL.** `isolation_level=None` + explicit `BEGIN`
  emission ensures `CREATE TABLE` / `ALTER TABLE` issued inside a
  transaction roll back on failure. Previously DDL implicit-committed
  before the SQLA transaction could wrap it.
- **`save(skip_errors=True)` no longer swallows programming errors.**
  Only `sa_exc.SQLAlchemyError` / `sqlite3.Error` are caught and turned
  into a `False` return; `TypeError`/`ValueError`/etc. from caller bugs
  propagate as before.
- **Retry backoff has jitter** (`×0.5..×1.5`) so concurrent retries
  desynchronise on transient BUSY.
- **`ResourceWarning` on undisposed engine** — if you drop an AsyncDB
  without `await db.close()`, GC fires a warning instead of silently
  leaking fds until process exit. The aiosqlite worker thread is
  daemon-marked, so the process still exits cleanly.
- **Cross-loop reuse preserved.** The same AsyncDB instance survives
  multiple `asyncio.run()` calls; locks and engines are keyed per loop.
- **Schema-drift detection.** Logs warnings (or raises with
  `strict_schema=True`) for type mismatches, retrofitted columns that
  can't enforce NOT NULL, and orphan columns. Type-equivalence map
  prevents false positives for `BOOLEAN ↔ INTEGER`, `DATETIME ↔ TEXT`,
  `VARCHAR ↔ TEXT`.

### Breaking changes (vs 0.1.x)

- `save_batch(skip_errors=True/False)` is **deprecated** (still works,
  emits `DeprecationWarning`). Migrate to `save_batch(items)` (fail-fast)
  or `save_each(items)` (best-effort).
- `transaction()` now yields a SQLA `AsyncConnection` instead of an
  `aiosqlite.Connection`. Raw SQL needs `text(...)` wrapping;
  `result.fetchone()` is sync (the I/O happened during `execute`).
- Datetime columns declared SQL type changed from `TIMESTAMP` to `TEXT`
  (custom decorator). SQLite is dynamically typed; this changes the
  `PRAGMA table_info` declared name only — affinity and behavior are
  equivalent. Drift detection treats them as interchangeable.
- Decimal columns declared SQL type changed from `NUMERIC` to `TEXT`
  (custom decorator). Same caveat as above; values now round-trip
  losslessly instead of going through `float`.

### Test suite

- Migrated from a custom `asyncio.run()`-per-file runner to pytest +
  pytest-asyncio (`asyncio_mode = "auto"`, function-scoped loops).
- 164 tests pass with 91% coverage on `src/esuls/db_cli.py`.
- New regression suites: `test_db_sqla.py` (TypeDecorators, PRAGMAs,
  read pool, BaseModel variants, all new API surface), `test_db_migrations.py`
  (discovery + apply + rollback).

### Dependencies

- Added: `sqlalchemy[asyncio]>=2.0.36`.
- Dev extras: `pytest>=8.3`, `pytest-asyncio>=0.24`, `pytest-cov>=5.0`.
- Kept: `aiosqlite==0.22.1` (used as SQLA's async SQLite driver).
