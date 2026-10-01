"""Capture rootfs changes as an OCI image layer and upload to S3."""

from .main import handle_rootfs_baseline, handle_rootfs_capture

__all__ = ["handle_rootfs_baseline", "handle_rootfs_capture"]
