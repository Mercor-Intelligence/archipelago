#!/usr/bin/env python3
"""Automated validation script for Looker online mode testing.

This script provides quick validation of all MCP tools against a live Looker
instance, with timing information and a comprehensive test report.

Usage:
    # Run full validation
    uv run python scripts/looker_online/validate_connection.py

    # Skip slow tests
    uv run python scripts/looker_online/validate_connection.py --quick

    # Auto-capture data after validation
    uv run python scripts/looker_online/validate_connection.py --capture
"""

import argparse
import asyncio
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

# Add server to path (go up to repo root, then into mcp_servers/looker)
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "mcp_servers" / "looker"))

from auth import LookerAuthService
from config import settings
from models import (
    ExploreRequest,
    ExploreResponse,
    HealthCheckRequest,
    ListDashboardsRequest,
    ListDashboardsResponse,
    ListFoldersRequest,
    ListFoldersResponse,
    ListLooksRequest,
    ListLooksResponse,
    LookMLModelRequest,
    LookMLModelResponse,
    SearchContentRequest,
    SearchContentResponse,
)
from repository_factory import create_repository

# ANSI color codes
GREEN = "\033[0;32m"
RED = "\033[0;31m"
YELLOW = "\033[1;33m"
BLUE = "\033[0;34m"
BOLD = "\033[1m"
NC = "\033[0m"  # No Color


class TestResult:
    """Result of a single test."""

    def __init__(self, name: str, success: bool, duration: float, error: str | None = None):
        self.name = name
        self.success = success
        self.duration = duration
        self.error = error


