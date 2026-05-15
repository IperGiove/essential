"""
Microbenchmark for esuls.db_cli.AsyncDB.

Run after changes to db_cli.py / SQLA version / aiosqlite version to spot
regressions. Reports throughput + p50/p95/p99 latency for the workloads
that matter for SQLite + WAL: batch insert, point reads (sequential and
concurrent against the reader pool), filtered queries, concurrent writes.

Usage:
    uv run python benchmarks/db_bench.py                # default scenario
    uv run python benchmarks/db_bench.py --rows 50000   # bigger seed
    uv run python benchmarks/db_bench.py --quick        # fast smoke run
    uv run python benchmarks/db_bench.py --json out.json  # machine-readable

The default db lives in a tempdir and is deleted after the run.
"""
import argparse
import asyncio
import json
import statistics
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Awaitable

from esuls.db_cli import AsyncDB, BaseModel


@dataclass
class Row(BaseModel):
    name: str = ""
    value: int = 0
    payload: str = ""


def _percentiles(samples_ms: list[float]) -> tuple[float, float, float]:
    """Return (p50, p95, p99) in milliseconds. Sorted-quantile method."""
    if not samples_ms:
        return 0.0, 0.0, 0.0
    s = sorted(samples_ms)
    n = len(s)

    def q(p: float) -> float:
        idx = min(n - 1, int(p * n))
        return s[idx]

    return q(0.50), q(0.95), q(0.99)


async def _timed_each(make_coro: Callable[[int], Awaitable], n: int) -> list[float]:
    """Run `make_coro(i)` n times sequentially; return per-call latencies in ms.

    Pass an iteration index so callers can vary inputs per call (e.g. cycling
    through a list of ids) without resorting to closure-on-mutated-state.
    """
    out = []
    for i in range(n):
        t0 = time.perf_counter()
        await make_coro(i)
        out.append((time.perf_counter() - t0) * 1000)
    return out


def _report(name: str, total_s: float, ops: int, samples_ms: list[float] | None = None) -> dict:
    """Print a single benchmark line and return a result dict."""
    throughput = ops / total_s if total_s > 0 else 0.0
    line = f"  {name:38} {total_s*1000:9.1f} ms  {throughput:>10,.0f} op/s"
    p50 = p95 = p99 = None
    if samples_ms:
        p50, p95, p99 = _percentiles(samples_ms)
        line += f"  p50={p50:6.2f}ms p95={p95:6.2f}ms p99={p99:6.2f}ms"
    print(line)
    return {
        "name": name,
        "total_ms": total_s * 1000,
        "ops": ops,
        "ops_per_sec": throughput,
        "p50_ms": p50,
        "p95_ms": p95,
        "p99_ms": p99,
    }


async def bench(rows: int, iterations: int, concurrency: int) -> list[dict]:
    """Run the benchmark scenarios end-to-end. Returns a list of result dicts."""
    results: list[dict] = []
    with tempfile.TemporaryDirectory() as d:
        db = AsyncDB(Path(d) / "bench.db", "items", Row)

        # ── Seed ────────────────────────────────────────────────────
        items = [Row(name=f"n{i}", value=i, payload="x" * 64) for i in range(rows)]
        t0 = time.perf_counter()
        await db.save_batch(items)
        results.append(_report(
            f"seed {rows:>6,} rows (batch executemany)",
            time.perf_counter() - t0, rows,
        ))

        # ── Point reads, sequential (single-conn latency floor) ─────
        sample_ids = [r.id for r in await db.find(limit=min(iterations, rows))]
        n_ids = len(sample_ids)
        samples = await _timed_each(
            lambda i: db.get_by_id(sample_ids[i % n_ids]), iterations,
        )
        results.append(_report(
            f"{iterations} sequential get_by_id",
            sum(samples) / 1000, iterations, samples,
        ))

        # ── Concurrent reads (reader pool win) ──────────────────────
        t0 = time.perf_counter()
        await asyncio.gather(*[db.find(limit=100) for _ in range(concurrency)])
        results.append(_report(
            f"{concurrency} concurrent find(limit=100)",
            time.perf_counter() - t0, concurrency,
        ))

        # ── Filtered query (no index) ───────────────────────────────
        cutoff = rows // 10
        samples = await _timed_each(
            lambda i: db.find(value__gt=cutoff, limit=200), iterations,
        )
        results.append(_report(
            f"{iterations} filtered find (value__gt+limit)",
            sum(samples) / 1000, iterations, samples,
        ))

        # ── count() sequential ──────────────────────────────────────
        samples = await _timed_each(lambda i: db.count(), iterations)
        results.append(_report(
            f"{iterations} sequential count()",
            sum(samples) / 1000, iterations, samples,
        ))

        # ── Concurrent writes (single-writer floor) ─────────────────
        t0 = time.perf_counter()
        await asyncio.gather(*[
            db.save(Row(name=f"w{i}", value=i)) for i in range(concurrency)
        ])
        results.append(_report(
            f"{concurrency} concurrent save()",
            time.perf_counter() - t0, concurrency,
        ))

        # ── Single save() latency distribution ──────────────────────
        samples = await _timed_each(
            lambda i: db.save(Row(name=f"single_{i}", value=i)), iterations,
        )
        results.append(_report(
            f"{iterations} sequential save()",
            sum(samples) / 1000, iterations, samples,
        ))

        # ── update_fields by id ─────────────────────────────────────
        samples = await _timed_each(
            lambda i: db.update_fields(sample_ids[i % n_ids], value=42),
            iterations,
        )
        results.append(_report(
            f"{iterations} sequential update_fields",
            sum(samples) / 1000, iterations, samples,
        ))

        await db.close()

    return results


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--rows", type=int, default=10_000,
                   help="rows to seed before timing (default: 10000)")
    p.add_argument("--iterations", type=int, default=200,
                   help="sequential-op repetition count (default: 200)")
    p.add_argument("--concurrency", type=int, default=100,
                   help="concurrent gather() fan-out (default: 100)")
    p.add_argument("--quick", action="store_true",
                   help="small scenario for fast iteration")
    p.add_argument("--json", type=Path, default=None,
                   help="also write machine-readable results to this path")
    args = p.parse_args()

    if args.quick:
        args.rows = 1_000
        args.iterations = 50
        args.concurrency = 25

    print(f"AsyncDB benchmark  rows={args.rows}  iters={args.iterations}  conc={args.concurrency}")
    print("-" * 100)
    results = asyncio.run(bench(args.rows, args.iterations, args.concurrency))
    print("-" * 100)

    if args.json:
        args.json.write_text(json.dumps({
            "config": vars(args) | {"json": str(args.json)},
            "results": results,
        }, indent=2))
        print(f"wrote {args.json}")


if __name__ == "__main__":
    main()
