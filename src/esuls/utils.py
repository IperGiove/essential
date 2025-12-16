"""
General utilities - no external dependencies required
"""
import asyncio
from typing import Awaitable, Callable, List, TypeVar

T = TypeVar("T")


async def run_parallel(
      *coroutines: Awaitable[T],
      limit: int = 20
  ) -> List[T]:
      """Run parallel coroutines with semaphore limit, preserving order"""

      semaphore = asyncio.Semaphore(limit)

      async def limited_coroutine(coro: Awaitable[T]) -> T:
          async with semaphore:
              return await coro

      return await asyncio.gather(*[limited_coroutine(coro) for coro in coroutines])