class ValidationReport:
    """Comprehensive validation report."""

    def __init__(self):
        self.results: list[TestResult] = []
        self.start_time = time.time()
        self.oauth_token: str | None = None

    def add_result(self, result: TestResult):
        """Add a test result."""
        self.results.append(result)

    @property
    def total_duration(self) -> float:
        """Total validation duration."""
        return time.time() - self.start_time

    @property
    def passed_count(self) -> int:
        """Number of passed tests."""
        return sum(1 for r in self.results if r.success)

    @property
    def failed_count(self) -> int:
        """Number of failed tests."""
        return sum(1 for r in self.results if not r.success)

    @property
    def success_rate(self) -> float:
        """Success rate as percentage."""
        if not self.results:
            return 0.0
        return (self.passed_count / len(self.results)) * 100

    def print_summary(self, lab_start_time: datetime | None = None):
        """Print comprehensive test summary."""
        print(f"\n{BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{NC}\n")
        print(f"{BOLD}📊 Validation Summary{NC}\n")

        # Overall results
        if self.passed_count == len(self.results):
            status_icon = f"{GREEN}✅"
            status_text = "ALL TESTS PASSED"
        elif self.failed_count == 0:
            status_icon = f"{YELLOW}⚠️"
            status_text = "NO TESTS RUN"
        else:
            status_icon = f"{RED}❌"
            status_text = "SOME TESTS FAILED"

        print(f"{status_icon} {BOLD}{status_text}{NC}")
        print(f"   {self.passed_count}/{len(self.results)} tests passed ({self.success_rate:.1f}%)")
        print(f"   Total time: {self.total_duration:.1f}s\n")

        # Test details
        if self.results:
            print(f"{BOLD}Test Results:{NC}\n")
            for result in self.results:
                icon = f"{GREEN}✅{NC}" if result.success else f"{RED}❌{NC}"
                print(f"  {icon} {result.name:<40} ({result.duration:.2f}s)")
                if result.error:
                    print(f"      {RED}Error: {result.error}{NC}")

        # Time remaining estimate
        if lab_start_time:
            elapsed = datetime.now() - lab_start_time
            remaining = timedelta(minutes=45) - elapsed
            if remaining.total_seconds() > 0:
                mins = int(remaining.total_seconds() // 60)
                secs = int(remaining.total_seconds() % 60)
                print(f"\n{YELLOW}⏱️  Estimated lab time remaining: ~{mins}m {secs}s{NC}")
            else:
                print(f"\n{RED}⏱️  Lab time may have expired!{NC}")

        print(f"\n{BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{NC}\n")


async def run_test(name: str, test_func) -> TestResult:
    """Run a single test with timing."""
    start = time.time()
    try:
        await test_func()
        duration = time.time() - start
        print(f"  {GREEN}✅{NC} {name} ({duration:.2f}s)")
        return TestResult(name, True, duration)
    except Exception as e:
        duration = time.time() - start
        error_msg = str(e)[:100]  # Truncate long errors
        print(f"  {RED}❌{NC} {name} ({duration:.2f}s)")
        print(f"      {RED}Error: {error_msg}{NC}")
        return TestResult(name, False, duration, error_msg)


async def validate_environment() -> tuple[bool, str | None]:
    """Validate environment configuration."""
    if not settings.looker_base_url:
        return False, "LOOKER_BASE_URL not set in .env"
    if not settings.looker_client_id:
        return False, "LOOKER_CLIENT_ID not set in .env"
    if not settings.looker_client_secret:
        return False, "LOOKER_CLIENT_SECRET not set in .env"
    return True, None


async def test_oauth() -> str:
    """Test OAuth authentication."""
    auth = LookerAuthService(
        base_url=settings.looker_base_url,
        client_id=settings.looker_client_id,
        client_secret=settings.looker_client_secret,
        verify_ssl=settings.looker_verify_ssl,
    )
    token = await auth.get_access_token()
    return token


async def test_health_check():
    """Test health check tool."""
    from tools.health import health_check

    result = await health_check(HealthCheckRequest())
    assert result.status == "ok"
    assert result.mode == "online"


async def test_list_models():
    """Test list_lookml_models tool."""
    repo = create_repository(LookMLModelRequest, LookMLModelResponse)
    result = await repo.get(LookMLModelRequest())
    assert result.models is not None


async def test_get_explore():
    """Test get_explore tool."""
    # First get models to find a valid one
    repo = create_repository(LookMLModelRequest, LookMLModelResponse)
    models = await repo.get(LookMLModelRequest())
    if not models.models:
        raise ValueError("No models found")

    # Get first explore from first model
    model_name = models.models[0].name
    if not models.models[0].explores:
        raise ValueError(f"No explores in model {model_name}")

    explore_name = models.models[0].explores[0].name
    repo = create_repository(ExploreRequest, ExploreResponse)
    result = await repo.get(ExploreRequest(model=model_name, explore=explore_name))
    # ExploreResponse has dimensions, measures, and joins (lists with default_factory)
    # Check that at least one list is non-empty
    assert len(result.dimensions) > 0 or len(result.measures) > 0


async def test_list_folders():
    """Test list_folders tool."""
    repo = create_repository(ListFoldersRequest, ListFoldersResponse)
    result = await repo.get(ListFoldersRequest())
    assert result.folders is not None


async def test_list_looks():
    """Test list_looks tool."""
    repo = create_repository(ListLooksRequest, ListLooksResponse)
    result = await repo.get(ListLooksRequest())
    assert result.looks is not None


async def test_search_content():
    """Test search_content tool."""
    repo = create_repository(SearchContentRequest, SearchContentResponse)
    result = await repo.get(SearchContentRequest(query=""))
    assert result.results is not None


async def test_list_dashboards():
    """Test list_dashboards tool."""
    repo = create_repository(ListDashboardsRequest, ListDashboardsResponse)
    result = await repo.get(ListDashboardsRequest())
    assert result.dashboards is not None


async def main():
    """Main validation workflow."""
    parser = argparse.ArgumentParser(description="Validate Looker online mode setup")
    parser.add_argument("--quick", action="store_true", help="Skip slow tests")
    parser.add_argument("--capture", action="store_true", help="Auto-capture data after validation")
    parser.add_argument(
        "--lab-start-time",
        type=str,
        help="Lab start time (ISO format) for time remaining calculation",
    )
    args = parser.parse_args()

    # Parse lab start time if provided
    lab_start_time = None
    if args.lab_start_time:
        try:
            lab_start_time = datetime.fromisoformat(args.lab_start_time)
        except ValueError:
            print(f"{RED}Warning: Invalid lab start time format{NC}")

    report = ValidationReport()

    print(f"\n{BLUE}{BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{NC}")
    print(f"{BLUE}{BOLD}   🧪 Looker Online Mode Validation{NC}")
    print(f"{BLUE}{BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{NC}\n")

    # Validate environment
    print(f"{BOLD}1. Validating environment configuration...{NC}\n")
    valid, error = await validate_environment()
    if not valid:
        print(f"  {RED}❌ Environment validation failed: {error}{NC}\n")
        print(
            f"{YELLOW}Hint: Run './scripts/looker_online/setup_credentials.sh' "
            f"to configure your .env file{NC}\n"
        )
        sys.exit(1)

    print(f"  {GREEN}✅{NC} LOOKER_BASE_URL: {settings.looker_base_url}")
    print(f"  {GREEN}✅{NC} LOOKER_CLIENT_ID: {settings.looker_client_id[:10]}...")
    print(f"  {GREEN}✅{NC} LOOKER_CLIENT_SECRET: ***")
    print(f"  {GREEN}✅{NC} Mode: {settings.is_offline_mode() and 'offline' or 'online'}\n")

    # Test OAuth
    print(f"{BOLD}2. Testing OAuth authentication...{NC}\n")
    result = await run_test("OAuth connection", test_oauth)
    report.add_result(result)
    if not result.success:
        print(f"\n{RED}❌ OAuth failed - cannot continue with tool testing{NC}\n")
        report.print_summary(lab_start_time)
        sys.exit(1)

    print()

    # Test all MCP tools
    print(f"{BOLD}3. Testing MCP tools against live API...{NC}\n")

    tests = [
        ("health_check", test_health_check),
        ("list_lookml_models", test_list_models),
        ("get_explore", test_get_explore),
        ("list_folders", test_list_folders),
        ("list_looks", test_list_looks),
        ("search_content", test_search_content),
        ("list_dashboards", test_list_dashboards),
    ]

    for name, test_func in tests:
        result = await run_test(name, test_func)
        report.add_result(result)

    # Print summary
    report.print_summary(lab_start_time)

    # Auto-capture if requested and all tests passed
    if args.capture and report.failed_count == 0:
        print(f"{BOLD}Running data capture...{NC}\n")
        from subprocess import run

        # Build capture command based on mode
        # Use absolute path to script for cross-platform compatibility
        script_path = Path(__file__).parent / "capture_data.py"
        repo_root = Path(__file__).parent.parent.parent

        capture_cmd = ["uv", "run", "python", str(script_path.relative_to(repo_root))]
        if args.quick:
            capture_cmd.append("--quick")
        else:
            capture_cmd.extend(["--output", "data/looker/captured/validation_capture.json"])

        run(capture_cmd, cwd=repo_root)

    sys.exit(0 if report.failed_count == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
