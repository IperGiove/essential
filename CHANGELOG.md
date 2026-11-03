# Changelog

## 0.12.1 — 2026-11-03

### `inspect --full` no longer prints every logo as "0% of page"

Image placements below 10% of the page are shown with one decimal
(`0.4% of page`); whole-number rounding made every logo and icon read "0%".
Display only: `page_fraction` in the report was already exact.

## 0.12.0 — 2026-11-03

### Text hidden under images

The cheapest edit of a PDF is not editing the text — that needs a font with
the right glyphs — but painting an image over a value (a screenshot of the new
number, or a plain box) and typing the new value on top. Nothing is added to
the fonts, so the font checks stay silent, while the original text is still in
the file, underneath.

New `esuls.pdf.layout.read_pdf_image_placements()`: a small content-stream
interpreter (graphics-state stack, CTM, text matrices and `TJ` kerning, Form
XObjects with their `/Matrix`, inline `BI…EI` images) that returns every image
placement with its box on the page, its share of the page, whether it has an
alpha/colour-key mask, the **earlier** visible text it covers and the
**later** text drawn over it. Text is decoded with pypdf's font tables;
undecodable runs still count, as `[n glyphs]`.

Paint order is the discriminator: an image only "covers" text painted before
it. Backgrounds and letterheads (painted first) and OCR layers (invisible
text, `Tr 3`) never count.

| code | severity | when |
|---|---|---|
| `content.text_covered_by_image` | warning | an opaque image is painted over earlier text — the finding quotes the hidden text and what was drawn on top |
| `content.text_covered_by_image` | info | the same, but the image is transparent (stamps, logos with alpha) |

`PdfReport.content["placements"]` carries the data; `esuls-pdf inspect --full`
prints each placement in paint order. Tests: `test_pdf_layout.py`. Positions
are estimates (glyph widths, centre-in-box rule); vector shapes (a white
rectangle drawn with `re f`) are not considered yet.

## 0.11.0 — 2026-11-03

### `esuls.pdf` verifies signatures instead of counting them

Until now a signed PDF earned one line in the summary — `1 signature(s)` — and
an exemption: `structure.incremental_updates` skips signed files, because
signing *is* an incremental update. So the cheapest forgery there is, editing a
signed invoice and leaving the signature dictionary in place, came out
**clean**.

The new `pdf-sign` extra (`pip install 'esuls[pdf-sign]'`, pulled in by `all`)
adds `verify_pdf_signatures()` / `async_verify_pdf_signatures()`, built on
pyHanko. Each embedded signature and document timestamp becomes a
`SignatureCheck`: `intact` (digest over `/ByteRange`), `valid` (CMS signature
against the signer key), `trusted` (chain to a root you supply), `revoked`,
`coverage`, `modification_level` / `docmdp_ok` (pyHanko's diff analysis of the
revisions appended afterwards), and the embedded timestamp's trust.

`check_pdf()` runs it whenever the file has signatures, and the results become
findings:

| code | severity | when |
|---|---|---|
| `signature.digest_mismatch` | critical | a signed byte was changed |
| `signature.invalid` | critical | the signature was not made by the certificate's key |
| `signature.revoked` | critical | signer certificate revoked (from data embedded in the file) |
| `signature.modified_after_signing` | critical | later revisions go beyond what the signature permits |
| `signature.unparseable` | critical | pyHanko cannot validate this signature at all |
| `signature.untrusted` | warning | valid, but does not chain to a trust root |
| `signature.timestamp_untrusted` | warning | the embedded timestamp token is not trusted |
| `signature.read_error` | warning | pypdf sees signatures, pyHanko cannot parse the file |
| `signature.unsigned_updates` | info | revisions after the last signature that pyHanko deems harmless |
| `signature.not_verified` | info | pyHanko missing, or `verify_signatures=False` |

`PdfReport.signatures` carries the per-signature results (and `to_dict()`
serialises them). `esuls-pdf check` gains `--trust-root PEM/DER` (repeatable)
and `--no-verify-signatures`, and prints one line per signature.

