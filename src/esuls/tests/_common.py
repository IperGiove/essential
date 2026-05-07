"""
Shared test fixtures and helpers.

Centralises duplication that pylint's R0801 (duplicate-code) flagged:
- `TestItem`: a minimal BaseModel-derived dataclass used by every test
  module that needs an AsyncDB instance.
- `run_test_suite`: the standard `[Test N]` runner used at the bottom of
  every test file's `__main__` block.
"""
import inspect
from dataclasses import dataclass

from esuls.db_cli import BaseModel


@dataclass
class TestItem(BaseModel):
    name: str = ""
    value: int = 0


async def run_test_suite(suite_name: str, tests: list[tuple[str, callable]]) -> int:
    """Run a list of `(name, no-args callable)` tests and print a summary.

    Each callable is invoked with no arguments. If it returns an awaitable
    (i.e. it's `async def`), it's awaited. Sync callables work too.
    Failures are caught and reported inline; returns the failure count.
    """
    print("\n" + "=" * 60)
    print(suite_name)
    print("=" * 60)

    passed = failed = 0
    for name, fn in tests:
        try:
            result = fn()
            if inspect.isawaitable(result):
                await result
            passed += 1
        except Exception as e:                              # noqa: BLE001 — runner intentionally swallows
            print(f"  [FAIL] {name}: {type(e).__name__}: {e}")
            failed += 1

    print("\n" + "=" * 60)
    print(f"RESULTS: {passed} passed, {failed} failed")
    if failed == 0:
        print("ALL TESTS PASSED!")
    print("=" * 60)
    return failed
