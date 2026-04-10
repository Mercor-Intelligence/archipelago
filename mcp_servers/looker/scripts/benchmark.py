#!/usr/bin/env python3
"""Performance benchmarking for Looker MCP server.

Measures response times for all MCP tools and validates against performance contracts.

Performance Contracts:
- Offline mode: <100ms per tool call
- Health check: <50ms
- Test suite: <5s total

Usage:
    uv run python scripts/benchmark.py
    uv run python scripts/benchmark.py --iterations 100
    uv run python scripts/benchmark.py --verbose
"""

import argparse
import asyncio
import statistics
import sys
import time
import tracemalloc
from dataclasses import dataclass
from pathlib import Path

# Add server to path
sys.path.insert(0, str(Path(__file__).parent.parent / "mcp_servers" / "looker"))

from models import (
    CreateQueryRequest,
    ExploreRequest,
    GetDashboardRequest,
    GetLookRequest,
    HealthCheckRequest,
    ListDashboardsRequest,
    ListFoldersRequest,
    ListLooksRequest,
    LookMLModelRequest,
    QueryFilter,
    RunDashboardRequest,
    RunLookRequest,
    RunQueryByIdRequest,
    RunQueryRequest,
    SearchContentRequest,
)
from tools.content_discovery import (
    get_dashboard,
    get_look,
    list_dashboards,
    list_folders,
    list_looks,
    run_dashboard,
    run_look,
    search_content,
)
from tools.health import health_check
from tools.lookml_discovery import get_explore, list_lookml_models
from tools.query_execution import create_query, run_query_by_id, run_query_inline

# Performance contracts (in milliseconds)
CONTRACTS = {
    "offline_tool_call": 100,  # <100ms per tool call in offline mode
    "health_check": 50,  # <50ms for health check
    "memory_startup_mb": 100,  # <100MB startup memory
    "memory_idle_mb": 100,  # <100MB idle memory
}


@dataclass
class BenchmarkResult:
    """Result from a single benchmark run."""

    name: str
    times_ms: list[float]
    success: bool
    error: str | None = None

    @property
    def mean_ms(self) -> float:
        return statistics.mean(self.times_ms) if self.times_ms else 0

    @property
    def median_ms(self) -> float:
        return statistics.median(self.times_ms) if self.times_ms else 0

    @property
    def min_ms(self) -> float:
        return min(self.times_ms) if self.times_ms else 0

    @property
    def max_ms(self) -> float:
        return max(self.times_ms) if self.times_ms else 0

    @property
    def p95_ms(self) -> float:
        if not self.times_ms:
            return 0
        sorted_times = sorted(self.times_ms)
        idx = int(len(sorted_times) * 0.95)
        return sorted_times[min(idx, len(sorted_times) - 1)]


async def benchmark_tool(name: str, func, iterations: int = 10) -> BenchmarkResult:
    """Benchmark a single tool function."""
    times = []
    error = None

    for _ in range(iterations):
        try:
            start = time.perf_counter()
            await func()
            elapsed = (time.perf_counter() - start) * 1000  # Convert to ms
            times.append(elapsed)
        except Exception as e:
            error = str(e)
            break

    return BenchmarkResult(
        name=name,
        times_ms=times,
        success=len(times) == iterations,
        error=error,
    )


async def run_benchmarks(iterations: int = 10, verbose: bool = False) -> list[BenchmarkResult]:
    """Run all tool benchmarks."""
    results = []

    # Define all tools to benchmark
    # Note: Look IDs start at 101, Query IDs start at 1001 in mock_data.py
    tools = [
        ("health_check", lambda: health_check(HealthCheckRequest())),
        ("list_lookml_models", lambda: list_lookml_models(LookMLModelRequest())),
        (
            "get_explore",
            lambda: get_explore(ExploreRequest(model="ecommerce", explore="order_items")),
        ),
        ("list_folders", lambda: list_folders(ListFoldersRequest())),
        ("list_looks", lambda: list_looks(ListLooksRequest())),
        ("get_look", lambda: get_look(GetLookRequest(look_id=101))),
        ("run_look", lambda: run_look(RunLookRequest(look_id=101))),
        ("list_dashboards", lambda: list_dashboards(ListDashboardsRequest())),
        ("get_dashboard", lambda: get_dashboard(GetDashboardRequest(dashboard_id=1))),
        ("run_dashboard", lambda: run_dashboard(RunDashboardRequest(dashboard_id=1))),
        (
            "search_content",
            lambda: search_content(
                SearchContentRequest(query="revenue", types=["look", "dashboard"])
            ),
        ),
        (
            "create_query",
            lambda: create_query(
                CreateQueryRequest(
                    model="ecommerce",
                    view="order_items",
                    fields=["order_items.status", "order_items.count"],
                )
            ),
        ),
        (
            "run_query_inline",
            lambda: run_query_inline(
                RunQueryRequest(
                    model="ecommerce",
                    view="order_items",
                    fields=["order_items.status", "order_items.count"],
                    filters=[QueryFilter(field="order_items.status", value="complete")],
                    limit=10,
                )
            ),
        ),
        ("run_query_by_id", lambda: run_query_by_id(RunQueryByIdRequest(query_id=1001))),
    ]

    for name, func in tools:
        if verbose:
            print(f"  Benchmarking {name}...", end=" ", flush=True)

        result = await benchmark_tool(name, func, iterations)
        results.append(result)

        if verbose:
            if result.success:
                print(f"{result.mean_ms:.2f}ms (p95: {result.p95_ms:.2f}ms)")
            else:
                print(f"FAILED: {result.error}")

    return results


