"""Tests for data management endpoints (populate and snapshot).

Verifies the /data/* endpoints for populating and snapshotting subsystems.
"""

import gzip
import io
import tarfile

import httpx
import pytest


def _create_test_tar_gz(files: dict[str, bytes]) -> bytes:
    """Create a tar.gz archive in memory with the given files.

    Args:
        files: Dict mapping filename to file contents

    Returns:
        The tar.gz archive as bytes
    """
    tar_buffer = io.BytesIO()
    with tarfile.open(fileobj=tar_buffer, mode="w:gz") as tar:
        for filename, content in files.items():
            file_data = io.BytesIO(content)
            tarinfo = tarfile.TarInfo(name=filename)
            tarinfo.size = len(content)
            tar.addfile(tarinfo, file_data)
    tar_buffer.seek(0)
    return tar_buffer.read()


class TestPopulateEndpoints:
    """Tests for /data/populate endpoints."""

    @pytest.mark.asyncio
    async def test_populate_with_file(self, base_url: str) -> None:
        """Test populating filesystem with an actual file upload."""
        # Create a test tar.gz with a simple file
        test_content = b"Hello from test file!"
        archive = _create_test_tar_gz({"test_file.txt": test_content})

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{base_url}/data/populate",
                params={"subsystem": "filesystem"},
                files={"archive": ("test.tar.gz", archive, "application/gzip")},
                timeout=60,
            )

        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code}: {response.text}"
        )

        # Verify response contains objects_added
        data = response.json()
        assert "objects_added" in data, f"Expected objects_added in response: {data}"
        assert data["objects_added"] >= 1, f"Expected at least 1 object added: {data}"

    @pytest.mark.asyncio
    async def test_populate_endpoint_validation_error(self, base_url: str) -> None:
        """Test that /data/populate returns validation error without file."""
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{base_url}/data/populate",
                params={"subsystem": "filesystem"},
                timeout=30,
            )

        # 422 means endpoint exists but validation failed (no file provided)
        assert response.status_code in [422, 400], (
            f"Expected 422 or 400 (validation error), got {response.status_code}"
        )

    @pytest.mark.asyncio
    async def test_populate_s3_endpoint_exists(self, base_url: str) -> None:
        """Test that /data/populate/s3 endpoint exists and responds."""
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{base_url}/data/populate/s3",
                json={"sources": []},
                timeout=30,
            )

        # Should accept empty sources (no-op) or return validation error
        # 404 would mean endpoint doesn't exist
        assert response.status_code != 404, (
            f"Expected endpoint to exist, got 404: {response.text}"
        )


class TestSnapshotEndpoints:
    """Tests for /data/snapshot endpoints."""

    @pytest.mark.asyncio
    async def test_snapshot_endpoint_returns_tar_gz(self, base_url: str) -> None:
        """Test that /data/snapshot returns a valid tar.gz stream."""
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{base_url}/data/snapshot",
                timeout=60,
            )

        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code}: {response.text}"
        )

        # Should return gzip content type
        content_type = response.headers.get("content-type", "")
        assert "gzip" in content_type or "octet-stream" in content_type, (
            f"Expected gzip content type, got: {content_type}"
        )

        # Should have content-disposition header with filename
        content_disposition = response.headers.get("content-disposition", "")
        assert "attachment" in content_disposition, (
            f"Expected attachment disposition, got: {content_disposition}"
        )
        assert ".tar.gz" in content_disposition, (
            f"Expected .tar.gz filename, got: {content_disposition}"
        )

        # Verify it's actually a valid gzip/tar file
        content = response.content
        assert len(content) > 0, "Snapshot should not be empty"

        # Decompress and verify it's a valid tar
        decompressed = gzip.decompress(content)
        tar_buffer = io.BytesIO(decompressed)
        with tarfile.open(fileobj=tar_buffer, mode="r") as tar:
            # Should be able to list members without error
            members = tar.getnames()
            assert isinstance(members, list), "Should return list of members"

    @pytest.mark.asyncio
    async def test_snapshot_s3_endpoint_exists(self, base_url: str) -> None:
        """Test that /data/snapshot/s3 endpoint exists.

        Note: This will fail without real S3 credentials, but we verify
        the endpoint exists and responds (not 404).
        """
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{base_url}/data/snapshot/s3",
                timeout=30,
            )

        # Should return 500 (no S3 configured) or 200 (if S3 is configured)
        # 404 would mean endpoint doesn't exist
        assert response.status_code != 404, (
            f"Expected endpoint to exist, got 404: {response.text}"
        )


class TestPopulateSnapshotRoundtrip:
    """End-to-end test: populate data, then verify it appears in snapshot."""

    @pytest.mark.asyncio
    async def test_populated_data_appears_in_snapshot(self, base_url: str) -> None:
        """Test that data populated via /data/populate appears in /data/snapshot."""
        # Create test files with unique content
        test_files = {
            "roundtrip_test.txt": b"This is a roundtrip test file",
            "subdir/nested_file.txt": b"Nested file content",
        }
        archive = _create_test_tar_gz(test_files)

        async with httpx.AsyncClient() as client:
            # Step 1: Populate the filesystem
            populate_response = await client.post(
                f"{base_url}/data/populate",
                params={"subsystem": "filesystem"},
                files={"archive": ("test.tar.gz", archive, "application/gzip")},
                timeout=60,
            )
            assert populate_response.status_code == 200, (
                f"Populate failed: {populate_response.text}"
            )

            # Step 2: Create a snapshot
            snapshot_response = await client.post(
                f"{base_url}/data/snapshot",
                timeout=60,
            )
            assert snapshot_response.status_code == 200, (
                f"Snapshot failed: {snapshot_response.text}"
            )

        # Step 3: Verify the snapshot contains our files
        snapshot_content = snapshot_response.content
        decompressed = gzip.decompress(snapshot_content)
        tar_buffer = io.BytesIO(decompressed)

        with tarfile.open(fileobj=tar_buffer, mode="r") as tar:
            member_names = tar.getnames()

            # Check that our files are in the snapshot
            # Files might be under filesystem/ prefix
            found_roundtrip = any("roundtrip_test.txt" in name for name in member_names)
            found_nested = any("nested_file.txt" in name for name in member_names)

            assert found_roundtrip, (
                f"roundtrip_test.txt not found in snapshot. Members: {member_names}"
            )
            assert found_nested, (
                f"nested_file.txt not found in snapshot. Members: {member_names}"
            )
