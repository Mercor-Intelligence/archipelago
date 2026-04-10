#!/usr/bin/env python3
"""Pre-flight check for Looker online mode - verifies auth and setup.

This script performs comprehensive checks BEFORE your 45-minute window to
ensure everything will work correctly.

Usage:
    # Run all checks
    uv run python scripts/preflight_check.py

    # With credentials (tests against live API)
    uv run python scripts/preflight_check.py --live
"""

import argparse
import asyncio
import sys
from pathlib import Path

# Add server to path (go up to repo root, then into mcp_servers/looker)
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "mcp_servers" / "looker"))

# ANSI colors
GREEN = "\033[0;32m"
RED = "\033[0;31m"
YELLOW = "\033[1;33m"
BLUE = "\033[0;34m"
BOLD = "\033[1m"
NC = "\033[0m"


def print_header(text: str):
    """Print section header."""
    print(f"\n{BLUE}{BOLD}{'=' * 60}{NC}")
    print(f"{BLUE}{BOLD}  {text}{NC}")
    print(f"{BLUE}{BOLD}{'=' * 60}{NC}\n")


def print_check(name: str, passed: bool, details: str = ""):
    """Print check result."""
    icon = f"{GREEN}✓{NC}" if passed else f"{RED}✗{NC}"
    print(f"{icon} {name}")
    if details:
        print(f"  {details}")


async def main():
    """Run pre-flight checks."""
    parser = argparse.ArgumentParser(description="Pre-flight check for Looker online mode")
    parser.add_argument("--live", action="store_true", help="Test against live API")
    args = parser.parse_args()

    all_passed = True

    print(f"\n{BOLD}Pre-Flight Check for Looker Online Mode{NC}")
    print("Verifying auth and HTTP setup...\n")

    # =================================================================
    # 1. Import Checks
    # =================================================================
    print_header("1. Import Checks")

    try:
        from auth import LookerAuthService

        print_check("LookerAuthService import", True)
    except ImportError as e:
        print_check("LookerAuthService import", False, str(e))
        all_passed = False

    try:
        from config import settings

        print_check("Config settings import", True)
    except ImportError as e:
        print_check("Config settings import", False, str(e))
        all_passed = False

    try:
        from utils.repository import LiveDataRepository

        print_check("LiveDataRepository import", True)
    except ImportError as e:
        print_check("LiveDataRepository import", False, str(e))
        all_passed = False

    try:
        from repository_factory import create_repository

        print_check("Repository factory import", True)
    except ImportError as e:
        print_check("Repository factory import", False, str(e))
        all_passed = False

    try:
        import httpx

        print_check("httpx library", True, f"Version: {httpx.__version__}")
    except ImportError:
        print_check("httpx library", False, "Not installed")
        all_passed = False

    # =================================================================
    # 2. Auth Service Structure Check
    # =================================================================
    print_header("2. Auth Service Structure")

    from auth import LookerAuthService

    # Verify auth service has required methods
    has_get_token = hasattr(LookerAuthService, "get_access_token")
    print_check("get_access_token method exists", has_get_token)
    if not has_get_token:
        all_passed = False

    has_refresh = hasattr(LookerAuthService, "_refresh_token")
    print_check("_refresh_token method exists", has_refresh)
    if not has_refresh:
        all_passed = False

    # =================================================================
    # 3. Repository Structure Check
    # =================================================================
    print_header("3. Repository Structure")

    from utils.repository import LiveDataRepository

    # Check LiveDataRepository has required methods
    has_get = hasattr(LiveDataRepository, "get")
    print_check("LiveDataRepository.get method", has_get)
    if not has_get:
        all_passed = False

    has_make_request = hasattr(LiveDataRepository, "_make_request_from_config")
    print_check("_make_request_from_config method", has_make_request)
    if not has_make_request:
        all_passed = False

    # =================================================================
    # 4. API Config Check
    # =================================================================
    print_header("4. API Configuration")

    from models import ExploreRequest, LookMLModelRequest

    # Verify API configs are correct
    lookml_config = LookMLModelRequest.get_api_config()
    correct_url = lookml_config.get("url_template") == "/lookml_models"
    print_check(
        "LookMLModelRequest API config",
        correct_url,
        f"URL: {lookml_config.get('url_template')}",
    )
    if not correct_url:
        all_passed = False

    explore_config = ExploreRequest.get_api_config()
    has_placeholders = "{model}" in explore_config.get(
        "url_template", ""
    ) and "{explore}" in explore_config.get("url_template", "")
    print_check(
        "ExploreRequest API config",
        has_placeholders,
        f"URL: {explore_config.get('url_template')}",
    )
    if not has_placeholders:
        all_passed = False

    # =================================================================
    # 5. Mode Auto-Detection Check
    # =================================================================
    print_header("5. Mode Auto-Detection")

    from config import settings

    # Test is_offline_mode() exists
    has_method = hasattr(settings, "is_offline_mode")
    print_check("is_offline_mode() method exists", has_method)
    if not has_method:
        all_passed = False
    else:
        # Show current mode
        current_mode = "offline" if settings.is_offline_mode() else "online"
        has_creds = bool(
            settings.looker_base_url and settings.looker_client_id and settings.looker_client_secret
        )
        print_check(
            "Current mode",
            True,
            f"{current_mode} (credentials: {'configured' if has_creds else 'not configured'})",
        )

    # =================================================================
    # 6. Live API Test (if --live flag)
    # =================================================================
    if args.live:
        print_header("6. Live API Test")

        if not (
            settings.looker_base_url and settings.looker_client_id and settings.looker_client_secret
        ):
            print_check(
                "Live API test",
                False,
                "Credentials not configured. "
                "Run ./scripts/looker_online/setup_credentials.sh first.",
            )
            all_passed = False
        else:
            # Test OAuth flow
            try:
                from auth import LookerAuthService

                auth = LookerAuthService(
                    base_url=settings.looker_base_url,
                    client_id=settings.looker_client_id,
                    client_secret=settings.looker_client_secret,
                    verify_ssl=settings.looker_verify_ssl,
                )

                token = await auth.get_access_token()
                print_check("OAuth token acquisition", True, f"Token: {token[:20]}...")

                # Verify token format
                is_string = isinstance(token, str)
                has_length = len(token) > 10
                print_check("Token validation", is_string and has_length, f"Length: {len(token)}")

                # Test API call
                from models import LookMLModelRequest, LookMLModelResponse
                from repository_factory import create_repository

                repo = create_repository(LookMLModelRequest, LookMLModelResponse)
                result = await repo.get(LookMLModelRequest())

                has_models = result and hasattr(result, "models")
                print_check(
                    "List LookML models API call",
                    has_models,
                    f"Found {len(result.models) if has_models else 0} models",
                )

                if has_models and result.models:
                    print(f"  {BLUE}Sample models:{NC}")
                    for model in result.models[:3]:
                        print(f"    - {model.name} ({len(model.explores or [])} explores)")

            except Exception as e:
                print_check("Live API test", False, str(e))
                all_passed = False

    # =================================================================
    # Summary
    # =================================================================
    print_header("Summary")

    if all_passed:
        print(f"{GREEN}{BOLD}✓ All checks passed!{NC}")
        print(f"\n{GREEN}Your setup is ready for Looker online mode.{NC}")
        if not args.live:
            print(
                f"\n{YELLOW}Tip: Run with --live flag to test "
                f"against actual API once you have credentials.{NC}"
            )
        print()
    else:
        print(f"{RED}{BOLD}✗ Some checks failed{NC}")
        print(f"\n{RED}Please fix the issues above before starting your lab.{NC}\n")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