Two deliberate choices:

- **Trust is explicit.** Only the roots passed in are trusted; the OS store is
  never used. It holds TLS server CAs, not document-signing trust lists, it
  differs between machines, and pyHanko is removing the fallback. Without
  `trust_roots` a signed file is therefore at best `suspicious`.
- **Validation is offline.** No OCSP/CRL/AIA fetching: those URLs come from
  the (untrusted) file, and network I/O would make the verdict depend on when
  you ran it.

Known gap, surfaced rather than hidden: pyHanko classifies an Info-dict edit
after signing as a metadata-only update, the same bucket as LTV data, so the
signature stays valid. Such a file gets `signature.unsigned_updates` (info)
with the modification level, not a worse verdict.

`import esuls.pdf` still does not import pyHanko; it loads on first use.
`src/esuls/tests/test_pdf_signatures.py` covers it on synthetic PDFs signed
with self-signed and CA-issued certificates minted per test: byte tampering, a
key that does not match its certificate, a same-name impostor certificate,
edits after signing, countersignatures, expiry and `validation_time`, signature
and document timestamps (pyHanko's dummy TSA), use inside a running event
loop, and the CLI exit codes.

### Fonts and images as evidence of editing

Typing onto an existing PDF needs glyphs the original font subset does not
have, so editors add a NEW font; pasting a logo or a patch over a number adds
a new image. New module `esuls.pdf.content`:

- `read_pdf_fonts()` / `read_pdf_images()` / `read_pdf_content()` — what page
  content uses (resources inherited from `/Pages`, nested Form XObjects
  included): font family, subset prefix, type, embedded or not; image size,
  filters, colour space, and the image editor named in a JPEG's metadata.
- `read_pdf_revision_content()` — rebuilds each earlier revision (the file
  cut at its `%%EOF`, skipping the extra one of linearized files) and reports
  the fonts/images each update added, plus fonts rewritten in place under the
  same object number. Capped at the last 32 updates.

Annotation appearances (signature widgets, form-field values, comments) are
deliberately not page content: they are legitimate later additions.

| code | severity | when |
|---|---|---|
| `content.fonts_added_in_update` | warning | a later revision added a font to page content |
| `content.fonts_modified_in_update` | warning | a later revision rewrote a font object (e.g. new subset) |
| `content.images_added_in_update` | warning | a later revision added an image to page content |
| `content.image_editor_metadata` | warning | a JPEG says Photoshop, GIMP, Paint.NET, … |
| `content.duplicate_font_subset` | info | two subsets of one face, same type, on one page |
| `content.mixed_font_embedding` | info | a non-embedded, non-standard font next to embedded ones |

The single-revision heuristics are info on purpose: run over 42 real-world
PDFs, the only hits were duplicate subsets from Chromium/Skia and an Acrobat
"combine files" — legitimate producers. The revision diff is the strong signal,
and it only exists while the file has not been fully re-saved.

`PdfReport.content` carries the inventory; `esuls-pdf inspect --full` prints
fonts, images and per-revision additions. Tests: `test_pdf_content.py`.

## 0.10.0 — 2026-09-04

### The writer takes its lock up front, so a second process can exist

A plain `BEGIN` is DEFERRED: the transaction takes no lock until its first
statement, and a read-only one. A transaction that reads and then writes — which
is nearly every interesting one, `save()` and `update_fields()` included — has to
UPGRADE that shared lock partway through, and SQLite refuses the upgrade with
SQLITE_BUSY **immediately**, without consulting `busy_timeout`. Honouring the
timeout there would deadlock: both connections would sit holding a read lock,
each waiting for the other to drop one. So the caller gets "database is locked"
with no waiting and no retry.

Inside ONE process it never shows. The writer is a `StaticPool` of a single
connection, so writers already queue in Python and no two ever race. It appears
the moment a second PROCESS opens the same file — N uvicorn workers, a cron job,
a maintenance script run while the site is up. Measured on a real site during a
load test, 6 workers, 70 seconds:

| | writes accepted | "database is locked" | throughput |
|---|---|---|---|
| `BEGIN` (deferred) | 44% | 246.910 | — |
| `BEGIN IMMEDIATE` | **100%** | **0** | **+40%** |

The writer engine now begins with `BEGIN IMMEDIATE`. The lock is taken at the
start, there is nothing to upgrade, and `busy_timeout` does the job it was
always meant to: writers queue instead of failing. The READER keeps the deferred
`BEGIN` — it never upgrades, and a deferred read is what lets WAL readers run
beside the writer.

`src/esuls/tests/test_multiprocess_writes.py` is the regression: six forked
processes, a hundred read-then-write transactions each, and not one lost. It
fails on the old behaviour with 438 of 600 writes rejected.

### `checkpoint()` no longer runs inside a write transaction

Fallout from the above, and a latent bug it made visible: `checkpoint()` used
`writer.connect()`, so with `BEGIN IMMEDIATE` its first statement took a write
lock and `PRAGMA wal_checkpoint` failed with SQLITE_LOCKED. A checkpoint is a
file-level operation, not a query, and has no business inside a transaction —
it now runs on a raw connection, the same shape `close()` already used.

## 0.9.0 — 2026-09-03

### Foreign keys become declarable

`metadata={"foreign_key": "parent.id"}` has been in the schema builder since the
SQLAlchemy port, and in the layout this library encourages — one `AsyncDB` per
table — it could never work. Each instance built its own `MetaData`, and
SQLAlchemy resolves a `ForeignKey` by looking the referenced table up in the
*same* `MetaData`, so the reference had nothing to resolve against and DDL died
with `NoReferencedTableError`. The feature was documented, tested only for the
PRAGMA being live, and undeclarable in practice.

**One `MetaData` per database file now, shared by every `AsyncDB` on it.** The
file is the right boundary and not a compromise: SQLite cannot hold a constraint
across two database files any more than it can make a transaction atomic across
them, so two tables that can reference each other are exactly two tables in one
file.

```python
@dataclass
class Enrollment(TimestampedModel):
    group_id: str = field(default=None, metadata={
        "index": True, "foreign_key": "class_group.id", "on_delete": "CASCADE"})

DB_GROUP      = AsyncDB("app.db", "class_group", Group)
DB_ENROLLMENT = AsyncDB("app.db", "enrollment", Enrollment)
```

The constraint reaches the schema, so `PRAGMA foreign_keys=ON` finally has
something to enforce: an orphan INSERT raises `IntegrityError`, and
`ON DELETE CASCADE` runs in the database instead of being an application's
job to remember.

Declaration order does not matter — a reference resolves when the DDL is
emitted, not when the dataclass is read — but the referenced model's `AsyncDB`
must have been constructed before the first *use* of the referring one. When it
has not been, the `NoReferencedTableError` now says so and says what to do,
instead of naming a table the caller never wrote.

`create_all` deliberately runs over the whole file's `MetaData` rather than the
one table: SQLAlchemy emits `CREATE TABLE` in dependency order, so a child
declared before its parent still lands after it. Restricting it would create a
child whose parent table does not exist, and with `foreign_keys=ON` that
surfaces much later, as `no such table` on the first INSERT.

Re-declaring a table with a *different* dataclass keeps working and keeps
meaning what it meant: that is what schema drift looks like from inside one
process (a model that gained a column since the table was created), so the new
declaration replaces the old one and the retrofit path reconciles it with the
live database. Two `AsyncDB` built from the *same* class share one `Table`.

### A new table no longer burns its neighbours' migrations

`PRAGMA user_version` is a property of the DATABASE. "Is it new?" was asked
about one TABLE. So adding a table to a database that already existed took the
fresh-schema path and **leap-frogged `user_version` to the highest declared
migration**, skipping every migration still pending for every *other* table in
the file.

It skipped them silently. Nothing raised, nothing logged: the migration simply
never ran, and because the pointer had already moved past it, it never ran on
any later start either. Adding a table to a live schema is one of the most
ordinary things a project does, which is exactly what kept this quiet.

A new table in an existing database now applies what is pending, like every
other path. A brand-new *database* still leap-frogs, which was always the
correct half of the rule: a schema `create_all` just built from the current
dataclasses is by definition current, and re-running migrations against it would
only re-do what CREATE TABLE did.

**Upgrading:** on the first start after this release, any migration that was
silently skipped will actually run. That is the point, and it is also the thing
to look at before deploying — if one of those was applied by hand in the
meantime, it will be applied again. Check `PRAGMA user_version` against your
`migrations/` directory on each live database first.

### Notes

No API surface changed, and nothing needs updating in a project that declares
no foreign keys and has no `migrations/` directory.

## 0.8.0 — 2026-09-03

### Foreign keys become declarable

`metadata={"foreign_key": "parent.id"}` has been in the schema builder since the
SQLAlchemy port, and in the layout this library encourages — one `AsyncDB` per
table — it could never work. Each instance built its own `MetaData`, and
SQLAlchemy resolves a `ForeignKey` by looking the referenced table up in the
*same* `MetaData`, so the reference had nothing to resolve against and DDL died
with `NoReferencedTableError`. The feature was documented, tested only for the
PRAGMA being live, and undeclarable in practice.

**One `MetaData` per database file now, shared by every `AsyncDB` on it.** The
file is the right boundary and not a compromise: SQLite cannot hold a constraint
across two database files any more than it can make a transaction atomic across
them, so two tables that can reference each other are exactly two tables in one
file.

```python
@dataclass
class Enrollment(TimestampedModel):
    group_id: str = field(default=None, metadata={
        "index": True, "foreign_key": "class_group.id", "on_delete": "CASCADE"})

DB_GROUP      = AsyncDB("app.db", "class_group", Group)
DB_ENROLLMENT = AsyncDB("app.db", "enrollment", Enrollment)
```

The constraint reaches the schema, so `PRAGMA foreign_keys=ON` finally has
something to enforce: an orphan INSERT raises `IntegrityError`, and
`ON DELETE CASCADE` runs in the database instead of being an application's
job to remember.

Declaration order does not matter — a reference resolves when the DDL is
emitted, not when the dataclass is read — but the referenced model's `AsyncDB`
must have been constructed before the first *use* of the referring one. When it
has not been, the `NoReferencedTableError` now says so and says what to do,
instead of naming a table the caller never wrote.

`create_all` deliberately runs over the whole file's `MetaData` rather than the
one table: SQLAlchemy emits `CREATE TABLE` in dependency order, so a child
declared before its parent still lands after it. Restricting it would create a
child whose parent table does not exist, and with `foreign_keys=ON` that
surfaces much later, as `no such table` on the first INSERT.

### Notes

Re-declaring a table with a *different* dataclass keeps working and keeps
meaning what it meant: that is what schema drift looks like from inside one
process (a model that gained a column since the table was created), so the new
declaration replaces the old one and the retrofit path reconciles it with the
live database. Two `AsyncDB` built from the *same* class share one `Table`.

No API surface changed, and nothing needs updating in a project that declares no
foreign keys.

## 0.7.0 — 2026-09-01

### The hot read path stops rebuilding what it can keep

Two changes, both inside `AsyncDB`, no API surface touched. A profile of
`get_by_id` said esuls' own code was **2.7% of the call** — everything else was
SQLAlchemy being asked to do the same work again on every query. So the work is
done once instead.

**One shared read connection per (database file, event loop).** Bounded reads no
longer check a connection out of the pool per query: the checkout, the
`Connection` object and the checkout/checkin events cost more than the query
itself. It is safe for the same reason the synchronous layer is: a bounded read
has no `await` inside, so the loop cannot interleave a second query onto it, and
the connection never leaves the loop's thread — threaded reads (`stream`,
`aggregate`, unindexed scans) keep taking their own.

