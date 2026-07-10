"""
Shared concurrency management for grading runners.

Provides semaphore management for controlling verifier execution concurrency
at both global and per-eval levels.
"""

import asyncio

VERIFIER_CONCURRENCY_LIMIT = 15

# Semaphore caches keyed by event loop ID
_global_semaphores: dict[int, asyncio.Semaphore] = {}
_eval_semaphores: dict[tuple[int, str], asyncio.Semaphore] = {}


def _get_global_semaphore() -> asyncio.Semaphore:
    """Get or create the global verifier concurrency semaphore for the current event loop."""
    loop = asyncio.get_running_loop()
    loop_id = id(loop)
    sem = _global_semaphores.get(loop_id)
    if sem is None:
        sem = asyncio.Semaphore(VERIFIER_CONCURRENCY_LIMIT)
        _global_semaphores[loop_id] = sem
    return sem


def _get_eval_semaphore(eval_defn_id: str, max_concurrency: int) -> asyncio.Semaphore:
    """Get or create a semaphore for a specific eval type within the current event loop."""
    loop = asyncio.get_running_loop()
    loop_id = id(loop)
    key = (loop_id, eval_defn_id)
    sem = _eval_semaphores.get(key)
    if sem is None:
        sem = asyncio.Semaphore(max_concurrency)
        _eval_semaphores[key] = sem
    return sem
