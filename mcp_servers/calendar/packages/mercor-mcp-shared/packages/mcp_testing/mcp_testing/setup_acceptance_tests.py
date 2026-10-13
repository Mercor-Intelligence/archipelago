"""Interactive setup wizard for acceptance testing.

Provides a user-friendly, guided workflow for setting up acceptance testing
without requiring command-line arguments.

Usage:
    python -m mcp_testing.setup_acceptance_tests
"""

import asyncio
import sys
from getpass import getpass
from pathlib import Path

# Standard project structure - edit these constants if your project differs
FIXTURES_DIR = "fixtures"
TESTS_DIR = "tests"
TOOL_NAME = "api_tool"


def interactive_setup() -> int:
    """Guide user through acceptance testing setup interactively.

    Returns:
        Exit code (0 for success, 1 for error)
    """
    print("MCP Acceptance Testing Setup")
    print("=" * 50)
    print()

    try:
        # Collect inputs with validation
        api_url = input("API base URL (e.g., https://api.example.com/v1): ").strip()
        if not api_url:
            print("Error: API URL is required")
            return 1
        if not api_url.startswith(("http://", "https://")):
            print("Error: API URL must start with http:// or https://")
            return 1

        token_env = input("Token environment variable name (e.g., API_TOKEN): ").strip().upper()
        if not token_env:
            print("Error: Token environment variable name is required")
            return 1
        if not token_env.replace("_", "").isalnum():
            print("Error: Environment variable name must be alphanumeric (underscores allowed)")
            return 1

        token_value = getpass("API token (input hidden): ").strip()
        if not token_value:
            print("Error: API token is required")
            return 1

        endpoints = input("Endpoints (comma-separated, e.g., /users,/posts): ").strip()
        if not endpoints:
            print("Error: At least one endpoint is required")
            return 1

        # Optional: methods
        print()
        print("Optional: Specify HTTP methods per endpoint")
        print("Format: /endpoint:METHOD1,METHOD2 (e.g., /users:GET,POST)")
        print("Press Enter to skip (defaults to GET for all endpoints)")
        methods = input("Methods (optional): ").strip()

        print()
        print("=" * 50)
        print("All inputs collected")
        print()

        # Create/update .env file
        env_path = Path(".env")
        env_exists = env_path.exists()

        # Check if token already exists in .env (line-by-line to avoid substring matches)
        existing_env_lines = []
        token_exists = False
        if env_exists:
            existing_env_content = env_path.read_text()
            existing_env_lines = existing_env_content.splitlines()
            # Check each line for exact match (not substring)
            for line in existing_env_lines:
                # Strip whitespace and check if line starts with token_env=
                if line.strip().startswith(f"{token_env}="):
                    token_exists = True
                    break

            if token_exists:
                overwrite = (
                    input(f"WARNING: {token_env} already exists in .env. Overwrite? (y/N): ")
                    .strip()
                    .lower()
                )
                if overwrite != "y":
                    print(f"Using existing {token_env} from .env")
                    # Don't write token
                    token_value = None

        if token_value:
            if token_exists and env_exists:
                # Replace existing line
                new_lines = []
                for line in existing_env_lines:
                    if line.strip().startswith(f"{token_env}="):
                        new_lines.append(f"{token_env}={token_value}")
                    else:
                        new_lines.append(line)
                with open(env_path, "w") as f:
                    f.write("\n".join(new_lines))
                    if new_lines and existing_env_content.endswith("\n"):
                        f.write("\n")
                print(f"Updated {token_env} in .env")
            else:
                # Append new token
                with open(env_path, "a") as f:
                    # Add newline before if file exists and doesn't end with newline
                    if (
                        env_exists
                        and existing_env_lines
                        and not existing_env_content.endswith("\n")
                    ):
                        f.write("\n")
                    f.write(f"{token_env}={token_value}\n")
                print(f"Added {token_env} to .env")

        # Ensure .gitignore includes .env (check as complete line, not substring)
        gitignore_path = Path(".gitignore")
        if gitignore_path.exists():
            gitignore_content = gitignore_path.read_text()
            # Check each line to see if .env is already ignored
            # Strip whitespace and ignore comments
            gitignore_lines = [
                line.strip().split("#")[0].strip() for line in gitignore_content.splitlines()
            ]
            if ".env" not in gitignore_lines:
                with open(gitignore_path, "a") as f:
                    if gitignore_content and not gitignore_content.endswith("\n"):
                        f.write("\n")
                    f.write(".env\n")
                print("Added .env to .gitignore")
        else:
            with open(gitignore_path, "w") as f:
                f.write(".env\n")
            print("Created .gitignore with .env")

        print()
        print("Generating fixtures and tests...")
        print("=" * 50)
        print()

        # Prepare arguments for auto_testing
        sys.argv = [
            "auto_testing",
            "--api-url",
            api_url,
            "--token-env",
            token_env,
            "--endpoints",
            endpoints,
            "--fixtures-dir",
            FIXTURES_DIR,
            "--tests-dir",
            TESTS_DIR,
            "--tool-name",
            TOOL_NAME,
        ]

        if methods:
            sys.argv.extend(["--methods", methods])

        # Import and run auto_testing
        from mcp_testing.auto_testing import main as auto_testing_main

        # Run the async main function
        exit_code = asyncio.run(auto_testing_main())

        if exit_code == 0:
            test_file = f"{TESTS_DIR}/test_{TOOL_NAME}_acceptance.py"
            print()
            print("=" * 50)
            print("Setup complete!")
            print()
            print("Next steps:")
            print(f"1. Edit {test_file}")
            print("2. Import your tools and map them to endpoints in the router")
            print("   (See TODOs in the generated file - clear instructions provided)")
            print(f"3. Run tests: uv run pytest {test_file} -v")
            print("4. Implement your tools until all tests pass")
            print("=" * 50)

        return exit_code

    except KeyboardInterrupt:
        print()
        print("Setup cancelled by user")
        return 1
    except Exception as e:
        print(f"Error: {e}")
        return 1


def main() -> None:
    """Entry point for the interactive setup."""
    sys.exit(interactive_setup())


if __name__ == "__main__":
    main()