That connection ends its read transaction after every query, and **this is the
part that is one mistake away from a silent bug**: SQLAlchemy opens a transaction
on first execute and holds it, so a connection living for the life of the process
would keep serving the snapshot it first saw — a row written a second ago
invisible for ever — and SQLite could never checkpoint the WAL past that open
read. `isolation_level="AUTOCOMMIT"` does not prevent it: this module's own
`begin` listener (which exists so migrations get transactional DDL) fires anyway
and emits a real `BEGIN`. `tests/test_db_sync_layer.py` pins it with an outside
`sqlite3` writer, and the test fails if the `rollback()` is removed.

**`get_by_id`'s statement is built once**, with the id as a bound parameter.
Rebuilding it per call meant coercing the comparison and re-deriving the
statement's cache key every time — the largest single entry in the profile.

Measured with `benchmarks/db_bench.py`, same machine, across the three versions:

| workload | 0.5.1 | 0.6.0 | 0.7.0 |
|---|---|---|---|
| 50 sequential `get_by_id` | 1,867/s | 4,912/s | **17,247/s** |
| 25 concurrent `find(limit=100)` | 259/s | 1,499/s | **1,951/s** |
| 50 filtered `find` | 503/s | 850/s | **1,140/s** |
| 50 sequential `count()` | 1,856/s | 2,599/s | **5,093/s** |
| 50 `update_fields` | 1,527/s | 4,767/s | **6,071/s** |
| 50 sequential `save()` | 940/s | 2,240/s | **2,369/s** |
| batch insert | 29,756/s | 39,142/s | **44,749/s** |

