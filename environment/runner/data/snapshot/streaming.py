"""Streaming utilities for tar.gz archives.

This module provides file-like objects for streaming tar.gz data either to S3
using multipart upload or as an HTTP response, allowing TB-scale snapshots
without loading everything into memory.
"""

import asyncio
import io
import queue
import tarfile
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Protocol, cast

from loguru import logger


class S3ClientProtocol(Protocol):
    """Protocol for S3 client from aioboto3."""

    async def create_multipart_upload(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        """Create a multipart upload."""
        ...

    async def upload_part(
        self,
        *,
        Bucket: str,
        Key: str,
        PartNumber: int,
        UploadId: str,
        Body: bytes,
    ) -> dict[str, Any]:
        """Upload a part."""
        ...

    async def complete_multipart_upload(
        self,
        *,
        Bucket: str,
        Key: str,
        UploadId: str,
        MultipartUpload: dict[str, Any],
    ) -> dict[str, Any]:
        """Complete multipart upload."""
        ...

    async def abort_multipart_upload(
        self, *, Bucket: str, Key: str, UploadId: str
    ) -> dict[str, Any]:
        """Abort multipart upload."""
        ...


class S3ServiceResourceProtocol(Protocol):
    """Protocol for S3 service resource from aioboto3."""

    @property
    def meta(self) -> Any:
        """Meta object containing the client."""
        ...

    async def Bucket(self, name: str) -> Any:
        """Get a bucket resource."""
        ...


class S3StreamUploader:
    """File-like object that streams data to S3 using multipart upload.

    This class implements a file-like interface that buffers data and uploads
    it to S3 in chunks using multipart upload. It can be used with tarfile
    or other libraries that expect a file-like object.

    The multipart upload threshold is 5 MiB - chunks smaller than this are
    buffered, and when the buffer exceeds the threshold, it's uploaded as a part.

    Example:
        async with get_s3_client() as s3:
            uploader = S3StreamUploader(s3, "my-bucket", "my-key.tar.gz")
            async with uploader:
                with tarfile.open(mode="w:gz", fileobj=uploader) as tf:
                    tf.add("file.txt")
                # Upload completes automatically on exit
    """

    def __init__(
        self,
        s3_resource: S3ServiceResourceProtocol,
        bucket: str,
        key: str,
        multipart_threshold: int = 5 * 1024 * 1024,  # 5 MiB
    ):
        """Initialize the streaming uploader.

        Args:
            s3_resource: The S3 resource from aioboto3
            bucket: S3 bucket name
            key: S3 object key
            multipart_threshold: Minimum size (bytes) before using multipart upload
        """
        self.s3_resource: S3ServiceResourceProtocol = s3_resource
        self.bucket: str = bucket
        self.key: str = key
        self.multipart_threshold: int = multipart_threshold

        self.buffer: io.BytesIO = io.BytesIO()
        self.total_size: int = 0
        self.multipart_upload_id: str | None = None
        self.parts: list[dict[str, Any]] = []
        self.part_number: int = 1
        self._aborted: bool = False
        self._should_stop: bool = False
        self._flush_task: asyncio.Task[None] | None = None
        self._thread_lock: threading.Lock = threading.Lock()  # For sync write()
        self._async_lock: asyncio.Lock = asyncio.Lock()  # For async operations

    async def _init_multipart_upload(self) -> None:
        """Initialize multipart upload if not already started."""
        if self.multipart_upload_id is None:
            client = self.s3_resource.meta.client
            response = await client.create_multipart_upload(
                Bucket=self.bucket, Key=self.key
            )
            self.multipart_upload_id = response["UploadId"]
            logger.debug(
                f"Started multipart upload {self.multipart_upload_id} for s3://{self.bucket}/{self.key}"
            )

    async def _upload_part(self, data: bytes) -> dict[str, Any]:
        """Upload a single part and return part info.

        Args:
            data: The data to upload as a part

        Returns:
            Dict with 'ETag' and 'PartNumber' for completing multipart upload
        """
        await self._init_multipart_upload()
        assert self.multipart_upload_id is not None

        client = self.s3_resource.meta.client
        response = await client.upload_part(
            Bucket=self.bucket,
            Key=self.key,
            PartNumber=self.part_number,
            UploadId=self.multipart_upload_id,
            Body=data,
        )

        part_info: dict[str, Any] = {
            "ETag": response["ETag"],
            "PartNumber": self.part_number,
        }
        self.part_number += 1
        return part_info

    async def _flush_buffer(self) -> None:
        """Upload buffered data as a part if buffer is large enough.

        Uses thread lock (via executor) to safely read buffer while write()
        operations may be happening synchronously. This prevents race conditions
        where write() modifies the buffer while flush is reading/truncating it.
        """
        loop = asyncio.get_event_loop()

        # Acquire thread lock and read buffer in executor to avoid blocking event loop
        def read_buffer_safely():
            """Read buffer data with thread lock protection."""
            with self._thread_lock:
                buffer_size = self.buffer.tell()
                if buffer_size >= self.multipart_threshold:
                    _ = self.buffer.seek(0)
                    data = self.buffer.read()
                    _ = self.buffer.seek(0)
                    _ = self.buffer.truncate(0)
                    return data
                return None

        # Run buffer read in executor (doesn't block event loop)
        data = await loop.run_in_executor(None, read_buffer_safely)

        if data:
            # Protect upload operations with async lock
            async with self._async_lock:
                part_info = await self._upload_part(data)
                self.parts.append(part_info)
            logger.debug(
                f"Uploaded part {part_info['PartNumber']} ({len(data)} bytes) for s3://{self.bucket}/{self.key}"
            )

    async def _background_flush_loop(self) -> None:
        """Background task that periodically checks buffer size and flushes.

        This task runs concurrently with tarfile writes, checking every 100ms
        if the buffer has exceeded the multipart threshold and needs to be flushed.
        This enables true streaming uploads without accumulating all data in memory.
        """
        while not self._should_stop:
            try:
                await self._flush_buffer()
                # Check every 100ms
                await asyncio.sleep(0.1)
            except Exception as e:
                # Log errors but don't stop the loop unless we're shutting down
                # The error will be caught when we try to complete the upload
                logger.error(
                    f"Error in background flush loop for s3://{self.bucket}/{self.key}: {e}"
                )
                if self._should_stop:
                    break
                # Continue running even if flush fails (will retry on next iteration)
                await asyncio.sleep(0.1)

    def write(self, data: bytes) -> int:
        """Write data to the buffer (synchronous, called by tarfile).

        This method is called synchronously by tarfile during compression.
        Uses threading.Lock to protect buffer operations from concurrent
        access by the background flush task.

        Args:
            data: Bytes to write

        Returns:
            Number of bytes written
        """
        with self._thread_lock:
            written = self.buffer.write(data)
            self.total_size += written
        return written

    async def flush(self) -> None:
        """Flush buffered data to S3."""
        await self._flush_buffer()

    async def close(self) -> None:
        """Close the uploader and complete the multipart upload."""
        if self._aborted:
            return

        # Stop the background flush task
        self._should_stop = True
        if self._flush_task is not None:
            try:
                await asyncio.wait_for(self._flush_task, timeout=5.0)
            except TimeoutError:
                logger.warning(
                    f"Background flush task did not stop cleanly for s3://{self.bucket}/{self.key}"
                )
                self._flush_task.cancel()
            except Exception as e:
                logger.warning(
                    f"Error stopping background flush task for s3://{self.bucket}/{self.key}: {e}"
                )

        # Upload any remaining buffered data
        await self._flush_buffer()

        # Get remaining data with thread lock protection
        loop = asyncio.get_event_loop()

        def read_remaining():
            """Read remaining buffer data with thread lock."""
            with self._thread_lock:
                buffer_size = self.buffer.tell()
                if buffer_size > 0:
                    _ = self.buffer.seek(0)
                    return self.buffer.read()
                return None

        remaining_data = await loop.run_in_executor(None, read_remaining)

        if remaining_data:
            if self.multipart_upload_id:
                # Upload as final part
                part_info = await self._upload_part(remaining_data)
                self.parts.append(part_info)
            else:
                # Small file, use simple upload
                try:
                    bucket_res = await self.s3_resource.Bucket(self.bucket)
                    obj = await bucket_res.Object(self.key)
                    _ = await obj.put(Body=remaining_data)
                except Exception as e:
                    logger.error(
                        f"Failed to upload small file to s3://{self.bucket}/{self.key}: {e}"
                    )
                    raise

        # Complete multipart upload if we started one
        if self.multipart_upload_id and self.parts:
            client = self.s3_resource.meta.client
            _ = await client.complete_multipart_upload(
                Bucket=self.bucket,
                Key=self.key,
                UploadId=self.multipart_upload_id,
                MultipartUpload=cast(Any, {"Parts": self.parts}),
            )
            logger.debug(
                f"Completed multipart upload {self.multipart_upload_id} for s3://{self.bucket}/{self.key} ({len(self.parts)} parts)"
            )

    async def abort(self) -> None:
        """Abort the multipart upload if one was started."""
        if self.multipart_upload_id and not self._aborted:
            try:
                client = self.s3_resource.meta.client
                await client.abort_multipart_upload(
                    Bucket=self.bucket,
                    Key=self.key,
                    UploadId=self.multipart_upload_id,
                )
                logger.debug(
                    f"Aborted multipart upload {self.multipart_upload_id} for s3://{self.bucket}/{self.key}"
                )
            except Exception as e:
                logger.warning(
                    f"Failed to abort multipart upload {self.multipart_upload_id} for s3://{self.bucket}/{self.key}: {e}"
                )
            finally:
                self._aborted = True

    def tell(self) -> int:
        """Return current position (total bytes written)."""
        return self.total_size

    def read(self, size: int = -1) -> bytes:  # noqa: ARG002
        """Read is not supported for streaming upload (write-only)."""
        raise NotImplementedError("Read not supported for streaming upload")

    def seek(self, pos: int, whence: int = 0) -> int:
        """Seek is not supported for streaming upload."""
        if pos == 0 and whence == 0:
            # Allow seek(0) for compatibility, but it's a no-op
            return 0
        raise OSError("Seek not supported for streaming upload")

    async def __aenter__(self) -> "S3StreamUploader":
        """Async context manager entry.

        Starts the background flush task that periodically checks buffer size
        and uploads parts to S3, enabling true streaming uploads.
        """
        self._should_stop = False
        self._flush_task = asyncio.create_task(self._background_flush_loop())
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object | None,
    ) -> None:
        """Async context manager exit - handles cleanup and error cases.

        Stops the background flush task and either aborts (on error) or
        completes (on success) the multipart upload.
        """
        if exc_type is not None:
            # Error occurred, abort multipart upload
            self._should_stop = True
            if self._flush_task is not None:
                self._flush_task.cancel()
                try:
                    await self._flush_task
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    logger.warning(f"Error cancelling background flush task: {e}")
            await self.abort()
        else:
            # Success, complete the upload
            await self.close()


