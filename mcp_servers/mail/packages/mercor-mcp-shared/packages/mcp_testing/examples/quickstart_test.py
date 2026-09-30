"""Quickstart: Test the framework works in practice.

This is a simple, working example you can run immediately.
"""

import asyncio
import sys
from tempfile import TemporaryDirectory

from mcp_testing import FixtureGenerator, HTTPClient, LiveAPIComparator, MockAPIClient


async def test_1_mocking():
    """Test 1: Mock API Client (no network calls)"""
    print("\n" + "=" * 60)
    print("TEST 1: Mock API Client")
    print("=" * 60)

    # Setup mock
    mock_client = MockAPIClient()
    mock_client.add_response(
        "GET", "/users", status=200, data={"users": [{"id": 1, "name": "Alice"}]}
    )

    # Make request
    status, data = await mock_client.request("GET", "/users")

    print(f"Status: {status}")
    print(f"Data: {data}")
    print("Result: PASS" if status == 200 else "Result: FAIL")

    return status == 200


async def test_2_fixture_generation():
    """Test 2: Generate fixtures from API"""
    print("\n" + "=" * 60)
    print("TEST 2: Fixture Generation")
    print("=" * 60)

    with TemporaryDirectory() as tmpdir:
        # Use JSONPlaceholder (free public API)
        generator = FixtureGenerator(
            http_client=HTTPClient(base_url="https://jsonplaceholder.typicode.com"),
            output_dir=tmpdir,
        )

        # Capture a real API response
        fixture_path = await generator.capture_response(
            name="Get post by ID", endpoint="/posts/1", method="GET"
        )

        print(f"Fixture saved to: {fixture_path.name}")
        print(f"File exists: {fixture_path.exists()}")
        print("Result: PASS" if fixture_path.exists() else "Result: FAIL")

        # Show fixture content (first 200 chars)
        content = fixture_path.read_text()[:200]
        print(f"\nFixture preview: {content}...")

        return fixture_path.exists()


async def test_3_live_comparison():
    """Test 3: Compare mock vs live API"""
    print("\n" + "=" * 60)
    print("TEST 3: Live API Comparison")
    print("=" * 60)

    # Mock MCP tool
    async def my_mcp_tool(request):
        # Simulates your MCP server returning data
        return {
            "status_code": 200,
            "data": {
                "userId": 1,
                "id": 1,
                "title": "sunt aut facere repellat provident occaecati excepturi optio reprehenderit",  # noqa: E501
                "body": "quia et suscipit\nsuscipit recusandae consequuntur expedita et cum\nreprehenderit molestiae ut ut quas totam\nnostrum rerum est autem sunt rem eveniet architecto",  # noqa: E501
            },
        }

    # Setup comparator with real API
    comparator = LiveAPIComparator(
        mock_tool=my_mcp_tool,
        http_client=HTTPClient(base_url="https://jsonplaceholder.typicode.com"),
        ignore_fields=["id"],  # Ignore auto-generated IDs
    )

    # Compare
    def request_factory(method, endpoint, request_data):
        return {"endpoint": endpoint, "method": method}

    result = await comparator.compare_endpoint(
        endpoint="/posts/1", method="GET", request_data={}, mock_request_factory=request_factory
    )

    print(f"Comparison passed: {result.passed}")
    print(f"Differences found: {len(result.differences)}")
    if result.differences:
        for diff in result.differences[:3]:
            print(f"  - {diff.path}: expected {diff.expected}, got {diff.actual}")
    print("Result: PASS" if result.passed else "Result: FAIL")

    return result.passed


async def test_4_batch_fixture_capture():
    """Test 4: Batch capture multiple fixtures"""
    print("\n" + "=" * 60)
    print("TEST 4: Batch Fixture Capture")
    print("=" * 60)

    with TemporaryDirectory() as tmpdir:
        generator = FixtureGenerator(
            http_client=HTTPClient(base_url="https://jsonplaceholder.typicode.com"),
            output_dir=tmpdir,
        )

        # Capture multiple test cases
        paths = await generator.capture_batch(
            [
                {"name": "Get post 1", "endpoint": "/posts/1"},
                {"name": "Get post 2", "endpoint": "/posts/2"},
                {"name": "List users", "endpoint": "/users"},
            ]
        )

        print(f"Captured {len(paths)} fixtures:")
        for path in paths:
            print(f"  - {path.name}")

        print("Result: PASS" if len(paths) == 3 else "Result: FAIL")

        return len(paths) == 3


async def main():
    """Run all tests."""
    print("=" * 60)
    print("MCP Testing Framework - Practical Tests")
    print("=" * 60)
    print("\nRunning 4 practical tests to verify the framework...\n")

    results = []

    try:
        results.append(await test_1_mocking())
    except Exception as e:
        print(f"Test 1 FAILED with error: {e}", file=sys.stderr)
        results.append(False)

    try:
        results.append(await test_2_fixture_generation())
    except Exception as e:
        print(f"Test 2 FAILED with error: {e}", file=sys.stderr)
        results.append(False)

    try:
        results.append(await test_3_live_comparison())
    except Exception as e:
        print(f"Test 3 FAILED with error: {e}", file=sys.stderr)
        results.append(False)

    try:
        results.append(await test_4_batch_fixture_capture())
    except Exception as e:
        print(f"Test 4 FAILED with error: {e}", file=sys.stderr)
        results.append(False)

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    passed = sum(results)
    total = len(results)
    print(f"Tests passed: {passed}/{total}")

    if passed == total:
        print("\nALL TESTS PASSED!")
        print("The framework is working correctly.")
    else:
        print("\nSOME TESTS FAILED")
        print("Check the output above for details.")

    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