End to end on a FastAPI page issuing 11 queries: **122 → 372 → 645 req/s**. The
same page written against raw `sqlite3` by hand reaches 1,165, so what this layer
now costs is 1.8x — it was 3.9x when the measuring started.

## 0.6.0 — 2026-09-01

### The execution layer is synchronous; the API is unchanged

`AsyncDB` now drives SQLAlchemy's **synchronous** engine and runs bounded queries
inline on the event loop. Every public method is still `async def`, every call
site keeps its `await`, and `transaction()` still yields something you
`await conn.execute(...)` on — including the migration files sitting in consumer
repos, which get an adapter rather than a bare connection.

The reason is a measurement. A point SELECT on a warm page cache is **4
microseconds**; reaching it through aiosqlite + SQLAlchemy's async bridge cost
**247**. aiosqlite hands each statement to a worker thread and back — one full
event-loop yield per statement — and the async bridge adds greenlet round trips
on top, all to keep the loop free during a wait that does not exist for a local
file. The loop was being suspended for 4 microseconds of work and 240 of
ceremony.

What keeps this honest is that "bounded" is checked, not assumed. `_bounded()`
asks the SCHEMA whether the query can be answered from the primary key, a
declared index, a unique column or a LIMIT. If it can, it runs inline. If it
cannot — `fetch_all()`, `aggregate()`, `stream()`, a filter on an unindexed
column, a batch write whose size the caller chose — it goes to a worker thread,
because that is the query that can hold the loop for as long as the table is big.