class StreamingTarFile:
    """File-like object that streams chunks via a queue for memory-efficient streaming.

    This allows streaming tar.gz data as it's being created without buffering
    everything in memory. Uses a queue to enable producer-consumer pattern where
    tarfile writes chunks to the queue and a generator yields them immediately.

    This is memory-safe for TB-scale files as chunks are yielded as soon as
    they're written, rather than accumulating in memory.
    """

    # Timeout for periodic closed flag checks (in seconds)
    TIMEOUT_SECONDS: float = 0.1

    def __init__(self, queue_size: int = 100) -> None:
        """Initialize the streaming tar file.

        Args:
            queue_size: Maximum number of chunks to buffer in the queue.
                Larger values use more memory but provide better throughput.
                Default is 100 chunks.
        """
        self.queue: queue.Queue[bytes | None] = queue.Queue(maxsize=queue_size)
        self.closed: bool = False
        self.total_size: int = 0
        self._write_error: Exception | None = None

    def write(self, data: bytes) -> int:
        """Write data - puts it in the queue for immediate consumption.

        This method is called synchronously by tarfile during compression.
        Chunks are immediately available for yielding via the generator.

        Args:
            data: Bytes to write

        Returns:
            Number of bytes written

        Raises:
            ValueError: If the file is closed
            RuntimeError: If a previous write error occurred
        """
        if self.closed:
            raise ValueError("I/O operation on closed file")
        if self._write_error:
            raise RuntimeError("Previous write error occurred") from self._write_error

        try:
            # Put chunk in queue - this will block if queue is full,
            # allowing backpressure to slow down tarfile if consumer is slow
            self.queue.put(data, block=True)
            self.total_size += len(data)
            return len(data)
        except Exception as e:
            # Store error so generator can detect it
            self._write_error = e
            raise

    def read(self, size: int = -1) -> bytes:  # noqa: ARG002
        """Read is not supported (write-only)."""
        raise NotImplementedError("Read not supported")

    def seek(self, pos: int, whence: int = 0) -> int:  # noqa: ARG002
        """Seek is not supported."""
        if pos == 0 and whence == 0:
            return 0
        raise OSError("Seek not supported")

    def tell(self) -> int:
        """Return total bytes written."""
        return self.total_size

    def set_error(self, error: Exception) -> None:
        """Set an error that occurred during writing.

        Args:
            error: The exception that occurred
        """
        self._write_error = error

    def close(self) -> None:
        """Mark as closed and signal end of data with None sentinel.

        If the queue is full and the sentinel cannot be added immediately,
        the iterator will detect closure via periodic timeout checks.
        """
        if not self.closed:
            self.closed = True
            # Put None sentinel to signal end of stream
            try:
                self.queue.put(None, block=False)
            except queue.Full:
                # Queue is full - iterator will detect closed flag via timeout check
                logger.debug(
                    "Queue full when closing, iterator will detect closure via timeout check"
                )
                pass

    def __iter__(self) -> Iterator[bytes]:
        """Iterate over chunks as they're written to the queue.

        This generator yields chunks immediately as they're written by tarfile,
        enabling true streaming without accumulating all data in memory.

        Uses timeout-based checks to detect closure even when the queue is full
        and the sentinel cannot be added, preventing deadlocks.

        Yields:
            Bytes chunks of the tar.gz archive

        Raises:
            RuntimeError: If a write error occurred during tarfile creation
        """
        while True:
            try:
                # Use timeout to periodically check closed flag
                # This prevents deadlock when queue is full and sentinel can't be added
                chunk = self.queue.get(block=True, timeout=self.TIMEOUT_SECONDS)
                if chunk is None:
                    # None sentinel indicates end of stream
                    break
                yield chunk
            except queue.Empty:
                # Timeout occurred - check if stream is closed
                if self.closed:
                    # Stream closed - check if queue has remaining data
                    # Use non-blocking get to avoid another timeout
                    try:
                        chunk = self.queue.get_nowait()
                        if chunk is None:
                            break
                        yield chunk
                        # Continue loop to get more chunks
                        continue
                    except queue.Empty:
                        # Queue is empty and closed - we're done
                        break
                # Not closed, continue waiting
                continue
            except Exception:
                # Check if write error occurred
                if self._write_error:
                    raise RuntimeError(
                        "Error during tarfile creation"
                    ) from self._write_error
                raise

        # Verify no write errors occurred
        if self._write_error:
            raise RuntimeError("Error during tarfile creation") from self._write_error


