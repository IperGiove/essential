"""
Cross-loop tests: verify db_cli and request_cli stay correct across multiple
asyncio.run() calls in the same process. The pre-fix code crashed with
"<asyncio.locks.Lock ...> is bound to a different event loop" the moment a
class-level/module-level asyncio.Lock saw contention in a second loop.

These tests are SYNC by design — they wrap multiple asyncio.run() calls and
inspect the per-loop registry's behaviour from outside any single loop.
"""
import asyncio
import gc
import tempfile
import weakref
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from esuls.db_cli import (
    AsyncDB,
    BaseModel,
    _db_state_by_loop,
    _db_loop_state,
)
from esuls.request_cli import (
    _get_domain_client,
    _req_loop_state,
    close_shared_client,
    make_request,
)


@dataclass
class Item(BaseModel):
    name: str = ""
    value: int = 0


# ─── AsyncDB cross-loop ───────────────────────────────────────────────────

def test_db_cross_loop_under_contention():
    """Two consecutive asyncio.run() with concurrent writes both succeed.

    Pre-fix: the second run failed with `bound to a different event loop`
    on the class-level write Lock the first run had bound. The default
    `skip_errors=True` swallowed the failures silently, dropping writes.
    Post-fix: every loop gets its own write lock from the per-loop registry.
    """
    async def write_burst(path: Path) -> int:
        db = AsyncDB(path, "items", Item)
        try:
            results = await asyncio.gather(*[
                db.save(Item(name=f"x{i}", value=i)) for i in range(20)
            ])
            assert all(results), "save() should not silently fail"
            return await db.count()
        finally:
            await db.close()

    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "x.db"
        n1 = asyncio.run(write_burst(p))
        n2 = asyncio.run(write_burst(p))
        n3 = asyncio.run(write_burst(p))
        assert n1 == 20, f"loop1 should have 20 rows, got {n1}"
        assert n2 == 40, f"loop2 should have 40 rows, got {n2}"
        assert n3 == 60, f"loop3 should have 60 rows, got {n3}"
    print("✓ AsyncDB survives 3 consecutive asyncio.run() under contention")


def test_db_same_instance_across_loops():
    """A single AsyncDB instance can be used across multiple loops.

    The cached connection is bound to its loop, so we have to detect a
    cross-loop reuse and reopen. This test pins one instance and runs
    operations in two different loops on it.
    """
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "shared.db"
        db = AsyncDB(p, "items", Item)

        async def use_in_loop(name: str):
            await asyncio.gather(*[
                db.save(Item(name=f"{name}_{i}", value=i)) for i in range(5)
            ])
            return await db.count()

        n1 = asyncio.run(use_in_loop("a"))
        n2 = asyncio.run(use_in_loop("b"))
        # The third run also exercises the reconnect-on-stale path.
        n3 = asyncio.run(use_in_loop("c"))
        assert n1 == 5 and n2 == 10 and n3 == 15, (n1, n2, n3)
    print("✓ AsyncDB instance reused across asyncio.run() reconnects cleanly")


def test_db_per_loop_state_gced_when_loop_dies():
    """Verify the per-loop registry doesn't leak memory across loops.

    A weak ref to the loop should be empty after asyncio.run() returns and
    GC has run, because the only strong ref to the loop object lives in
    the WeakKeyDictionary's keys.
    """
    captured = {}

    async def grab():
        # Touch the registry so this loop has an entry
        _db_loop_state()
        captured["loop"] = asyncio.get_running_loop()

    asyncio.run(grab())

    # WeakRef to the dead loop
    loop_ref = weakref.ref(captured["loop"])
    del captured["loop"]
    gc.collect()
    assert loop_ref() is None, (
        "loop object leaked — _db_state_by_loop kept a strong reference"
    )
    # And the registry entry is gone
    assert len(_db_state_by_loop) == 0 or all(
        loop_ref() is not loop for loop in _db_state_by_loop.keys()
    )
    print("✓ AsyncDB per-loop state is GC'd when the loop dies")


# ─── request_cli cross-loop ───────────────────────────────────────────────

def test_request_cross_loop_get_domain_client():
    """`_get_domain_client` works across multiple asyncio.run() calls.

    Pre-fix: the second run hit the same _client_lock bound to the dead
    loop. Post-fix: each loop has its own pool + lock.
    """
    async def acquire_with_contention():
        clients = await asyncio.gather(*[
            _get_domain_client("https://example.com") for _ in range(8)
        ])
        # Cache returns the same client within a loop
        assert all(c is clients[0] for c in clients)
        # And it must NOT be the client from a previous loop (the previous
        # loop already closed it via close_shared_client).
        await close_shared_client()

    asyncio.run(acquire_with_contention())
    asyncio.run(acquire_with_contention())
    asyncio.run(acquire_with_contention())
    print("✓ _get_domain_client survives 3 consecutive asyncio.run() under contention")


def test_request_make_request_cross_loop():
    """make_request works across asyncio.run() calls (mocked transport).

    `_run_with_retry` now uses `client.stream(...)` (an async context
    manager that yields a streaming response) rather than `client.request`,
    so the mock impersonates the streaming surface: an async-CM that
    yields a response whose `.aiter_bytes()` produces the body in chunks.
    """

    class _MockStreamResp:
        status_code = 200
        encoding = "utf-8"
        headers: dict = {}
        url = "https://example.com"
        async def aiter_bytes(self):
            yield b"ok"

    class _MockClient:
        is_closed = False
        def stream(self, *args, **kwargs):
            class _CM:
                async def __aenter__(self):
                    return _MockStreamResp()
                async def __aexit__(self, *a):
                    return False
            return _CM()

    def make_mock():
        return _MockClient()

    async def burst():
        with patch(
            "esuls.request_cli._get_domain_client",
            return_value=make_mock(),
        ):
            results = await asyncio.gather(*[
                make_request(
                    "https://example.com",
                    max_attempt=1,
                    exception_sleep=0,
                    jitter=0,
                )
                for _ in range(5)
            ])
            assert all(r is not None and r.status_code == 200 for r in results)

    asyncio.run(burst())
    asyncio.run(burst())
    print("✓ make_request survives consecutive asyncio.run() calls")


def test_request_per_loop_state_isolated():
    """Two separate loops see completely separate registry entries."""
    seen_states = []

    async def capture():
        seen_states.append(id(_req_loop_state()))

    asyncio.run(capture())
    asyncio.run(capture())
    assert seen_states[0] != seen_states[1], (
        "two loops returned the same per-loop state dict — registry is broken"
    )
    print("✓ request_cli per-loop state is isolated between loops")


def test_request_per_loop_state_gced():
    """Verify request_cli's per-loop state is GC'd when its loop dies."""
    captured = {}

    async def grab():
        _req_loop_state()
        captured["loop"] = asyncio.get_running_loop()

    asyncio.run(grab())

    loop_ref = weakref.ref(captured["loop"])
    del captured["loop"]
    gc.collect()
    assert loop_ref() is None, (
        "loop object leaked — _req_state_by_loop kept a strong reference"
    )
    print("✓ request_cli per-loop state is GC'd when the loop dies")