Measured with `benchmarks/db_bench.py`, same machine, same run:

| workload | 0.5.1 | 0.6.0 |
|---|---|---|
| 50 sequential `get_by_id` | 1,867/s | **4,912/s** |
| 25 concurrent `find(limit=100)` | 259/s | **1,499/s** |
| 50 filtered `find` | 503/s | **850/s** |
| 50 sequential `save()` | 940/s | **2,240/s** (p99 6.65 ms → 1.13 ms) |
| 50 `update_fields` | 1,527/s | **4,767/s** |
| batch insert | 29,756/s | **39,142/s** |

End to end on a FastAPI page issuing 11 queries: 122 → 335 req/s. Hand-written
`sqlite3` on the same page reaches 471, so the gap that used to argue for
dropping this layer in favour of raw SQL is now 1.4x.

### Fix: `transaction()` could hand two callers one transaction

This is why 0.6.0 is not merely a speed release. `transaction()` deliberately did
not take the write lock, on the grounds that "the writer engine uses a StaticPool
of size 1, so two concurrent writers queue at the pool level". StaticPool does
not queue — it hands the **same connection** to every caller. Two overlapping
transactions therefore shared one connection and one transaction: the second
`BEGIN` raised `cannot start a transaction within a transaction`, and when the
timing let both through, a rollback in one erased writes the other had already
been told were committed. On a signup endpoint under a 200-caller rush that
produced 36 success responses against 30 rows — the data consistent, the answers
not.

