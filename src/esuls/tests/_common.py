"""
Shared test fixtures and helpers.

Centralises duplication that pylint's R0801 (duplicate-code) flagged:
- `TestItem`: a minimal BaseModel-derived dataclass used by every test
  module that needs an AsyncDB instance.
- `run_test_suite`: the standard `[Test N]` runner used at the bottom of
  every test file's `__main__` block.
"""
import inspect
import sys
from dataclasses import dataclass
from typing import ClassVar

from esuls.db_cli import BaseModel


@dataclass
class TestItem(BaseModel):
    # Suppresses pytest collection warning for a class starting with `Test`.
    # ClassVar so dataclass doesn't mistake it for a field.
    __test__: ClassVar[bool] = False
    name: str = ""
    value: int = 0


async def run_test_suite(
    suite_name: str,
    tests: list[tuple[str, callable]],
    *,
    exit_on_failure: bool = True,
) -> int:
    """Run a list of `(name, no-args callable)` tests and print a summary.

    Each callable is invoked with no arguments. If it returns an awaitable
    (i.e. it's `async def`), it's awaited. Sync callables work too.
    Failures are caught and reported inline; returns the failure count.

    With `exit_on_failure=True` (the default), the function calls
    `sys.exit(1)` when any test fails so a `python -m esuls.tests.test_X`
    invocation propagates non-zero to the shell. Without this, CI runners
    that interpret a 0 exit as "green" would silently miss every
    regression even though "X failed" is right there in the output. The
    caller can pass `exit_on_failure=False` to keep the legacy behaviour
    (return-only) when composing suites.
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

    if failed and exit_on_failure:
        sys.exit(1)
    return failed
