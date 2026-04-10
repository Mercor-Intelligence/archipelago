#!/usr/bin/env python3
"""Capture live data from Looker instance for testing and hybrid mode.

This script connects to a live Looker API instance and captures responses from
all MCP tool endpoints. The captured data can be used to generate realistic mock
data for offline testing.

Usage:
    # Full capture (captures extensive data, ~15 minutes)
    uv run python scripts/capture_live_data.py --output data/capture.json

    # Quick capture (captures minimal data, ~5 minutes)
    uv run python scripts/capture_live_data.py --quick

    # Custom output file
    uv run python scripts/capture_live_data.py --output my_capture.json

    # Capture with anonymization
    uv run python scripts/capture_live_data.py --anonymize
"""

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

# Add server to path (go up to repo root, then into mcp_servers/looker)
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "mcp_servers" / "looker"))

from auth import LookerAuthService
from config import settings
from models import (
    ExploreRequest,
    ExploreResponse,
    GetDashboardRequest,
    GetDashboardResponse,
    GetLookRequest,
    GetLookResponse,
    HealthCheckRequest,
    ListDashboardsRequest,
    ListDashboardsResponse,
    ListFoldersRequest,
    ListFoldersResponse,
    ListLooksRequest,
    ListLooksResponse,
    LookMLModelRequest,
    LookMLModelResponse,
    QueryResult,
    RunQueryByIdRequest,
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


class CaptureProgress:
    """Simple progress tracker without external dependencies."""

    def __init__(self, total: int, description: str):
        self.total = total
        self.current = 0
        self.description = description
        self.start_time = time.time()

    def update(self, n: int = 1):
        """Update progress."""
        self.current += n
        elapsed = time.time() - self.start_time
        pct = (self.current / self.total) * 100 if self.total > 0 else 0
        rate = self.current / elapsed if elapsed > 0 else 0

        bar_width = 40
        filled = int(bar_width * self.current / self.total) if self.total > 0 else 0
        bar = "=" * filled + "-" * (bar_width - filled)

        print(
            f"\r{self.description}: [{bar}] {self.current}/{self.total} "
            f"({pct:.1f}%) {rate:.1f} items/s",
            end="",
            flush=True,
        )

        if self.current >= self.total:
            print()  # New line when complete

    def close(self):
        """Finish progress bar."""
        if self.current < self.total:
            print()


class DataCapture:
    """Capture live Looker data."""

    def __init__(self, quick_mode: bool = False, anonymize: bool = False):
        self.quick_mode = quick_mode
        self.anonymize = anonymize
        self.data: dict[str, Any] = {
            "metadata": {
                "captured_at": datetime.now().isoformat(),
                "mode": "quick" if quick_mode else "full",
                "anonymized": anonymize,
            },
            "endpoints": {},
        }
        self.errors: list[dict[str, str]] = []

    async def capture_health_check(self):
        """Capture health check endpoint."""
        from tools.health import health_check

        try:
            result = await health_check(HealthCheckRequest())
            self.data["endpoints"]["health_check"] = result.model_dump()
        except Exception as e:
            self.errors.append({"endpoint": "health_check", "error": str(e)})

    async def capture_lookml_models(self) -> list[str]:
        """Capture LookML models and return model names."""
        try:
            repo = create_repository(LookMLModelRequest, LookMLModelResponse)
            result = await repo.get(LookMLModelRequest())

            self.data["endpoints"]["lookml_models"] = result.model_dump()

            # Return model names for explore capture
            return [model.name for model in result.models] if result.models else []
        except Exception as e:
            self.errors.append({"endpoint": "lookml_models", "error": str(e)})
            return []

    async def capture_explores(self, model_names: list[str]):
        """Capture explores for all models."""
        explores_data = []

        # Limit explores in quick mode
        max_models = 2 if self.quick_mode else len(model_names)

        progress = CaptureProgress(max_models, "Capturing explores")

        for model_name in model_names[:max_models]:
            try:
                # Get model to find explores
                repo = create_repository(LookMLModelRequest, LookMLModelResponse)
                models_result = await repo.get(LookMLModelRequest())

                # Find this model
                model = next((m for m in models_result.models if m.name == model_name), None)
                if not model or not model.explores:
                    progress.update(1)
                    continue

                # Capture explores (limit in quick mode)
                max_explores = 2 if self.quick_mode else len(model.explores)
                for explore in model.explores[:max_explores]:
                    try:
                        explore_repo = create_repository(ExploreRequest, ExploreResponse)
                        explore_result = await explore_repo.get(
                            ExploreRequest(model=model_name, explore=explore.name)
                        )

                        explores_data.append(
                            {
                                "model": model_name,
                                "explore": explore.name,
                                "data": explore_result.model_dump(),
                            }
                        )
                    except Exception as e:
                        self.errors.append(
                            {
                                "endpoint": f"explore:{model_name}:{explore.name}",
                                "error": str(e),
                            }
                        )

                progress.update(1)

            except Exception as e:
                self.errors.append({"endpoint": f"model:{model_name}", "error": str(e)})
                progress.update(1)

        progress.close()
        self.data["endpoints"]["explores"] = explores_data

    async def capture_folders(self) -> list[str]:
        """Capture folders and return folder IDs."""
        try:
            repo = create_repository(ListFoldersRequest, ListFoldersResponse)
            result = await repo.get(ListFoldersRequest())

            self.data["endpoints"]["folders"] = result.model_dump()

            # Return folder IDs for looks filtering
            return [folder.id for folder in result.folders] if result.folders else []
        except Exception as e:
            self.errors.append({"endpoint": "folders", "error": str(e)})
            return []

    async def capture_looks(self, folder_ids: list[str]):
        """Capture looks (all and by folder)."""
        looks_data = []

        # Capture all looks
        try:
            repo = create_repository(ListLooksRequest, ListLooksResponse)
            all_looks = await repo.get(ListLooksRequest())
            looks_data.append({"filter": "all", "data": all_looks.model_dump()})

            # Get look IDs for detailed capture
            look_ids = [look.id for look in all_looks.looks] if all_looks.looks else []

        except Exception as e:
            self.errors.append({"endpoint": "looks:all", "error": str(e)})
            look_ids = []

        # Capture looks by folder (limit in quick mode)
        max_folders = 2 if self.quick_mode else len(folder_ids)
        progress = CaptureProgress(max_folders, "Capturing looks by folder")

        for folder_id in folder_ids[:max_folders]:
            try:
                repo = create_repository(ListLooksRequest, ListLooksResponse)
                result = await repo.get(ListLooksRequest(folder_id=folder_id))
                looks_data.append({"filter": f"folder:{folder_id}", "data": result.model_dump()})
            except Exception as e:
                self.errors.append({"endpoint": f"looks:folder:{folder_id}", "error": str(e)})

            progress.update(1)

        progress.close()

        # Capture individual looks (limit in quick mode)
        max_looks = 3 if self.quick_mode else min(10, len(look_ids))
        if max_looks > 0:
            progress = CaptureProgress(max_looks, "Capturing look details")

            for look_id in look_ids[:max_looks]:
                try:
                    repo = create_repository(GetLookRequest, GetLookResponse)
                    result = await repo.get(GetLookRequest(look_id=look_id))
                    looks_data.append({"filter": f"look:{look_id}", "data": result.model_dump()})
                except Exception as e:
                    self.errors.append({"endpoint": f"look:{look_id}", "error": str(e)})

                progress.update(1)

            progress.close()

        self.data["endpoints"]["looks"] = looks_data

    async def capture_dashboards(self):
        """Capture dashboards."""
        dashboards_data = []

        # List all dashboards
        try:
            repo = create_repository(ListDashboardsRequest, ListDashboardsResponse)
            all_dashboards = await repo.get(ListDashboardsRequest())
            dashboards_data.append({"filter": "all", "data": all_dashboards.model_dump()})

            # Get dashboard IDs for detailed capture
            dashboard_ids = (
                [d.id for d in all_dashboards.dashboards] if all_dashboards.dashboards else []
            )

        except Exception as e:
            self.errors.append({"endpoint": "dashboards:all", "error": str(e)})
            dashboard_ids = []

        # Capture individual dashboards (limit in quick mode)
        max_dashboards = 3 if self.quick_mode else min(10, len(dashboard_ids))
        if max_dashboards > 0:
            progress = CaptureProgress(max_dashboards, "Capturing dashboard details")

            for dashboard_id in dashboard_ids[:max_dashboards]:
                try:
                    repo = create_repository(GetDashboardRequest, GetDashboardResponse)
                    result = await repo.get(GetDashboardRequest(dashboard_id=dashboard_id))
                    dashboards_data.append(
                        {"filter": f"dashboard:{dashboard_id}", "data": result.model_dump()}
                    )
                except Exception as e:
                    self.errors.append({"endpoint": f"dashboard:{dashboard_id}", "error": str(e)})

                progress.update(1)

            progress.close()

        self.data["endpoints"]["dashboards"] = dashboards_data

    async def capture_search(self):
        """Capture search content."""
        search_data = []

        # Empty search (returns all)
        try:
            repo = create_repository(SearchContentRequest, SearchContentResponse)
            result = await repo.get(SearchContentRequest(query=""))
            search_data.append({"query": "", "data": result.model_dump()})
        except Exception as e:
            self.errors.append({"endpoint": "search:empty", "error": str(e)})

        # Sample searches
        sample_queries = ["revenue", "users"] if self.quick_mode else ["revenue", "users", "order"]

        progress = CaptureProgress(len(sample_queries), "Capturing search queries")

        for query in sample_queries:
            try:
                repo = create_repository(SearchContentRequest, SearchContentResponse)
                result = await repo.get(SearchContentRequest(query=query))
                search_data.append({"query": query, "data": result.model_dump()})
            except Exception as e:
                self.errors.append({"endpoint": f"search:{query}", "error": str(e)})

            progress.update(1)

        progress.close()
        self.data["endpoints"]["search"] = search_data

    async def capture_queries(self):
        """Capture query execution."""
        queries_data = []

        # Get some looks to find query IDs
        try:
            repo = create_repository(ListLooksRequest, ListLooksResponse)
            looks = await repo.get(ListLooksRequest())

            if looks.looks:
                # Limit queries in quick mode
                max_looks = 2 if self.quick_mode else min(5, len(looks.looks))

                # Count looks with query_ids to set accurate progress total
                looks_with_queries = [look for look in looks.looks[:max_looks] if look.query_id]

                if looks_with_queries:
                    progress = CaptureProgress(len(looks_with_queries), "Capturing query results")

                    for look in looks_with_queries:
                        try:
                            query_repo = create_repository(RunQueryByIdRequest, QueryResult)
                            result = await query_repo.get(
                                RunQueryByIdRequest(query_id=look.query_id)
                            )
                            queries_data.append(
                                {"query_id": look.query_id, "data": result.model_dump()}
                            )
                        except Exception as e:
                            self.errors.append(
                                {"endpoint": f"query:{look.query_id}", "error": str(e)}
                            )

                        progress.update(1)

                    progress.close()

        except Exception as e:
            self.errors.append({"endpoint": "queries", "error": str(e)})

        self.data["endpoints"]["queries"] = queries_data

    def anonymize_data(self):
        """Anonymize sensitive data in captured responses."""
        # This is a placeholder - implement based on your data sensitivity
        if not self.anonymize:
            return

        # Example anonymizations (add more as needed):
        # - Replace user emails with fake emails
        # - Replace user names with "User 1", "User 2", etc.
        # - Redact sensitive field values

        print(f"{YELLOW}Note: Anonymization not yet implemented{NC}")

    def save(self, output_path: Path):
        """Save captured data to JSON file."""
        # Anonymize if requested
        self.anonymize_data()

        # Create output directory
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Save to file
        with open(output_path, "w") as f:
            json.dump(self.data, f, indent=2)

        print(f"{GREEN}Saved capture to: {output_path}{NC}")
        print(f"  Endpoints captured: {len(self.data['endpoints'])}")
        print(f"  File size: {output_path.stat().st_size / 1024:.1f} KB")

        if self.errors:
            print(f"\n{YELLOW}Errors encountered: {len(self.errors)}{NC}")
            for error in self.errors[:5]:  # Show first 5 errors
                print(f"  - {error['endpoint']}: {error['error']}")
            if len(self.errors) > 5:
                print(f"  ... and {len(self.errors) - 5} more")


async def main():
    """Main capture workflow."""
    parser = argparse.ArgumentParser(description="Capture live Looker data")
    parser.add_argument(
        "--output",
        type=str,
        default="data/looker/captured/live_capture.json",
        help="Output JSON file path",
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Quick mode (captures less data, ~5 minutes)",
    )
    parser.add_argument(
        "--anonymize",
        action="store_true",
        help="Anonymize sensitive data in output",
    )
    args = parser.parse_args()

    print(f"\n{BLUE}{BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{NC}")
    print(f"{BLUE}{BOLD}   📦 Live Data Capture{NC}")
    print(f"{BLUE}{BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{NC}\n")

    mode = "quick" if args.quick else "full"
    print(f"{BOLD}Mode: {mode}{NC}")
    print(f"{BOLD}Output: {args.output}{NC}")
    print(f"{BOLD}Anonymize: {args.anonymize}{NC}\n")

    # Validate environment
    if not settings.looker_base_url:
        print(f"{RED}Error: LOOKER_BASE_URL not set in .env{NC}")
        print(f"{YELLOW}Run './scripts/looker_online/setup_credentials.sh' to configure{NC}\n")
        sys.exit(1)
    if not settings.looker_client_id:
        print(f"{RED}Error: LOOKER_CLIENT_ID not set in .env{NC}")
        print(f"{YELLOW}Run './scripts/looker_online/setup_credentials.sh' to configure{NC}\n")
        sys.exit(1)
    if not settings.looker_client_secret:
        print(f"{RED}Error: LOOKER_CLIENT_SECRET not set in .env{NC}")
        print(f"{YELLOW}Run './scripts/looker_online/setup_credentials.sh' to configure{NC}\n")
        sys.exit(1)

    # Test OAuth
    print(f"{BOLD}Testing OAuth connection...{NC}")
    try:
        auth = LookerAuthService(
            base_url=settings.looker_base_url,
            client_id=settings.looker_client_id,
            client_secret=settings.looker_client_secret,
            verify_ssl=settings.looker_verify_ssl,
        )
        await auth.get_access_token()
        print(f"  {GREEN}Connected to {settings.looker_base_url}{NC}\n")
    except Exception as e:
        print(f"  {RED}OAuth failed: {e}{NC}\n")
        sys.exit(1)

    # Start capture
    start_time = time.time()
    capture = DataCapture(quick_mode=args.quick, anonymize=args.anonymize)

    print(f"{BOLD}Capturing data from live API...{NC}\n")

    # Capture all endpoints
    print(f"{BLUE}1. Health check{NC}")
    await capture.capture_health_check()

    print(f"{BLUE}2. LookML models{NC}")
    model_names = await capture.capture_lookml_models()

    print(f"{BLUE}3. Explores{NC}")
    await capture.capture_explores(model_names)

    print(f"{BLUE}4. Folders{NC}")
    folder_ids = await capture.capture_folders()

    print(f"{BLUE}5. Looks{NC}")
    await capture.capture_looks(folder_ids)

    print(f"{BLUE}6. Dashboards{NC}")
    await capture.capture_dashboards()

    print(f"{BLUE}7. Search{NC}")
    await capture.capture_search()

    print(f"{BLUE}8. Query results{NC}")
    await capture.capture_queries()

    # Save results
    duration = time.time() - start_time
    print(f"\n{BOLD}Capture complete in {duration:.1f}s{NC}\n")

    output_path = Path(args.output)
    capture.save(output_path)

    print(f"\n{GREEN}{BOLD}Success!{NC} Data captured and saved.\n")

    if args.quick:
        print(f"{YELLOW}Note: Quick mode captured limited data.{NC}")
        print("For full capture, run without --quick flag.\n")


if __name__ == "__main__":
    asyncio.run(main())