A write transaction now holds the per-loop write lock for the whole block, so
concurrent transactions queue. Callers who had noticed and wrapped their own
mutex around it can drop it. `tests/test_db_sync_layer.py` pins the three
properties (no interleaving, no cross-transaction rollback, single writes queue
behind an open transaction); all three fail against 0.5.1.

### Fix: `close()` never actually checkpointed the WAL

`PRAGMA wal_checkpoint(TRUNCATE)` ran while both connection pools were still
open, so SQLite refused it with "database table is locked" — logged at DEBUG,
i.e. nowhere — and the `-wal` file carried across restarts instead of folding
back into the database. The checkpoint now runs last, on its own connection,
after both engines are disposed. Measured: a 1.8 MB `-wal` that used to survive
`close()` is now gone.

### New: `db.col("name")`

The column object, for the write the database must compute itself:
`update_many({"taken": db.col("taken") + 1}, id=x, taken__lt=db.col("capacity"))`.
Read-modify-write in Python is the most common way to lose data on this layer,
and it does not take multiple processes: 200 coroutines each reading a counter
before any of them writes land 200 increments as one. Measured on a real
endpoint, 200 concurrent claims on a 30-seat course wrote 91 rows through
read-then-write and exactly 30 through a single atomic statement. Naming a column
also lets a filter compare two of them, which is what removes the window between
"check the capacity" and "insert".

### Also

- Engines are process-global instead of per-event-loop (a synchronous engine has
  no loop to be bound to), which removes the "bound to a different event loop"
  class of failure the per-loop registry existed to work around. The write locks
  stay per-loop, because `asyncio.Lock` does bind.
- Engines and the schema-init flag are cached on the instance: reaching them used
  to cost a WeakKeyDictionary lookup keyed on the running loop plus a dict lookup
  on every call.

## 0.5.1 — 2026-07-27

### Fix: NULLs were counted as duplicates, refusing valid UNIQUE indexes

0.5.0's duplicate pre-check ran `GROUP BY col HAVING COUNT(*) > 1` over the
whole table. SQL groups all NULLs together, so N rows with a NULL in the indexed
column looked like N duplicates and the index was skipped — even though SQL
treats NULLs as DISTINCT and SQLite would have built it without complaint.

This hit the common case, not an edge one: an optional-but-unique column (a
handle, a slug, an external id) is mostly NULL before the feature that fills it
ships, so 0.5.0 would refuse the very index the column was declared to have. The
pre-check now excludes rows with a NULL in any indexed column, matching SQLite's
own rule (a row is exempt from a composite UNIQUE if ANY of its columns is NULL).

Found by running 0.5.0 against a real database whose `handle` column was
entirely NULL.

### Known gap, now documented and tested

`__unique_together__` compiles to a table-level `UniqueConstraint`, not an
`Index`, and SQLite cannot ALTER-add a table constraint — so it is still NOT
retrofitted onto an existing table. Adding one to a live model needs a migration
or a hand-written composite `CREATE UNIQUE INDEX`. Only per-field
`metadata={"unique": True}` / `{"index": True}` is retrofitted.

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
