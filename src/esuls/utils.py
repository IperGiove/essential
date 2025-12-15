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
    """Run parallel coroutines with semaphore limit"""
    
    semaphore = asyncio.Semaphore(limit)
    
    async def limited_coroutine(coro: Awaitable[T]) -> T:
        async with semaphore:
            return await coro
    
    tasks = [asyncio.create_task(limited_coroutine(coro)) for coro in coroutines]
    
    results = []
    for fut in asyncio.as_completed(tasks):
        results.append(await fut)
    
    return results