def measure_memory() -> dict[str, float]:
    """Measure current memory usage."""
    tracemalloc.start()

    # Import and initialize to measure startup memory
    import stores  # noqa: F401

    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    return {
        "current_mb": current / 1024 / 1024,
        "peak_mb": peak / 1024 / 1024,
    }


def print_results(results: list[BenchmarkResult], memory: dict[str, float]) -> bool:
    """Print benchmark results and return True if all contracts met."""
    all_passed = True

    print("\n" + "=" * 70)
    print("BENCHMARK RESULTS")
    print("=" * 70)

    # Tool response times
    print("\nTool Response Times (offline mode):")
    print("-" * 70)
    print(f"{'Tool':<25} {'Mean':>10} {'P95':>10} {'Min':>10} {'Max':>10} {'Status':>10}")
    print("-" * 70)

    for result in results:
        if result.success:
            # Check against contract
            contract = (
                CONTRACTS["health_check"]
                if result.name == "health_check"
                else CONTRACTS["offline_tool_call"]
            )
            passed = result.p95_ms < contract
            status = "PASS" if passed else "FAIL"
            if not passed:
                all_passed = False

            print(
                f"{result.name:<25} "
                f"{result.mean_ms:>8.2f}ms "
                f"{result.p95_ms:>8.2f}ms "
                f"{result.min_ms:>8.2f}ms "
                f"{result.max_ms:>8.2f}ms "
                f"{status:>10}"
            )
        else:
            print(f"{result.name:<25} {'ERROR':>10} - {result.error}")
            all_passed = False

    # Memory usage
    print("\nMemory Usage:")
    print("-" * 70)
    memory_passed = memory["peak_mb"] < CONTRACTS["memory_startup_mb"]
    status = "PASS" if memory_passed else "FAIL"
    if not memory_passed:
        all_passed = False
    contract_mb = CONTRACTS["memory_startup_mb"]
    print(f"Peak memory: {memory['peak_mb']:.2f}MB (contract: <{contract_mb}MB) - {status}")

    # Summary
    print("\n" + "=" * 70)
    print("PERFORMANCE CONTRACTS")
    print("=" * 70)
    print(f"  Offline tool call: <{CONTRACTS['offline_tool_call']}ms (p95)")
    print(f"  Health check: <{CONTRACTS['health_check']}ms (p95)")
    print(f"  Memory startup: <{CONTRACTS['memory_startup_mb']}MB")

    print("\n" + "=" * 70)
    if all_passed:
        print("ALL CONTRACTS MET")
    else:
        print("SOME CONTRACTS FAILED")
    print("=" * 70)

    return all_passed


async def main():
    parser = argparse.ArgumentParser(description="Benchmark Looker MCP server performance")
    parser.add_argument(
        "--iterations",
        "-i",
        type=int,
        default=10,
        help="Number of iterations per tool (default: 10)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show progress during benchmarking",
    )
    args = parser.parse_args()

    print("Looker MCP Server Performance Benchmark")
    print("=" * 70)
    print(f"Iterations per tool: {args.iterations}")

    # Measure memory
    print("\nMeasuring memory usage...")
    memory = measure_memory()

    # Run benchmarks
    print("\nRunning tool benchmarks...")
    results = await run_benchmarks(iterations=args.iterations, verbose=args.verbose)

    # Print results
    all_passed = print_results(results, memory)

    # Exit with appropriate code
    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    asyncio.run(main())