def create_tar_gz_stream(
    subsystems: list[str],
    snapshot_id: str,
    iter_paths_func: Callable[[str, str], Iterator[tuple[Path, str]]],
) -> Iterator[bytes]:
    """Create a tar.gz archive and yield chunks as bytes.

    This generator runs tarfile compression in a background thread and yields
    chunks directly as they're produced, enabling true streaming without
    buffering everything in memory.

    Args:
        subsystems: List of subsystem names to include in archive
        snapshot_id: Snapshot ID for logging
        iter_paths_func: Function to iterate over file paths for a subsystem

    Yields:
        Bytes chunks of the tar.gz archive

    Raises:
        RuntimeError: If tarfile creation fails
    """
    stream_file = StreamingTarFile()

    # Run tarfile compression in a background thread
    # This allows chunks to be yielded immediately as they're produced
    def create_archive():
        """Create tar.gz archive, writing chunks to stream_file."""
        try:
            with tarfile.open(mode="w:gz", fileobj=stream_file) as tf:
                for subsystem in subsystems:
                    subsystem_path = f"/{subsystem}"
                    logger.debug(
                        f"Adding subsystem '{subsystem}' from {subsystem_path} to archive"
                    )
                    file_count = 0
                    for path, arcname in iter_paths_func(subsystem_path, subsystem):
                        tf.add(path, arcname=arcname, recursive=False)
                        file_count += 1
                    logger.debug(
                        f"Added {file_count} file(s) from subsystem '{subsystem}'"
                    )
        except Exception as e:
            stream_file.set_error(e)
            logger.error(
                f"Error creating tar archive for snapshot {snapshot_id}: {repr(e)}"
            )
        finally:
            stream_file.close()

    # Start tarfile compression in background thread
    archive_thread = threading.Thread(target=create_archive, daemon=True)
    archive_thread.start()

    # Yield chunks as they're written to the queue
    # The generator blocks waiting for chunks, enabling true streaming
    yield from stream_file
