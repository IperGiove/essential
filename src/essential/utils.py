"""
General utilities - no external dependencies required
"""
import asyncio
from typing import Awaitable, Callable, List, TypeVar

T = TypeVar("T")


async def run_parallel(
    *functions: Callable[[], Awaitable[T]],
    limit: int = 20
) -> List[T]:
    """
    run parallel multiple functions
    """
    semaphore = asyncio.Semaphore(limit)

    async def limited_function(func: Callable[[], Awaitable[T]]) -> T:
        async with semaphore:
            return await func()

    tasks = [asyncio.create_task(limited_function(func)) for func in functions]

    results = []
    for fut in asyncio.as_completed(tasks):
        results.append(await fut)

    return results
